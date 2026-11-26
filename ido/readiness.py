"""'Klaar voor echt geld?' meter, computed only from closed paper trades on the selected feed.

The bar reaches 100% only when every requirement is met. Until then it is capped at 99%.
Nothing in this module can send an order; it only reports.
"""

import json
import math
from statistics import mean, stdev

from strategy import START_BALANCE

DAY, WEEK = 86400, 7 * 86400
MIN_TRADES = 200
MIN_DAYS = 28
MIN_PROFIT_FACTOR = 1.3
MIN_WEEKS = 4
MIN_WINNING_WEEK_SHARE = 0.6
MAX_DRAWDOWN = 0.05 * START_BALANCE      # never more than 5% below the highest point
MIN_T = 2.0                              # average result at least 2 standard errors above zero


def _clamp(x):
    return max(0.0, min(1.0, x))


def closed_trades(db, sid, provider):
    rows = db.execute("""SELECT opened, closed, pnl, data FROM trades
                         WHERE account=? AND closed IS NOT NULL ORDER BY closed, rowid""", (sid,))
    return [(r["opened"], r["closed"], r["pnl"]) for r in rows if json.loads(r["data"]).get("feed") == provider]


def report(db, sid, provider, now):
    trades = closed_trades(db, sid, provider) if sid else []
    pnls = [p for _, _, p in trades]
    n = len(pnls)
    first = trades[0][0] if trades else None
    days = (now - first) / DAY if first else 0
    wins, losses = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p < 0)
    pf = wins / losses if losses else (math.inf if wins else None)
    equity = peak = START_BALANCE
    drawdown = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    weeks = {}
    for _, closed, p in trades:
        weeks[closed // WEEK] = weeks.get(closed // WEEK, 0) + p
    full = {w: v for w, v in weeks.items() if w < now // WEEK}      # only finished weeks count
    winning = sum(v > 0 for v in full.values())
    share = winning / len(full) if full else 0
    t = (mean(pnls) / (stdev(pnls) / math.sqrt(n))) if n > 2 and stdev(pnls) > 0 else 0

    checks = [
        dict(key="trades", label="Genoeg trades", target=f"{MIN_TRADES} trades",
             value=f"{n} trades", progress=_clamp(n / MIN_TRADES), met=n >= MIN_TRADES,
             why="Pas na honderden trades weet je of het geen geluk is."),
        dict(key="days", label="Lang genoeg getest", target=f"{MIN_DAYS} dagen",
             value=f"{days:.0f} dagen", progress=_clamp(days / MIN_DAYS), met=days >= MIN_DAYS,
             why="De markt verandert. Hij moet rustige en wilde weken meemaken."),
        dict(key="profit", label="Winst na alle kosten", target=f"winstfactor {MIN_PROFIT_FACTOR:.1f}",
             value="—" if pf is None else ("∞" if pf == math.inf else f"winstfactor {pf:.2f}") + f" · {sum(pnls):+.2f} USD",
             progress=0.0 if pf is None else _clamp((min(pf, 3) - 1) / (MIN_PROFIT_FACTOR - 1)),
             met=pf is not None and pf >= MIN_PROFIT_FACTOR and sum(pnls) > 0,
             why="Voor elke dollar verlies minstens 1,30 dollar winst, na spread en kosten."),
        dict(key="weeks", label="De meeste weken in de plus", target=f"{MIN_WINNING_WEEK_SHARE:.0%} van minstens {MIN_WEEKS} weken",
             value=f"{winning} van {len(full)} weken", met=len(full) >= MIN_WEEKS and share >= MIN_WINNING_WEEK_SHARE,
             progress=_clamp(len(full) / MIN_WEEKS) * _clamp(share / MIN_WINNING_WEEK_SHARE),
             why="Niet één geluksweek, maar steeds opnieuw winst."),
        dict(key="drawdown", label="Nooit te diep weggezakt", target=f"max {MAX_DRAWDOWN:.0f} USD onder de top",
             value=f"{drawdown:.2f} USD", met=n > 0 and drawdown <= MAX_DRAWDOWN,
             progress=(1.0 if drawdown <= MAX_DRAWDOWN else _clamp(1 - (drawdown - MAX_DRAWDOWN) / MAX_DRAWDOWN)) if n else 0.0,
             why="Met echt geld moet je een slechte periode kunnen uitzitten."),
        dict(key="chance", label="Geen toeval", target=f"zekerheid {MIN_T:.1f}",
             value=f"zekerheid {t:.1f}", progress=_clamp(t / MIN_T), met=t >= MIN_T,
             why="Rekent uit of de gemiddelde winst groot genoeg is om geen toeval te zijn."),
    ]
    ready = all(c["met"] for c in checks)
    score = round(100 * mean(c["progress"] for c in checks))
    if not ready:
        score = min(score, 99)
    for c in checks:
        c["progress"] = round(c["progress"], 3)
    message = ("Klaar: de bot heeft op demo alle eisen gehaald. Begin met echt geld alleen klein, en blijf de cijfers volgen."
               if ready else "Nog niet klaar. Laat hem op demo doorlopen. De balk komt pas op 100% als alle zes eisen gehaald zijn.")
    return dict(score=score, ready=ready, message=message, checks=checks, provider=provider)


def announce(db, sid, provider, state, now, audit):
    """Write one log line when the meter first reaches 100% (and again if it ever drops)."""
    r = report(db, sid, provider, now)
    if r["ready"] != state.get("readyForLive", False):
        state["readyForLive"] = r["ready"]
        audit(db, sid, now, "success" if r["ready"] else "warning",
              "Klaar-voor-echt-geld-meter staat op 100%." if r["ready"] else "Klaar-voor-echt-geld-meter is onder 100% gezakt.")
    return r
