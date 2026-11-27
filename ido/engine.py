"""Persistent simulated accounts. This module has no broker order transport."""

import json
import math
import sqlite3
import uuid
from contextlib import closing

import advisor_scores as scores
import agent_coordinator as coordinator
import readiness
import forward_learning as learning
from advisor_journal import init_advisors, latest_round, record_outcome, record_round
from advisors import Risk, causal_bars, frozen_input, risk
from books import (SWING_MINUTES, book_of, entry_limit, freeze_swing, init_swing,
                   prefs, scalp_filter, settle_swing, swing_setup)
from candle_features import describe
from connections import bridge_check, bridge_record
from costs import COSTS
from market import GOLD, REAL, SIM
from spot import init_spot
from strategy import BASELINE, START_BALANCE, market_regime, metrics, signal, variant_name
from validation import init_validation, profile as validation_profile, record_entry, record_exit, snapshot

DEFAULT = dict(provider=GOLD, maxPositions=1, riskPct=0.5, dailyLossPct=2)


class Rejected(Exception):
    pass


def initial(now):
    return dict(settings=DEFAULT.copy(), engine="stopped", day=now // 86400, anchor=START_BALANCE,
                balance=START_BALANCE, lastBar=0, feedStatus="unavailable", riskLockedDay=None)


def connect(path):
    db = sqlite3.connect(path, timeout=1.25)
    db.row_factory = sqlite3.Row
    return db


def initialize(path):
    with closing(connect(path)) as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, state TEXT NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, account TEXT NOT NULL,
                time INTEGER NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS audit_account ON audit(account, id);
            CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, account TEXT NOT NULL, opened INTEGER NOT NULL,
                closed INTEGER, pnl REAL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS trades_account ON trades(account, closed);
            CREATE INDEX IF NOT EXISTS trades_account_opened ON trades(account, opened);
            CREATE TABLE IF NOT EXISTS bridge (id INTEGER PRIMARY KEY CHECK(id=1), seen INTEGER NOT NULL,
                balance REAL NOT NULL, currency TEXT NOT NULL, identity TEXT);
            CREATE TABLE IF NOT EXISTS bridge_nonces (nonce TEXT PRIMARY KEY, seen INTEGER NOT NULL);
        """)
        if "identity" not in {column["name"] for column in db.execute("PRAGMA table_info(bridge)")}:
            db.execute("ALTER TABLE bridge ADD COLUMN identity TEXT")
        init_validation(db)
        init_swing(db)
        init_spot(db)
        init_advisors(db)
        coordinator.init_mailbox(db)
        learning.init_learning(db)
        scores.init_scores(db)


def audit(db, sid, now, level, message):
    db.execute("INSERT INTO audit(account,time,level,message) VALUES(?,?,?,?)", (sid, now, level, message))


def load(db, sid, now):
    row = db.execute("SELECT state FROM accounts WHERE id=?", (sid,)).fetchone()
    return json.loads(row["state"]) if row else initial(now)


def store(db, sid, state, positions):
    db.execute("""INSERT INTO accounts VALUES(?,?,?) ON CONFLICT(id) DO UPDATE
                  SET state=excluded.state, active=excluded.active""",
               (sid, json.dumps(state), bool(state["engine"] == "running" or positions or learning.pending(db, sid)
                 or db.execute("SELECT 1 FROM swing_calls WHERE account=? AND evaluated IS NULL LIMIT 1", (sid,)).fetchone())))


def get_positions(db, sid):
    return [json.loads(r["data"]) for r in db.execute(
        "SELECT data FROM trades WHERE account=? AND closed IS NULL ORDER BY opened", (sid,))]


def chart_trades(db, sid, provider, start, end):
    ids = db.execute("""SELECT data FROM trades WHERE account=? AND opened BETWEEN ? AND ?
                        UNION
                        SELECT data FROM trades WHERE account=? AND closed BETWEEN ? AND ?""",
                     (sid, start, end, sid, start, end))
    return [trade for row in ids if (trade := json.loads(row["data"]))["feed"] == provider]


def cost_per_oz(trade):
    return sum(trade.get(key) or 0 for key in ("spreadUsdPerOz", "feesUsdPerOz", "slippageUsdPerOz"))


def pnl(trade, price):
    move = (price - trade["entry"]) * (1 if trade["side"] == "long" else -1)
    return round((move - cost_per_oz(trade)) * trade["quantity"], 2)


def decision(db, sid, state, now, action, reason, quote, model=None, trade=None, book="scalp"):
    decisions = state.setdefault("bookDecisions", {})
    old = decisions.get(book)
    decisions[book] = dict(action=action, reason=reason, time=now, quoteTime=quote["time"], model=model)
    previous = state.get("decision")
    if not (action == "WAIT" and previous and previous["time"] == now and previous["action"] != "WAIT"):
        state["decision"] = decisions[book]
    if action != "WAIT" or not old or old["action"] != "WAIT" or old["reason"] != reason:
        audit(db, sid, now, "info", f"{'Swing' if book == 'swing' else 'Kortetermijn'} · {action} · {reason}")
    if book != "scalp":
        return
    target = state.get("learningTarget") if quote["fresh"] else None
    learning.audit_decision(db, sid, state["settings"]["provider"], now, action, reason,
                            describe(causal_bars(quote["bars"], now, quote["time"], quote["fresh"])),
                            trade, target if target and now < target else None)


def close_trade(db, sid, state, trade, quote, now, reason):
    mark = quote["price"]
    decision(db, sid, state, now, "CLOSE", reason, quote, trade.get("model"), trade["id"], book_of(trade))
    trade.update(closedAt=now, exit=mark, pnl=pnl(trade, mark), reason=reason,
                 exitQuoteAt=quote["time"], costUsd=round(cost_per_oz(trade) * trade["quantity"], 2))
    state["balance"] = round(state["balance"] + trade["pnl"], 2)
    db.execute("UPDATE trades SET closed=?,pnl=?,data=? WHERE id=? AND account=?",
               (now, trade["pnl"], json.dumps(trade), trade["id"], sid))
    record_exit(db, sid, trade, quote)
    record_outcome(db, sid, trade)
    audit(db, sid, now, "info", f"{reason} · {trade['pnl']:+.2f} USD netto")
    readiness.announce(db, sid, state["settings"]["provider"], state, now, audit)


def open_trade(db, sid, state, direction, quote, qty, distance, now, model, round_id=None, reason=None, regime=None,
               book="scalp", horizon=15):
    sign = 1 if direction == "long" else -1
    mark = quote["price"]
    spread, fee, slip = COSTS
    trade = dict(id=uuid.uuid4().hex, origin="paper", openedAt=now, closedAt=None, entry=mark, regime=regime,
                 strategy=book, holdMinutes=horizon,
                 side=direction, exit=None, stop=round(mark - sign * distance, 3),
                 target=round(mark + sign * distance * 1.6, 3), quantity=qty,
                 feed=state["settings"]["provider"], pnl=None, reason=None, model=model,
                 advisorRoundId=round_id, entryReason=reason or "Synthetische test · signaal van de hoofdstrategie.",
                 entryQuoteAt=quote["time"], exitQuoteAt=None, spreadUsdPerOz=spread, feesUsdPerOz=fee,
                 slippageUsdPerOz=slip, costUsd=round((spread + fee + slip) * qty, 2))
    decision(db, sid, state, now, "BUY" if direction == "long" else "SELL",
             f"Paperpositie geopend · {qty:.3f} oz · {model}", quote, model, trade["id"], book)
    db.execute("INSERT INTO trades VALUES(?,?,?,?,?,?)", (trade["id"], sid, now, None, None, json.dumps(trade)))
    return trade


def risk_at_stop(positions, mark):
    return sum(max(0, ((mark - t["stop"]) * (1 if t["side"] == "long" else -1) + cost_per_oz(t)) * t["quantity"])
               for t in positions) if mark is not None else 0


def refresh_day(db, sid, now, state, equity):
    if now // 86400 == state["day"]:
        return
    state.update(day=now // 86400, anchor=equity)
    if state["engine"] == "risk_locked":
        state["engine"] = "stopped"
        audit(db, sid, now, "info", "Nieuwe UTC-dag. Dagverliesblokkade opgeheven; START is opnieuw beschikbaar.")


def log_blocked_round(db, sid, state, quote, now, positions, daily_room):
    config = state["settings"]
    if config["provider"] == SIM:
        return
    bars = causal_bars(quote["bars"], now, quote["time"], quote["fresh"])
    inputs = Risk(quote["fresh"], COSTS is not None, len(positions) < config["maxPositions"], daily_room, False, None)
    job = coordinator.prepare(config["provider"], bars, "WAIT", inputs, now, quote)
    report = coordinator.conclusion(job, now)
    record_round(db, sid, config["provider"], report, frozen_input(bars, inputs, quote["price"], None), False)


def cancel_discussion(db, sid, provider, row, now, reason):
    if not row:
        return
    job = json.loads(row["payload"])
    report = coordinator.conclusion(job, now, reason)
    record_round(db, sid, provider, report, job["inputs"], False)
    coordinator.remove(db, row)


def entry_room(state, positions, mark, side, distance, book):
    """Single gross exposure, position count and day-loss budget across both books."""
    if any(book_of(t) == book for t in positions):
        return 0, "Dit handelsboek heeft al een open positie."
    if len(positions) >= state["settings"]["maxPositions"]:
        return 0, "Het gezamenlijke maximum voor open posities is bereikt."
    if side and any(t["side"] != side for t in positions):
        return 0, "Een tegengestelde demo-positie is nog open."
    equity = state["balance"] + sum(pnl(t, mark) for t in positions)
    budget = min(state["balance"], equity) * state["settings"]["riskPct"] / 100
    daily_cap = state["anchor"] * state["settings"]["dailyLossPct"] / 100
    daily_left = daily_cap - max(0, state["anchor"] - equity) - risk_at_stop(positions, mark)
    if COSTS is None or budget <= 0 or daily_left <= 0:
        return 0, "Onvoldoende ruimte binnen de gezamenlijke risicolimieten."
    spare_notional = max(0, equity * .75 - sum(t["quantity"] * mark for t in positions))
    unit_risk = distance + sum(COSTS)
    qty = math.floor(max(0, min(budget / unit_risk, daily_left / unit_risk,
                               equity * .25 / mark, spare_notional / mark)) * 1000) / 1000
    return qty, None if qty >= .001 else "Te weinig ruimte binnen het gezamenlijke risicobudget."


def evaluate_swing(db, sid, state, positions, hours, cutoff, quote, now):
    model = "Swing · H1/H4"
    action, explanation, distance, volatility = swing_setup(hours, cutoff, quote["price"])
    hour = hours[-1]["time"] if hours else None
    if (state.get("lastSwingHour") == hour and state.get("bookDecisions", {}).get("swing")):
        return positions
    state["lastSwingHour"] = hour
    side = {"BUY": "long", "SELL": "short"}.get(action)
    reason = explanation if side is None else entry_limit(db, sid, "swing", now)
    if reason is None and sum(COSTS or ()) > distance * .20:
        reason = "Spread, kosten en slippage zijn te hoog voor de swingstop."
    if reason is None:
        qty, reason = entry_room(state, positions, quote["price"], side, distance, "swing")
    if reason is not None:
        decision(db, sid, state, now, "WAIT", reason, quote, model, book="swing")
        return positions
    equity = state["balance"] + sum(pnl(t, quote["price"]) for t in positions)
    trade = open_trade(db, sid, state, side, quote, qty, distance, now, model, reason=explanation,
                       regime="bullish" if side == "long" else "bearish", book="swing", horizon=SWING_MINUTES)
    if trade["feed"] != SIM:
        record_entry(db, sid, state["settings"], trade, hours, quote, equity, swing_volatility=volatility)
        freeze_swing(db, sid, trade["feed"], trade, now)
    return [*positions, trade]


def evaluate(db, sid, state, positions, market, now):
    config = state["settings"]
    provider = config["provider"]
    quote = market.execution_quote(provider, now)
    preferences = prefs(state)
    waiting = coordinator.pending(db, sid, provider) if provider != SIM else None
    if waiting and not preferences["scalpEnabled"]:
        cancel_discussion(db, sid, provider, waiting, now, "Kortetermijn staat uit.")
        waiting = None
    mark, status = quote["price"], quote["status"]
    cutoff = min(now, quote["time"] or now)
    swing_pending = provider != SIM and db.execute(
        "SELECT 1 FROM swing_calls WHERE account=? AND provider=? AND evaluated IS NULL LIMIT 1", (sid, provider)).fetchone()
    hours = market.strategy_hours(provider, cutoff) if quote["fresh"] and (preferences["swingEnabled"] or swing_pending) else []
    if swing_pending:
        settle_swing(db, sid, provider, hours, cutoff, now)
    remote_mode = getattr(market, "agent_remote", False)
    key = learning.policy(config, remote_mode)
    if provider != SIM and now >= state.get("lastLearningSettle", 0)+10:
        learning.settle(db, sid, provider, quote, now)
        scores.settle(db, sid, provider, quote["bars"] if quote["fresh"] else [], min(now, quote["time"] or now), now)
        state["lastLearningSettle"] = now
    advice_bars = causal_bars(quote["bars"], now, quote["time"], quote["fresh"]) if provider != SIM else ()
    arm = "basis"
    if provider != SIM and state["engine"] == "running" and preferences["scalpEnabled"] and advice_bars:
        m = learning.advance(db, sid, key, config, now, remote_mode)
        arm = m["active"]
        if advice_bars[-1].time > state.get("lastLearningBar", 0):
            state["lastLearningBar"] = advice_bars[-1].time
            distance = max(0.30, mark * .001)
            # Matched, independent research accounts; current account limits still apply to any true paper execution below.
            matched = Risk(True, COSTS is not None, True, True, True,
                           sum(COSTS)/distance if COSTS else None, distance)
            state["learningTarget"] = learning.save_call(db, sid, key, provider, advice_bars, quote, now, matched, config, arm)
    else:
        state["learningTarget"] = None
    if state["engine"] == "running" and status != state["feedStatus"]:
        audit(db, sid, now, "info" if status in ("simulated", "connected") else "warning",
              {"simulated":"Simulatiekoers actief.", "connected":"XAU/USD-feed geeft actuele koersen.",
               "stale":"Koers verouderd; geen nieuwe paperposities.", "unavailable":"Koersfeed onbeschikbaar; geen nieuwe paperposities."}[status])
    state["feedStatus"] = status
    equity = state["balance"] + sum(pnl(t, mark) for t in positions) if mark is not None else state["balance"]
    if not positions or status in ("connected", "simulated"):
        refresh_day(db, sid, now, state, equity)
    if status not in ("connected", "simulated") or mark is None:
        if state["engine"] == "running" and (state.get("decision") or {}).get("reason") != "Geen actuele koers":
            if waiting:
                cancel_discussion(db, sid, provider, waiting, now, "Discussie afgebroken: de koersfeed is niet actueel.")
            else:
                log_blocked_round(db, sid, state, quote, now, positions, True)
            decision(db, sid, state, now, "WAIT", "Geen actuele koers", quote)
        if state["engine"] == "running" and preferences["swingEnabled"] and (
                state.get("bookDecisions", {}).get("swing", {}).get("reason") != "Geen actuele koers"):
            decision(db, sid, state, now, "WAIT", "Geen actuele koers", quote, book="swing")
            state.pop("lastSwingHour", None)
        return positions
    new_quote = quote["time"] is not None and quote["time"] > state.get("lastQuote", 0)
    if not new_quote and waiting is None:
        return positions
    if new_quote:
        state["lastQuote"] = quote["time"]
    max_loss = max(0, state["anchor"]) * config["dailyLossPct"] / 100
    if state["engine"] == "running" and (equity <= 0 or state["anchor"] - equity >= max_loss):
        if waiting:
            cancel_discussion(db, sid, provider, waiting, now, "Zittende posities beschermd; dagverlieslimiet bereikt.")
        else:
            log_blocked_round(db, sid, state, quote, now, positions, False)
        for trade in positions:
            close_trade(db, sid, state, trade, quote, now, "Dagverlieslimiet")
        state.update(engine="risk_locked", riskLockedDay=state["day"])
        audit(db, sid, now, "warning", "Dagverlieslimiet bereikt. Nieuwe paperorders geblokkeerd tot de volgende UTC-dag.")
        return []
    still_open = []
    for trade in positions:
        long = trade["side"] == "long"
        stop = mark <= trade["stop"] if long else mark >= trade["stop"]
        target = mark >= trade["target"] if long else mark <= trade["target"]
        expired = now - trade["openedAt"] >= trade.get("holdMinutes", 3 if provider == SIM else 15) * 60
        if new_quote and (state["engine"] != "running" or stop or target or expired):
            reason = ("Noodstop" if state["engine"] == "emergency" else "Stop") if state["engine"] != "running" else (
                "Stop-loss" if stop else "Koersdoel" if target else "Houdtijd verstreken")
            if quote["time"] > (trade.get("entryQuoteAt") or 0) or state["engine"] != "running":
                close_trade(db, sid, state, trade, quote, now, reason)
            else:
                still_open.append(trade)
        else:
            still_open.append(trade)
    if state["engine"] != "running" or len(still_open) != len(positions):
        cancel_discussion(db, sid, provider, waiting, now, "Discussie afgebroken: bescherming van bestaande posities heeft voorrang.")
        return still_open
    if new_quote and preferences["swingEnabled"]:
        still_open = evaluate_swing(db, sid, state, still_open, hours, cutoff, quote, now)
    if not preferences["scalpEnabled"]:
        return still_open
    capacity = len(still_open) < config["maxPositions"] and not any(book_of(t) == "scalp" for t in still_open)
    if provider == SIM and not capacity:
        return still_open
    bars = (market.entry_bars(provider, now) if provider == SIM else
            [dict(time=b.time, open=b.open, high=b.high, low=b.low, close=b.close) for b in advice_bars])
    new_bar = bool(bars and bars[-1]["time"] > state["lastBar"])
    if waiting:
        if waiting["phase"] != "ready" and now < waiting["deadline"]:
            return still_open
    elif not bars:
        if state.get("advisorNoCandles") and (state.get("decision") or {}).get("reason") != "Geen actuele koers":
            return still_open
        state["advisorNoCandles"] = True
    elif bars[-1]["time"] <= state["lastBar"] and (state.get("decision") or {}).get("reason") != "Geen actuele koers":
        return still_open
    elif new_bar:
        state["lastBar"] = bars[-1]["time"]
        state["advisorNoCandles"] = False
    if waiting:
        job = coordinator.expire(waiting, now)
        model_name = job["inputs"]["model"]
        arm = job["inputs"].get("arm", "basis")
        proposed = job["report"]["proposed"]
        direction = {"BUY": "long", "SELL": "short"}.get(proposed)
    else:
        model_name = variant_name(BASELINE) if provider == SIM else learning.model_label(arm)
        if provider == SIM:
            kind, *_ = signal(bars, BASELINE)
            proposed = {"long":"BUY", "short":"SELL"}.get(kind, "WAIT")
        elif advice_bars:
            proposed, *_ = learning.prediction(advice_bars, describe(advice_bars), arm)
        else:
            proposed = "WAIT"
        if not new_bar:
            proposed = "WAIT"
        direction = {"BUY": "long", "SELL": "short"}.get(proposed)
    if provider == SIM and proposed == "WAIT":
        decision(db, sid, state, now, "WAIT", "Geen duidelijk long- of shortsignaal", quote, model_name)
        return still_open
    if provider == SIM and COSTS is None:
        decision(db, sid, state, now, "WAIT", "Kosteninstellingen niet geldig", quote, model_name)
        return still_open
    equity = state["balance"] + sum(pnl(t, mark) for t in still_open)
    daily_left = max_loss - max(0, state["anchor"] - equity) - risk_at_stop(still_open, mark)
    distance = round(max(0.30, mark * .001), 3)
    qty, risk_reason = entry_room(state, still_open, mark, direction, distance, "scalp")
    guard = scalp_filter(bars, cutoff, mark, proposed)
    if direction is not None:
        guard = guard or entry_limit(db, sid, "scalp", now) or risk_reason
        if not guard and COSTS and sum(COSTS) / distance > .30:
            guard = "Spread, kosten en slippage zijn te hoog voor de scalpstop."
    can_execute = not guard and capacity and quote["fresh"] and COSTS is not None and qty >= 0.001 and proposed != "WAIT"
    report, round_id = None, None
    if provider != SIM:
        risk_input = Risk(quote["fresh"], COSTS is not None, capacity, daily_left > 0,
                          qty >= 0.001 and not guard, sum(COSTS) / distance if COSTS is not None else None, distance)
        inputs = frozen_input(advice_bars, risk_input, mark, model_name)
        inputs["arm"] = arm
        reason = None
        if waiting:
            original = job["snapshot"]
            if now >= waiting["deadline"] and waiting["phase"] == "ready":
                reason = "Discussie te laat afgerond voor een instap."
            elif original["candles"] != inputs["candles"] or not quote["fresh"] or quote["time"] < original["quoteTime"]:
                reason = "Marktsnapshot is veranderd; advies wordt niet gebruikt voor een nieuwe positie."
            elif abs(mark-original["price"]) > distance/2:
                reason = "De prijs is te ver verschoven sinds de snapshot."
            check = risk(advice_bars, risk_input)
            coordinator.event(job, now, "review", "risk", "Hercontrole vóór uitvoering: " + check["evidence"],
                              "coordinator", check["vote"])
            if check["vote"] != "PASS":
                reason = reason or "Risicohercontrole door de engine staat geen nieuwe positie toe."
            inputs = job["inputs"]
            coordinator.remove(db, waiting)
        else:
            remote = bool(advice_bars and getattr(market, "agent_remote", False))
            context = dict(learning=True, weights=scores.weights(db, sid, provider, now),
                           hours=market.strategy_hours(provider, cutoff) if quote["fresh"] else [])
            job = coordinator.prepare(provider, advice_bars, proposed, risk_input, now, quote, remote, context)
            if remote and not guard:
                if coordinator.start_remote(db, sid, provider, job, inputs, now):
                    decision(db, sid, state, now, "WAIT", "Adviseurs bespreken de marktsnapshot", quote, model_name)
                    return still_open
                job["unavailable"] = True
                coordinator.event(job, now, "unavailable", "remote", "De externe advieswachtrij is vol.")
                coordinator.discussion(job, now)
        report = coordinator.conclusion(job, now, reason or guard, can_execute)
        can_execute = report["executed"]
        round_id = record_round(db, sid, provider, report, inputs, can_execute)
        if advice_bars and COSTS is not None:
            scores.record(db, sid, provider, report["votes"], advice_bars[-1].time, advice_bars[-1].close, sum(COSTS))
    if can_execute:
        trade = open_trade(db, sid, state, direction, quote, qty, distance, now, model_name, round_id,
                           report["reason"] if report else None, market_regime(bars), horizon=preferences["scalpMinutes"])
        record_entry(db, sid, config, trade, bars, quote, equity, remote=remote_mode, strategy_arm=arm)
        still_open.append(trade)
    else:
        reason = guard or ("Zijwaartse koers: geen nieuwe long of short." if market_regime(bars) == "sideways" else
                           report["reason"] if report else "Onvoldoende ruimte binnen de risicolimieten")
        decision(db, sid, state, now, "WAIT", reason, quote, model_name)
    return still_open


def apply_action(db, sid, state, positions, market, now, action):
    provider = state["settings"]["provider"]
    quote = market.execution_quote(provider, now)
    mark = quote["price"]
    equity = state["balance"] + sum(pnl(t, mark) for t in positions) if mark is not None else state["balance"]
    if not positions or quote["fresh"]:
        refresh_day(db, sid, now, state, equity)
    if action == "start":
        if state["engine"] == "running":
            return positions
        if state["engine"] != "stopped" or positions or state.get("riskLockedDay") == now // 86400:
            raise Rejected("START is geblokkeerd. Controleer de noodstop, daglimiet en eventuele open posities.")
        if not any(prefs(state)[key] for key in ("scalpEnabled", "swingEnabled")):
            raise Rejected("Zet minstens één DEMO-strategie aan vóór START.")
        if not quote["fresh"]:
            raise Rejected("Geen actuele koers. Controleer de koersfeed in Instellingen.")
        if COSTS is None:
            raise Rejected("Ongeldige serverinstelling voor spread of kosten.")
        max_loss = state["anchor"] * state["settings"]["dailyLossPct"] / 100
        if equity <= 0 or state["anchor"] - equity >= max_loss:
            raise Rejected("Dagverlieslimiet bereikt. START is geblokkeerd tot de volgende UTC-dag.")
        state.update(engine="running", feedStatus=quote["status"])
        decision(db, sid, state, now, "WAIT", "Gestart; wacht op een nieuw signaal", quote)
        audit(db, sid, now, "success", "Papertrading gestart. Er gaan geen orders naar MT5.")
        return evaluate(db, sid, state, positions, market, now)
    if action == "reset_emergency":
        if state["engine"] != "emergency" or positions:
            raise Rejected("Noodstop kan pas worden opgeheven nadat paperposities zijn gesloten.")
        state["engine"] = "risk_locked" if state.get("riskLockedDay") == now // 86400 else "stopped"
        audit(db, sid, now, "info", "Noodstop opgeheven; papertrading blijft gestopt.")
        return positions
    if state["engine"] == "emergency" and action == "stop":
        return positions
    state["engine"] = "emergency" if action == "emergency_stop" else (
        "risk_locked" if state["engine"] == "risk_locked" else "stopped")
    cancel_discussion(db, sid, provider, coordinator.pending(db, sid, provider), now,
                      "Discussie afgebroken: de gebruiker stopte de engine.")
    text = "Noodstop geactiveerd. START geblokkeerd." if action == "emergency_stop" else "Papertrading gestopt."
    if quote["fresh"]:
        for trade in positions:
            close_trade(db, sid, state, trade, quote, now, "Noodstop" if action == "emergency_stop" else "Stop")
        positions = []
    elif positions:
        text += " Geen actuele koers; open paperposities worden bij de eerstvolgende actuele koers gesloten."
    audit(db, sid, now, "warning" if action == "emergency_stop" else "info", text)
    return positions


def save_settings(db, sid, state, positions, now, data, key_configured):
    if state["engine"] == "running" or positions:
        raise Rejected("Stop papertrading en wacht op sluiting van posities vóór je instellingen wijzigt.")
    if data["provider"] == REAL and not key_configured:
        raise Rejected("De server heeft geen Twelve Data-sleutel. Stel IDO_TWELVE_DATA_KEY in op de server.")
    if state["day"] == now // 86400 and state["anchor"] - state["balance"] >= state["anchor"] * state["settings"]["dailyLossPct"] / 100:
        state["riskLockedDay"] = state["day"]
        if state["engine"] != "emergency":
            state["engine"] = "risk_locked"
    # Risk can be raised only while stopped. A latched risk lock does not clear.
    if data["provider"] != state["settings"]["provider"]:
        learning.settle(db, sid, state["settings"]["provider"], {"bars": [], "fresh":False, "time":None}, now, canceled=True)
        settle_swing(db, sid, state["settings"]["provider"], [], now, now, cancel=True)
        state["lastBar"] = 0
        state["lastQuote"] = 0
        state["decision"] = None
        state["advisorNoCandles"] = False
        state["lastLearningBar"] = 0
        state.pop("lastSwingHour", None)
        state.pop("bookDecisions", None)
    if data != state["settings"]:
        state["learningTarget"] = None
    state["settings"] = data
    audit(db, sid, now, "info", "Databron en risicolimieten opgeslagen.")


def save_strategies(db, sid, state, now, data):
    previous = prefs(state)
    provider = state["settings"]["provider"]
    if previous["scalpMinutes"] != data["scalpMinutes"] or not data["scalpEnabled"]:
        cancel_discussion(db, sid, provider, coordinator.pending(db, sid, provider), now,
                          "Kortetermijninstellingen gewijzigd; het oude advies vervalt.")
    state["strategies"] = dict(data)
    decisions = state.setdefault("bookDecisions", {})
    for book in ("scalp", "swing"):
        if previous[book+"Enabled"] != data[book+"Enabled"]:
            decisions[book] = dict(action="WAIT", reason="Wacht op een nieuwe gesloten candle." if data[book+"Enabled"]
                                   else "Uitgeschakeld voor nieuwe instappen; bestaande posities blijven beschermd.",
                                   time=now, quoteTime=None, model=None)
    if not previous["swingEnabled"] and data["swingEnabled"]:
        state.pop("lastSwingHour", None)
    audit(db, sid, now, "info", "Strategieën en looptijd opgeslagen. Open posities houden hun afgesproken looptijd.")


def combined_validation(scalp, swing):
    score = min(scalp["score"], swing["score"])
    status = ("none" if all(v["status"] == "none" for v in (scalp, swing)) else
              "insufficient" if any(v["status"] in ("none", "insufficient") for v in (scalp, swing)) else
              "rigorous" if score == 100 else "strong" if score >= 90 else "measured")
    return dict(score=score, status=status)


def validation_reports(db, sid, state, market, now):
    settings = state["settings"]
    remote = getattr(market, "agent_remote", False)
    forecast_profile = learning.policy(settings, remote)
    arm = learning.load_model(db, sid, forecast_profile)["active"]
    horizon = prefs(state)["scalpMinutes"]
    scalp = snapshot(db, sid, settings, now, remote, arm, forecast_profile, horizon=horizon)
    swing = snapshot(db, sid, settings, now, book="swing", horizon=SWING_MINUTES)
    return scalp, swing, arm, horizon


def validation_history_rows(db, sid, state, market, now):
    scalp, swing, arm, horizon = validation_reports(db, sid, state, market, now)
    remote = getattr(market, "agent_remote", False)
    scalp_profile = validation_profile(state["settings"], remote, arm, "scalp", horizon)
    swing_profile = validation_profile(state["settings"], book="swing", horizon=SWING_MINUTES)
    combined = combined_validation(scalp, swing)
    return (
        ("scalp", scalp, scalp_profile, f"Kortetermijn · {horizon} min · {arm}"),
        ("swing", swing, swing_profile, "Swing · H1/H4"),
        ("combined", {
            "score": combined["score"],
            "evidenceCeiling": min(scalp["evidenceCeiling"], swing["evidenceCeiling"]),
            "sampleSize": scalp["sampleSize"] + swing["sampleSize"],
            "directionalSamples": scalp["directionalSamples"] + swing["directionalSamples"],
            "excluded": scalp["excluded"] + swing["excluded"],
            "status": combined["status"],
            "updatedAt": max((t for t in (scalp["updatedAt"], swing["updatedAt"]) if t is not None), default=None),
        }, f"{scalp_profile}:{swing_profile}", "Samen · laagste boekscore"),
    )


def dashboard(db, sid, state, market, now, bridge_identity):
    positions = get_positions(db, sid)
    provider = state["settings"]["provider"]
    quote = market.execution_quote(provider, now)
    mark = quote["price"]
    for t in positions:
        t["pnl"] = pnl(t, mark) if mark is not None else None
    equity = state["balance"] + sum(t["pnl"] or 0 for t in positions)
    daily = round(equity - state["anchor"], 2) if state["day"] == now // 86400 else 0
    bridge = bridge_record(db, bridge_identity)
    status = bridge_check(bridge, bridge_identity, now)["status"]
    verified = status == "verified_demo"
    connections = dict(feed=quote["status"], mt5=status,
        mt5Balance=bridge["balance"] if verified else None, mt5Currency=bridge["currency"] if verified else None,
        marketKeyConfigured=bool(market.key), bridgeConfigured=bool(bridge_identity), liveTrading=False)
    history = [json.loads(r["data"]) for r in db.execute(
        "SELECT data FROM trades WHERE account=? AND closed IS NOT NULL ORDER BY closed DESC, rowid DESC LIMIT 200", (sid,))]
    logs = [dict(r) for r in db.execute(
        "SELECT id,time,level,message FROM audit WHERE account=? ORDER BY id DESC LIMIT 200", (sid,))]
    results = list(db.execute("""
        SELECT pnl, json_extract(data,'$.side') AS side, json_extract(data,'$.regime') AS regime,
               json_extract(data,'$.feed') AS feed, coalesce(json_extract(data,'$.strategy'),'scalp') AS book
        FROM trades WHERE account=? AND closed IS NOT NULL ORDER BY closed, rowid
        """, (sid,)))
    selected_feed = [r for r in results if r["feed"] == provider]
    performance = {
        key: metrics([r["pnl"] for r in selected_feed if r[field] == key])
        for field, keys in (("side", ("long", "short")), ("regime", ("bullish", "bearish", "sideways")))
        for key in keys
    }
    performance["unknownTrades"] = sum(r["regime"] is None for r in selected_feed)
    engine = "stopped" if state["engine"] == "risk_locked" and not positions and state["day"] != now // 86400 else state["engine"]
    preferences = prefs(state)
    scalp, swing, _, _ = validation_reports(db, sid, state, market, now)
    strategy_books = dict(prefs=preferences, combined=combined_validation(scalp, swing))
    for name, validation in (("scalp", scalp), ("swing", swing)):
        open_book = [t for t in positions if book_of(t) == name]
        closed_book = [json.loads(r["data"]) for r in db.execute("""
            SELECT data FROM trades WHERE account=? AND closed IS NOT NULL
                AND coalesce(json_extract(data,'$.strategy'),'scalp')=?
            ORDER BY closed DESC,rowid DESC LIMIT 200
            """, (sid, name))]
        strategy_books[name] = dict(decision=state.get("bookDecisions", {}).get(name), validation=validation,
                                    metrics=metrics([r["pnl"] for r in results if r["book"] == name]),
                                    positions=open_book, history=closed_book,
                                    unrealizedPnl=round(sum(t["pnl"] or 0 for t in open_book), 2) if quote["fresh"] or not open_book else None)
    return dict(mode="paper", engine=engine, balance=state["balance"], equity=round(equity, 2),
                dailyPnl=daily, openRisk=round(risk_at_stop(positions, mark), 2), dayLoss=max(0, -daily),
                connections=connections, settings=state["settings"], positions=positions, history=history,
                logs=logs, metrics=metrics([r["pnl"] for r in results]), performance=performance,
                validation=scalp, strategyBooks=strategy_books,
                decision=state.get("decision"), advisorRound=latest_round(db, sid, provider) if provider != SIM else None,
                marketQuote=dict(price=mark, time=quote["time"],
                    error=None if provider == SIM else market.connection_check(now, provider)["error"]))
