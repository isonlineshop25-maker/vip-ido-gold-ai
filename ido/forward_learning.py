"""Prospective paper-entry research. Nothing in this module can send an order."""

import json
import math
from collections import defaultdict
from statistics import mean, stdev

from advisors import assess, causal_bars
from books import scalp_filter
from candle_features import describe
from costs import COSTS
from market import SIM
from strategy import BASELINE, START_BALANCE, signal
from validation import profile as validation_profile

DAY = 86400
WEEK = 7 * DAY
VERSION = "forward-candles-1"
ARMS = {"basis": BASELINE, "pullback": (3, 10), "structuur": (8, 20)}
MIN_TRADES = 120
MIN_PER_BLOCK = 30
BLOCKS = 3
GRACE = 300


def init_learning(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS forward_calls (
        account TEXT NOT NULL, profile TEXT NOT NULL, provider TEXT NOT NULL, target INTEGER NOT NULL,
        made INTEGER NOT NULL, input TEXT NOT NULL, forecasts TEXT NOT NULL, evaluated INTEGER, outcome TEXT,
        PRIMARY KEY(account,profile,target)
      );
      CREATE INDEX IF NOT EXISTS forward_pending ON forward_calls(account, provider, target) WHERE evaluated IS NULL;
      CREATE TABLE IF NOT EXISTS forward_models (
        account TEXT NOT NULL, profile TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(account,profile)
      );
      CREATE TABLE IF NOT EXISTS candle_decisions (
        id INTEGER PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL, time INTEGER NOT NULL,
        action TEXT NOT NULL, trade TEXT, reason TEXT NOT NULL, candle TEXT, target INTEGER
      );
      CREATE INDEX IF NOT EXISTS candle_decision_account ON candle_decisions(account,provider,id);
      CREATE INDEX IF NOT EXISTS candle_decision_trade
          ON candle_decisions(account,trade) WHERE trade IS NOT NULL;
    """)


def policy(settings, remote):
    return VERSION + "/" + validation_profile(settings, remote)


def model_label(arm):
    a, b = ARMS[arm]
    return f"{ {'basis':'Basis','pullback':'Pullback','structuur':'Structuur'}[arm] } · SMA {a}/{b} + RSI"


def prediction(bars, features, arm):
    name, rsi, fast, slow = signal([{"close": b.close} for b in bars], ARMS[arm])
    direction = {"long": "BUY", "short": "SELL"}.get(name, "WAIT")
    if direction != "WAIT" and arm != "basis":
        side = "up" if direction == "BUY" else "down"
        oppose = "down" if side == "up" else "up"
        if arm == "pullback" and (features["doji"] or features["pinBar"] == oppose or features["engulfing"] == oppose):
            direction = "WAIT"
        if arm == "structuur" and (features["trend"] != side or not features["atr"] or
                abs(features["momentum"] or 0) < features["atr"] or features["body"] > 1.8 * features["atr"]):
            direction = "WAIT"
    return direction, rsi, fast, slow


def preview(arm, quote, now):
    bars = causal_bars(quote["bars"], now, quote["time"], quote["fresh"])
    direction, rsi, fast, slow = prediction(bars, describe(bars), arm)
    if bars and scalp_filter(quote["bars"], min(now, quote["time"]), quote["price"], direction):
        direction = "WAIT"
    return ({"BUY":"long","SELL":"short","WAIT":"neutral"}[direction] if len(bars) >= max(16, ARMS[arm][1]) else "unavailable",
            rsi, fast, slow, model_label(arm))


def save_call(db, sid, key, provider, bars, quote, now, risk, settings, current):
    if provider == SIM or not quote["fresh"] or len(bars) < 16:
        return None
    target = (now // 60 + 1) * 60  # first complete candle whose OPEN is after publication
    features = describe(bars)
    distance = max(0.30, quote["price"] * 0.001)
    cost = sum(COSTS) if COSTS else None
    forecasts = {}
    for arm in ARMS:
        direction, *_ = prediction(bars, features, arm)
        if scalp_filter(quote.get("bars") or [], min(now, quote["time"]), quote["price"], direction):
            direction = "WAIT"
        approved = (assess(bars, direction, risk, now, quote["time"])["recommendation"]
                    if cost is not None and cost / distance <= .30 else "WAIT")
        forecasts[arm] = dict(direction=direction, shadow=approved)
    context = dict(features=features, quoteTime=quote["time"], current=current, distance=distance, cost=cost,
                   riskPct=settings["riskPct"], dailyLossPct=settings["dailyLossPct"])
    db.execute("""INSERT OR IGNORE INTO forward_calls(account,profile,provider,target,made,input,forecasts)
                  VALUES(?,?,?,?,?,?,?)""", (sid, key, provider, target, now, json.dumps(context), json.dumps(forecasts)))
    return target


def settle(db, sid, provider, quote, now, canceled=False):
    if provider == SIM:
        return
    bars = {b["time"]: b for b in quote["bars"]} if quote["fresh"] else {}
    visible = min(now, quote["time"] or 0)
    for row in db.execute("""SELECT profile,target,input FROM forward_calls
                           WHERE account=? AND provider=? AND evaluated IS NULL AND target<=?""",
                          (sid, provider, now if canceled else now - 60)):
        target = row["target"]
        bar = bars.get(target) if target+60 <= visible and now <= target+60+GRACE else None
        if bar is not None:
            outcome = dict(candle=bar)
        elif canceled or now >= target + 60 + GRACE:
            outcome = dict(candle=None, reason="Koersbron gewijzigd" if canceled else "Geen tijdige, gesloten doelcandle")
        else:
            continue
        db.execute("UPDATE forward_calls SET evaluated=?,outcome=? WHERE account=? AND profile=? AND target=? AND evaluated IS NULL",
                   (now, json.dumps(outcome), sid, row["profile"], target))
    if canceled:
        db.execute("""UPDATE forward_calls SET evaluated=?, outcome=? WHERE account=? AND provider=? AND evaluated IS NULL""",
                   (now, json.dumps({"candle": None, "reason": "Koersbron gewijzigd"}), sid, provider))


def pending(db, sid):
    return db.execute("SELECT 1 FROM forward_calls WHERE account=? AND evaluated IS NULL LIMIT 1", (sid,)).fetchone() is not None


def rows_between(db, sid, key, start, end):
    for row in db.execute("""SELECT made,target,input,forecasts,outcome FROM forward_calls
                            WHERE account=? AND profile=? AND target>=? AND target<? AND evaluated IS NOT NULL
                            ORDER BY target""", (sid, key, start, end)):
        yield row["made"], row["target"], json.loads(row["input"]), json.loads(row["forecasts"]), json.loads(row["outcome"])["candle"]


def measure(rows, arm):
    equity = peak = anchor = START_BALANCE
    day = None
    maxdd = maxday = 0
    scored = hits = missed = breaches = 0
    outcomes, days = [], set()
    by_candle = {}
    first = end = None
    for made, target, context, forecasts, b in rows:
        if not b:
            missed += 1
            continue
        first = target if first is None else first
        end = target + 60
        p = forecasts[arm]
        change = b["close"] - b["open"]
        if p["direction"] != "WAIT" and change != 0:
            scored += 1
            hits += (change > 0) == (p["direction"] == "BUY")
        result = 0.0
        if day != target // DAY:
            day, anchor = target // DAY, equity
        if p["shadow"] != "WAIT" and context["cost"] is not None and equity > 0:
            distance, cost = context["distance"], context["cost"]
            daily_limit = anchor * context["dailyLossPct"] / 100
            equity_risk = min(equity * context["riskPct"] / 100, equity - (anchor - daily_limit))
            qty = math.floor(max(0, min(equity_risk / (distance + cost), equity * .25 / b["open"])) * 1000) / 1000
            if qty >= .001:
                buy = p["shadow"] == "BUY"
                adverse = b["open"] - b["low"] if buy else b["high"] - b["open"]
                # If stop and target could both hit, take the stop. Gaps/liquidity aren't a fill guarantee.
                r = -distance if adverse >= distance else min(distance * 1.6, change if buy else -change)
                result = (r - cost) * qty
                # Drawdown includes the adverse intraminute mark, not just closes.
                lowest = equity - (min(adverse, distance) + cost) * qty
                maxdd = max(maxdd, (peak-lowest) / peak * 100)
                maxday = max(maxday, (anchor-lowest) / anchor * 100)
                breaches += lowest < anchor - daily_limit - 0.01
                equity += result
                peak = max(equity, peak)
                outcomes.append(result)
                days.add(day)
        by_candle[target] = result
    wins, losses = [v for v in outcomes if v > 0], [-v for v in outcomes if v < 0]
    return dict(key=arm, name=model_label(arm), evaluatedSignals=scored, evaluatedTrades=len(outcomes),
                directionalAccuracy=round(100 * hits / scored, 2) if scored else None,
                winRate=round(100 * len(wins) / len(outcomes), 2) if outcomes else None,
                expectancy=round(mean(outcomes), 4) if outcomes else None,
                profitFactor=round(sum(wins)/sum(losses), 3) if losses else None,
                maxDrawdown=round(maxdd, 3) if outcomes else None, maxDailyLoss=maxday, breaches=breaches,
                netPnl=sum(outcomes), periodFrom=first, periodTo=end, missing=missed, days=len(days), paired=by_candle)


def public(stat):
    return {k:v for k,v in stat.items() if k not in ("paired", "breaches", "maxDailyLoss", "days", "netPnl")}


def load_model(db, sid, key):
    row = db.execute("SELECT state FROM forward_models WHERE account=? AND profile=?", (sid, key)).fetchone()
    return json.loads(row["state"]) if row else dict(active="basis", candidate=None, cycleFrom=None,
                 lastBlock=None, reason="Nog geen vooraf vastgelegde validatie.", state="collecting", promotedAt=None)


def assess_gate(rows, champion, candidate, start, settings):
    old, new = measure(rows, champion), measure(rows, candidate)
    blocks = [[r for r in rows if start+i*WEEK <= r[1] < start+(i+1)*WEEK] for i in range(BLOCKS)]
    pairs = [(measure(b, champion), measure(b, candidate)) for b in blocks]
    if (min(old["evaluatedTrades"], new["evaluatedTrades"]) < MIN_TRADES or min(old["days"], new["days"]) < 12 or
            any(min(a["evaluatedTrades"], b["evaluatedTrades"]) < MIN_PER_BLOCK for a,b in pairs)):
        return "insufficient", "Niet genoeg ongeziene transacties in alle drie testblokken."
    if new["missing"] / max(1, len(rows)) > .05:
        return "insufficient", "Meer dan 5% van de doelcandles ontbreekt."
    diff_by_day = defaultdict(float)
    for t, value in new["paired"].items():
        diff_by_day[t//DAY] += value - old["paired"][t]
    diffs = list(diff_by_day.values())
    lower = mean(diffs) - 2.5 * stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else -math.inf
    if (lower <= 0 or any(b["netPnl"] <= max(0, a["netPnl"]) for a,b in pairs)
            or (new["expectancy"] or 0) <= max(0, old["expectancy"] or 0)):
        return "overfit", "Trainingsvoordeel hield niet stand in ongeziene blokken of de dagelijkse voorsprong is te onzeker."
    if (new["breaches"] or (new["profitFactor"] or 0) < 1.25 or new["maxDailyLoss"] > settings["dailyLossPct"]
            or (new["maxDrawdown"] or 0) > min(3, (old["maxDrawdown"] or 0) + .25)):
        return "risk_rejected", "Kandidaat passeerde de verlies-, drawdown- of kostencontrole niet."
    return "promoted", "Beter na kosten in alle drie ongeziene blokken, met voldoende steekproef en binnen de risicogrenzen."


def advance(db, sid, key, settings, now, remote):
    m = load_model(db, sid, key)
    # Close each UTC-aligned week after the last target candle's settlement grace.
    block = (now - 60 - GRACE) // WEEK * WEEK
    if remote or COSTS is None or m["lastBlock"] == block:
        return m
    m["lastBlock"] = block
    if m["candidate"] and block >= m["cycleFrom"] + BLOCKS * WEEK:
        start = m["cycleFrom"]
        rows = [r for r in rows_between(db, sid, key, start, start+BLOCKS*WEEK) if r[0] >= start]
        m["state"], m["reason"] = assess_gate(rows, m["active"], m["candidate"], start, settings)
        m["lastTest"] = dict(fromTime=start, toTime=start+BLOCKS*WEEK, incumbent=m["active"],
                             challenger=m["candidate"], results=[public(measure(rows, k)) for k in (m["active"], m["candidate"])])
        if m["state"] == "promoted":
            m["active"], m["promotedAt"] = m["candidate"], now
        m.update(candidate=None, cycleFrom=None)
    elif not m["candidate"]:
        training = list(rows_between(db, sid, key, block-WEEK, block))
        old = measure(training, m["active"])
        options = [measure(training, k) for k in ARMS if k != m["active"]]
        best = max(options, key=lambda x:x["netPnl"])
        if (old["evaluatedTrades"] >= MIN_PER_BLOCK and best["evaluatedTrades"] >= MIN_PER_BLOCK and
                best["netPnl"] > max(0, old["netPnl"]) and (best["expectancy"] or 0) > max(0, old["expectancy"] or 0)):
            # Freeze before the first prediction in the next block. Old observations can never qualify the challenger.
            m.update(candidate=best["key"], cycleFrom=block+WEEK, state="testing",
                     reason="Vastgezet op oudere data; alleen komende, ongeziene blokken tellen mee voor promotie.")
    db.execute("INSERT INTO forward_models VALUES(?,?,?) ON CONFLICT(account,profile) DO UPDATE SET state=excluded.state",
               (sid, key, json.dumps(m)))
    return m


def audit_decision(db, sid, provider, now, action, reason, features, trade=None, target=None):
    if provider != SIM:
        db.execute("""INSERT INTO candle_decisions(account,provider,time,action,trade,reason,candle,target)
                      VALUES(?,?,?,?,?,?,?,?)""", (sid, provider, now, action, trade, reason, json.dumps(features), target))


def direction_of(bar):
    return ("up" if bar["close"] > bar["open"] else "down" if bar["close"] < bar["open"] else "flat") if bar else None


def report(db, sid, settings, now, remote):
    provider = settings["provider"]
    key = policy(settings, remote)
    m = load_model(db, sid, key)
    rows = list(rows_between(db, sid, key, now-30*DAY, now)) if provider != SIM else []
    active_rows = [r for r in rows if r[2]["current"] == m["active"] and (m["promotedAt"] is None or r[0] >= m["promotedAt"])]
    scope = m["cycleFrom"]
    comparison_rows = [r for r in rows if r[0] >= scope and r[1] < scope+BLOCKS*WEEK] if scope is not None else rows
    latest = []
    for row in db.execute("""SELECT d.id,d.time,d.action,d.reason,d.candle,d.target,t.data AS tradeData FROM candle_decisions d
                             LEFT JOIN trades t ON t.id=d.trade AND t.account=d.account
                             WHERE d.account=? AND d.provider=? ORDER BY d.id DESC LIMIT 15""", (sid, provider)):
        observed = db.execute("SELECT outcome FROM forward_calls WHERE account=? AND provider=? AND target=? AND made<=? ORDER BY made DESC LIMIT 1",
                              (sid, provider, row["target"], row["time"])).fetchone() if row["target"] else None
        observed = json.loads(observed["outcome"]) if observed and observed["outcome"] else None
        b = observed.get("candle") if observed else None
        movement = direction_of(b)
        trade = json.loads(row["tradeData"]) if row["tradeData"] else None
        latest.append(dict(id=row["id"], time=row["time"], action=row["action"], reason=row["reason"],
                           features=json.loads(row["candle"]), target=row["target"], observed=movement,
                           missing=observed.get("reason") if observed else None,
                           paperPnl=trade["pnl"] if trade else None, paperClosed=bool(trade and trade["closedAt"] is not None)))
    forecasts = []
    for row in db.execute("""SELECT made,target,input,forecasts,outcome FROM forward_calls
                            WHERE account=? AND profile=? ORDER BY target DESC LIMIT 12""", (sid, key)):
        outcome = json.loads(row["outcome"]) if row["outcome"] else None
        forecasts.append(dict(made=row["made"], target=row["target"], features=json.loads(row["input"])["features"],
                              market=(direction_of(outcome["candle"]) or "missing") if outcome else None,
                              calls={k:v["direction"] for k,v in json.loads(row["forecasts"]).items()}))
    return dict(active=m["active"], activeName=model_label(m["active"]), challenger=m["candidate"], periodFrom=scope,
                periodTo=scope+BLOCKS*WEEK if scope is not None else None, promotedAt=m["promotedAt"],
                state="disabled" if provider==SIM or remote or COSTS is None else m["state"],
                reason=("Automatische promotie staat uit in synthetische modus, zonder kostenmodel of met de externe adviseur."
                        if provider==SIM or remote or COSTS is None else m["reason"]),
                closedBlocks=max(0,min(BLOCKS,(now-60-GRACE-scope)//WEEK)) if scope is not None else 0,
                qualifying=MIN_TRADES, comparisons=[public(measure(comparison_rows,k)) for k in ARMS],
                activeMetrics=public(measure(active_rows,m["active"])), lastTest=m.get("lastTest"),
                pending=db.execute("SELECT count(*) FROM forward_calls WHERE account=? AND profile=? AND evaluated IS NULL", (sid,key)).fetchone()[0],
                decisions=latest, forecasts=forecasts)
