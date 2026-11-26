"""Prospective, cost-adjusted validation of recorded real-feed paper trades."""

import hashlib
import json
import math
from collections import defaultdict

from advisors import POLICY
from books import HOUR, book_of, direction, rollup
from costs import COSTS
from market import SIM
from strategy import STRATEGY, market_regime

DAY = 86400
WEEK = 7 * DAY
WINDOW_DAYS = 365


def init_validation(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS validation_trials (
            trade TEXT PRIMARY KEY, account TEXT NOT NULL, profile TEXT NOT NULL,
            opened INTEGER NOT NULL, entry TEXT NOT NULL, closed INTEGER, result TEXT
        );
        CREATE INDEX IF NOT EXISTS validation_profile
            ON validation_trials(account, profile, opened) WHERE closed IS NOT NULL;
        CREATE TABLE IF NOT EXISTS validation_history (
            id INTEGER PRIMARY KEY, account TEXT NOT NULL, book TEXT NOT NULL,
            series INTEGER NOT NULL, profile TEXT NOT NULL, strategy TEXT NOT NULL,
            observed INTEGER NOT NULL, score INTEGER NOT NULL, evidence_ceiling INTEGER NOT NULL,
            sample_size INTEGER NOT NULL, directional_samples INTEGER NOT NULL, excluded INTEGER NOT NULL,
            status TEXT NOT NULL, updated_at INTEGER, cause TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS validation_history_account
            ON validation_history(account, book, id);
    """)


def record_history(db, sid, book, report, profile_key, strategy, now):
    """Persist only materially different, actually calculated score snapshots."""
    previous = db.execute("""
        SELECT series, profile, score, evidence_ceiling, sample_size, directional_samples,
               excluded, status, updated_at
        FROM validation_history WHERE account=? AND book=? ORDER BY id DESC LIMIT 1
        """, (sid, book)).fetchone()
    values = (report["score"], report["evidenceCeiling"], report["sampleSize"],
              report["directionalSamples"], report["excluded"], report["status"], report["updatedAt"])
    if previous and values == tuple(previous[key] for key in
                                    ("score", "evidence_ceiling", "sample_size", "directional_samples",
                                     "excluded", "status", "updated_at")) and previous["profile"] == profile_key:
        return False

    series = 1 if previous is None else previous["series"]
    if previous is None:
        cause = "initial"
    elif previous["profile"] != profile_key:
        series += 1
        cause = "strategy_changed"
    elif (report["sampleSize"] < previous["sample_size"] or
          report["directionalSamples"] < previous["directional_samples"] or
          report["excluded"] < previous["excluded"] or
          (report.get("periodTo") is not None and now - report["periodTo"] > 30 * DAY and
           report["score"] < previous["score"] and
           (previous["updated_at"] is None or now - previous["updated_at"] <= 30 * DAY))):
        cause = "evidence_expired"
    elif (report["sampleSize"] > previous["sample_size"] or
          report["directionalSamples"] > previous["directional_samples"] or
          report["excluded"] > previous["excluded"] or
          (report["updatedAt"] is not None and report["updatedAt"] != previous["updated_at"])):
        cause = "new_evidence"
    else:
        cause = "score_recalculated"

    db.execute("""
        INSERT INTO validation_history
            (account,book,series,profile,strategy,observed,score,evidence_ceiling,
             sample_size,directional_samples,excluded,status,updated_at,cause)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (sid, book, series, profile_key, strategy, now, *values, cause))
    return True


def history(db, sid, book, limit=500):
    """Return observed score snapshots only; no interpolation or generated values."""
    rows = db.execute("""
        SELECT series,strategy,observed,score,evidence_ceiling AS evidenceCeiling,
               sample_size AS sampleSize,directional_samples AS directionalSamples,
               excluded,status,updated_at AS updatedAt,cause
        FROM validation_history WHERE account=? AND book=? ORDER BY id DESC LIMIT ?
        """, (sid, book, limit))
    return dict(book=book, points=[dict(row) for row in reversed(list(rows))])


def profile(settings, remote=False, strategy_arm="basis", book="scalp", horizon=15):
    # Do not pool different rules, risk profiles or cost assumptions.
    payload = [STRATEGY, "dual-books-v1", book, horizon, POLICY, strategy_arm, "remote-review" if remote else "local-review", settings["maxPositions"],
               float(settings["riskPct"]), float(settings["dailyLossPct"]), settings.get("provider"), COSTS]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def classify(bars, now):
    """Use only completed, contiguous minute bars available at entry."""
    bars = bars[-16:]
    if len(bars) < 16 or not 60 <= now - bars[-1]["time"] < 180:
        return None
    if any(b["time"] - a["time"] != 60 for a, b in zip(bars, bars[1:])):
        return None
    changes = [math.log(b["close"] / a["close"]) for a, b in zip(bars, bars[1:])]
    regime = market_regime(bars)
    if regime == "sideways":
        return None  # the current entry policy abstains; no sideways fills qualify
    volatile = sum(abs(x) for x in changes) / len(changes) >= 0.00035
    return regime + ("_high" if volatile else "_low")


def record_entry(db, sid, settings, trade, bars, quote, equity, remote=False, strategy_arm="basis", swing_volatility=None):
    if trade["feed"] == SIM:
        return
    book = book_of(trade)
    regime = (f"{trade['regime']}_{swing_volatility}" if book == "swing" and swing_volatility
              else classify(bars, trade["openedAt"]) if book == "scalp" else None)
    entry = dict(price=trade["entry"], stop=trade["stop"], quantity=trade["quantity"],
                 side=trade["side"], equity=equity, quoteTime=quote["time"],
                 fresh=quote["fresh"], regime=regime, costs=COSTS,
                 signalBars=[(b["time"], b["close"]) for b in bars[-16:]])
    if book == "swing":
        cutoff = min(trade["openedAt"], quote["time"])
        h4 = rollup(bars, HOUR, 4 * HOUR, cutoff)
        entry["evidence"] = dict(asOf=cutoff, h1=bars[-8:], h4=h4[-4:], h4Direction=direction(h4[-4:], .001))
    db.execute("INSERT INTO validation_trials(trade,account,profile,opened,entry) VALUES(?,?,?,?,?)",
               (trade["id"], sid, profile(settings, remote, strategy_arm, book, trade.get("holdMinutes", 15)),
                trade["openedAt"], json.dumps(entry)))


def record_exit(db, sid, trade, quote):
    if trade["feed"] == SIM:
        return
    row = db.execute("SELECT entry,opened FROM validation_trials WHERE trade=? AND account=? AND closed IS NULL",
                     (trade["id"], sid)).fetchone()
    if not row:
        # Never retroactively certify trades opened before this evaluator existed.
        return
    entry = json.loads(row["entry"])
    quote_time = quote["time"]
    result = dict(reason=None, quoteTime=quote_time, price=quote["price"])
    if not entry["costs"]:
        result["reason"] = "missing_costs"
    elif not entry["regime"]:
        result["reason"] = "incomplete_entry_history"
    elif not entry["fresh"] or not quote["fresh"] or quote_time is None or entry["quoteTime"] is None or quote_time <= entry["quoteTime"]:
        result["reason"] = "unverified_exit"
    else:
        # Recalculate independently from frozen prices, not engine-reported P/L.
        round_trip = sum(entry["costs"])
        sign = 1 if entry["side"] == "long" else -1
        risk = (abs(entry["price"] - entry["stop"]) + round_trip) * entry["quantity"]
        gross = sign * (quote["price"] - entry["price"]) * entry["quantity"]
        net = gross - round_trip * entry["quantity"]
        result.update(net=net, r=net / risk, regime=entry["regime"], equity=entry["equity"],
                      side=entry["side"], beyondStop=net < -1.25 * risk)
    db.execute("UPDATE validation_trials SET closed=?,result=? WHERE trade=? AND account=? AND closed IS NULL",
               (trade["closedAt"], json.dumps(result), trade["id"], sid))


def clamp(value):
    return min(1, max(0, value))


def directions(db, sid, paper_profile, forecast_profile, provider, arm, cutoff, now, eligible):
    """Match only audited paper entries to their pre-published, unseen target candle."""
    if not forecast_profile or not eligible:
        return {}
    scored = {}
    for row in db.execute("""
        SELECT p.trade, p.closed, d.action, f.target, f.evaluated, f.forecasts, f.input, f.outcome
        FROM validation_trials p
        JOIN candle_decisions d ON d.account=p.account AND d.trade=p.trade AND d.time=p.opened
        JOIN forward_calls f ON f.account=p.account AND f.profile=? AND f.target=d.target AND f.provider=d.provider
        WHERE p.account=? AND p.profile=? AND p.opened>=? AND p.closed BETWEEN p.opened AND ?
          AND d.provider=? AND d.action IN ('BUY','SELL')
          AND f.made<=p.opened AND p.opened<f.target AND f.evaluated BETWEEN f.target+60 AND ?
        ORDER BY p.closed, p.rowid
        """, (forecast_profile, sid, paper_profile, cutoff, now, provider, now)):
        if row["trade"] not in eligible or row["target"] in scored:
            continue
        predicted = json.loads(row["forecasts"]).get(arm, {}).get("direction")
        bar = json.loads(row["outcome"]).get("candle")
        if (predicted != row["action"] or json.loads(row["input"]).get("current") != arm or
                not bar or bar["time"] != row["target"] or bar["open"] == bar["close"]):
            continue
        scored[row["target"]] = (max(row["closed"], row["evaluated"]),
                                 (bar["close"] > bar["open"]) == (predicted == "BUY"))
    return scored


def snapshot(db, sid, settings, now, remote=False, strategy_arm="basis", forecast_profile=None, book="scalp", horizon=15):
    spread, fees, slip = COSTS or (None, None, None)
    report = dict(score=0, evidenceCeiling=0, lastStep=0, status="none", sampleSize=0, excluded=0, periodFrom=None,
                  periodTo=None, updatedAt=None, elapsedDays=0, evaluatedBlocks=0,
                  positiveBlocks=0, testedRegimes=0, positiveRegimes=0, expectancyR=None,
                  expectancyUsd=None, winRate=None, profitFactor=None, drawdownPct=None, worstDailyLossPct=None,
                  directionalSamples=0, directionalAccuracy=None,
                  longSampleSize=0, shortSampleSize=0, longExpectancyR=None, shortExpectancyR=None,
                  costModelConfigured=COSTS is not None, spreadUsdPerOz=spread, feesUsdPerOz=fees, slippageUsdPerOz=slip)
    cutoff = (now // DAY - WINDOW_DAYS) * DAY
    paper_profile = profile(settings, remote, strategy_arm, book, horizon)
    attempts = db.execute("""
        SELECT trade, opened, closed, result FROM validation_trials
        WHERE account=? AND profile=? AND opened>=? AND closed BETWEEN opened AND ?
        ORDER BY closed, rowid
        """, (sid, paper_profile, cutoff, now))
    evidence, eligible, exclusions = [], set(), []
    for row in attempts:
        trade, opened, closed, result = row
        report["updatedAt"] = closed
        assessment = json.loads(result)
        if assessment["reason"] is None:
            evidence.append((opened, closed, assessment))
            eligible.add(trade)
        else:
            report["excluded"] += 1
            exclusions.append(closed)
    if not evidence:
        return report

    if book == "swing":
        scored = {}
        for row in db.execute("""
            SELECT f.trade, f.target, f.hit, max(p.closed,f.evaluated) AS ready
            FROM swing_calls f JOIN validation_trials p ON p.trade=f.trade
            WHERE f.account=? AND f.provider=? AND p.profile=? AND p.opened>=? AND f.made=p.opened
                AND f.made<f.target AND f.hit IS NOT NULL AND f.evaluated BETWEEN f.target+3600 AND ?
            ORDER BY ready
            """, (sid, settings["provider"], paper_profile, cutoff, now)):
            if row["trade"] in eligible:
                scored.setdefault(row["target"], (row["ready"], bool(row["hit"])))
    else:
        scored = directions(db, sid, paper_profile, forecast_profile, settings["provider"],
                            strategy_arm, cutoff, now, eligible)
    if scored:
        report["updatedAt"] = max(report["updatedAt"], max(at for at, _ in scored.values()))
    return score_trials(report, evidence, settings, now, scored.values(), exclusions)


class Evidence:
    def __init__(self, equity):
        self.equity = self.peak = self.anchor = max(1, equity)
        self.first, self.last, self.day = None, None, None
        self.drawdown = self.day_loss = self.gains = self.losses = self.total_r = 0
        self.n = self.wins = self.losers = self.excluded = self.hits = self.directional_n = 0
        self.stop_breached = False
        self.blocks = defaultdict(lambda: [0, 0.0])
        self.regimes = defaultdict(lambda: [0, 0.0])
        self.sides = {"long": [0, 0.0], "short": [0, 0.0]}

    def add(self, opened, closed, result):
        self.first = opened if self.first is None else min(self.first, opened)
        self.last = closed
        r, net = result["r"], result["net"]
        self.n += 1
        self.wins += net > 0
        self.losers += net < 0
        self.gains += max(net, 0)
        self.losses += max(-net, 0)
        self.total_r += r
        self.stop_breached |= result["beyondStop"]
        for bucket in (self.blocks[opened // WEEK], self.regimes[result["regime"]], self.sides[result["side"]]):
            bucket[0] += 1
            bucket[1] += r
        if closed // DAY != self.day:
            self.day, self.anchor = closed // DAY, max(1, self.equity)
        self.equity += net
        self.peak = max(self.peak, self.equity)
        self.drawdown = max(self.drawdown, (self.peak - self.equity) / self.peak * 100)
        self.day_loss = max(self.day_loss, (self.anchor - self.equity) / self.anchor * 100)

    def assess(self, settings, now, report=None):
        if not self.n:
            return 0, False
        n, directional_n = self.n, self.directional_n
        days = (self.last - self.first) // DAY
        expectancy = self.total_r / n
        pf = self.gains / self.losses if self.losses else None
        accuracy = self.hits / directional_n if directional_n else 0
        weekly = [total / size for size, total in self.blocks.values() if size >= 5]
        qualified = [total / size for size, total in self.regimes.values() if size >= 5]
        positive_weeks = sum(x > 0 for x in weekly)
        positive_regimes = sum(x > 0 for x in qualified)
        long_n, long_net = self.sides["long"]
        short_n, short_net = self.sides["short"]
        long_r = long_net / long_n if long_n else None
        short_r = short_net / short_n if short_n else None
        if report is not None:
            report.update(sampleSize=n, periodFrom=self.first, periodTo=self.last, elapsedDays=days,
                          evaluatedBlocks=len(weekly), positiveBlocks=positive_weeks,
                          testedRegimes=len(qualified), positiveRegimes=positive_regimes,
                          expectancyR=round(expectancy, 3), expectancyUsd=round((self.gains - self.losses) / n, 2),
                          winRate=round(100 * self.wins / n, 2),
                          directionalSamples=directional_n,
                          longSampleSize=long_n, shortSampleSize=short_n,
                          longExpectancyR=round(long_r, 3) if long_r is not None else None,
                          shortExpectancyR=round(short_r, 3) if short_r is not None else None,
                          directionalAccuracy=round(100 * accuracy, 2) if directional_n else None,
                          profitFactor=round(pf, 2) if pf is not None else None,
                          drawdownPct=round(self.drawdown, 2), worstDailyLossPct=round(self.day_loss, 2))
        if n < 30 or min(long_n, short_n) < 8 or days < 14 or len(weekly) < 3 or directional_n < 20:
            return 0, False

        positive_share = positive_weeks / len(weekly)
        count = len(weekly)
        average = sum(weekly) / count
        standard_error = math.sqrt(sum((week - average) ** 2 for week in weekly) / (count * (count - 1)))
        lower_bound = average - 2.35 * standard_error
        excluded_rate = self.excluded / (n + self.excluded)
        weight = min(1, n / 90, days / 45, len(weekly) / 6, directional_n / 60) * (1 - excluded_rate)
        raw = (20 * clamp(positive_share) + 25 * clamp(expectancy / 0.4) +
               15 * clamp(((pf or 0) - 1) / 0.9) + 15 * clamp(1 - self.drawdown / 7) +
               15 * positive_regimes / 4 + 10 * clamp((accuracy - .50) / .20))
        points = math.floor(raw * weight)
        daily_ok = self.day_loss <= settings["dailyLossPct"]
        fresh = now - self.last <= 30 * DAY
        if (expectancy <= 0 or (pf is not None and pf < 1) or not daily_ok or self.stop_breached or
                self.drawdown > 7 or positive_share < 0.5 or accuracy < .50 or min(long_r, short_r) <= 0):
            return min(49, points), True
        if len(qualified) < 2 or positive_regimes < 2 or lower_bound <= 0 or excluded_rate > 0.05 or not fresh:
            return min(69, points), True
        strong = (n >= 120 and days >= 60 and len(weekly) >= 8 and positive_share >= 0.75
                  and expectancy >= 0.12 and pf is not None and pf >= 1.35 and self.losers >= 10
                  and self.drawdown <= 3 and directional_n >= 80 and accuracy >= .55 and min(long_n, short_n) >= 30
                  and sum(size >= 10 for size, _ in self.regimes.values()) >= 3 and all(x > 0 for x in qualified)
                  and excluded_rate <= 0.01)
        if not strong:
            return min(89, points), True
        rigorous = (n >= 240 and days >= 120 and len(weekly) >= 16 and positive_share >= 0.9
                    and expectancy >= 0.22 and lower_bound >= 0.10 and pf >= 1.6 and self.losers >= 20
                    and self.drawdown <= 2 and not self.excluded and directional_n >= 160 and accuracy >= .60
                    and min(long_n, short_n) >= 60 and min(long_r, short_r) >= .10
                    and len(self.regimes) == 4 and all(size >= 25 and total > 0 for size, total in self.regimes.values()))
        return (100 if rigorous else min(99, max(90, points))), True


def score_trials(report, trades, settings, now, forecasts, exclusions=()):
    """Replay the recorded evidence, independent of refresh rate or a last-viewed score."""
    evidence = Evidence(trades[0][2]["equity"])
    events = [(at, 0, hit) for at, hit in forecasts]
    events += [(at, 1, None) for at in exclusions]
    events += [(trade[1], 2, trade) for trade in trades]
    events.sort(key=lambda item: item[:2])
    score = last_step = previous_ceiling = 0
    for at, kind, item in events:
        net = 0
        if kind == 0:
            evidence.directional_n += 1
            evidence.hits += item
        elif kind == 1:
            evidence.excluded += 1
        else:
            evidence.add(*item)
            net = item[2]["net"]
        ceiling, _ = evidence.assess(settings, at)
        both_positive = all(total > 0 for _, total in evidence.sides.values())
        step = (1 if net > 0 and both_positive and ceiling >= previous_ceiling else -1 if net < 0 else 0)
        next_score = max(0, min(ceiling, score + step))
        last_step = next_score - score
        score, previous_ceiling = next_score, ceiling
    ceiling, enough = evidence.assess(settings, now, report)
    if ceiling < score:
        last_step, score = ceiling - score, ceiling
    status = ("insufficient" if not enough else "rigorous" if score == 100 else "strong" if score >= 90 else "measured")
    report.update(status=status, score=score, evidenceCeiling=ceiling, lastStep=last_step)
    return report
