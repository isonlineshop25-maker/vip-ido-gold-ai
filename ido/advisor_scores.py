"""Scoreboard: every directional bot vote is checked against the real price 5 minutes later.

A vote only counts as a hit when the move in the voted direction would also have paid the
round-trip costs. Bots that are right more often get a heavier vote; bots that are often wrong
count less. Only the last 14 days count, so the weights keep adapting to the current market.
Nothing in this module can send an order.
"""

from advisors import DIRECTIONAL

HORIZON = 5 * 60            # seconds between the decision candle and the checked candle
WINDOW = 14 * 86400         # only recent votes shape the weight
GRACE = 10 * 60             # how long we wait for the checked candle before skipping the vote
PRIOR = 10                  # pseudo-votes at 50%: a few lucky calls cannot make a bot dominant
MIN_WEIGHT, MAX_WEIGHT = 0.2, 2.0

NAMES = {
    "trend": "Trendbot", "momentum": "Momentumbot", "volatility": "Volatiliteitsbot",
    "candle": "Candlelezer", "breakout": "Uitbraakbot", "session": "Sessiebot", "bigtrend": "Grote-trendbot",
    "risk": "Risicobewaker", "skeptic": "Scepticus",
}
ROLES = {
    "trend": "Kijkt of toppen en bodems stijgen of dalen.",
    "momentum": "RSI en snelheid van de koers.",
    "volatility": "Grote candles die door de vorige toppen of bodems breken.",
    "candle": "Leest candlepatronen: engulfing, hamer, shooting star, doji, bij steun of weerstand.",
    "breakout": "Uitbraak uit het kanaal van de laatste 8 minuten.",
    "session": "Handelt alleen mee als Londen of New York open is.",
    "bigtrend": "Uur- en 4-uurgrafiek: nooit tegen de grote trend in.",
    "risk": "Harde grenzen: kosten, stop-loss, dagverlies. Kan alles blokkeren.",
    "skeptic": "Zoekt redenen waarom de instap fout kan zijn.",
}


def init_scores(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS advisor_votes (
            account TEXT NOT NULL, provider TEXT NOT NULL, agent TEXT NOT NULL, candle INTEGER NOT NULL,
            target INTEGER NOT NULL, side TEXT NOT NULL, entry REAL NOT NULL, cost REAL NOT NULL,
            evaluated INTEGER, exit REAL, hit INTEGER,
            PRIMARY KEY(account, provider, agent, candle)
        );
        CREATE INDEX IF NOT EXISTS advisor_votes_pending ON advisor_votes(account, provider, target) WHERE evaluated IS NULL;
        CREATE INDEX IF NOT EXISTS advisor_votes_scored ON advisor_votes(account, provider, agent, candle) WHERE hit IS NOT NULL;
    """)


def record(db, sid, provider, votes, candle_time, entry, cost):
    """Store each BUY/SELL vote once per candle. WAIT votes are not scored."""
    if cost is None or entry is None:
        return 0
    stored = 0
    for v in votes:
        if v["agent"] in DIRECTIONAL and v["vote"] in ("BUY", "SELL"):
            stored += db.execute("""INSERT OR IGNORE INTO advisor_votes(account,provider,agent,candle,target,side,entry,cost)
                                    VALUES(?,?,?,?,?,?,?,?)""",
                                 (sid, provider, v["agent"], candle_time, candle_time + HORIZON, v["vote"], entry, cost)).rowcount
    return stored


def settle(db, sid, provider, bars, cutoff, now):
    """Compare each pending vote with the close of the candle HORIZON later (closed candles only)."""
    closes = {b["time"]: b["close"] for b in (bars or []) if b["time"] + 60 <= cutoff}
    for row in list(db.execute("""SELECT agent,candle,target,side,entry,cost FROM advisor_votes
                                  WHERE account=? AND provider=? AND evaluated IS NULL AND target<=?""",
                               (sid, provider, now))):
        close = closes.get(row["target"])
        if close is not None:
            move = (close - row["entry"]) * (1 if row["side"] == "BUY" else -1)
            hit = int(move >= row["cost"])
        elif now > row["target"] + 60 + GRACE:
            close, hit = None, None
        else:
            continue
        db.execute("""UPDATE advisor_votes SET evaluated=?, exit=?, hit=?
                      WHERE account=? AND provider=? AND agent=? AND candle=?""",
                   (now, close, hit, sid, provider, row["agent"], row["candle"]))


def weight_for(hits, total):
    score = (hits + PRIOR / 2) / (total + PRIOR)
    return round(max(MIN_WEIGHT, min(MAX_WEIGHT, 1 + 4 * (score - 0.5))), 2)


def stats(db, sid, provider, now):
    rows = {r["agent"]: r for r in db.execute("""
        SELECT agent, count(hit) AS total, coalesce(sum(hit),0) AS hits,
               coalesce(sum(CASE WHEN hit=1 THEN (CASE side WHEN 'BUY' THEN exit-entry ELSE entry-exit END) - cost
                                 WHEN hit=0 THEN (CASE side WHEN 'BUY' THEN exit-entry ELSE entry-exit END) - cost END),0) AS edge
        FROM advisor_votes WHERE account=? AND provider=? AND candle>=? AND hit IS NOT NULL GROUP BY agent
        """, (sid, provider, now - WINDOW))}
    pending = {r["agent"]: r["n"] for r in db.execute("""
        SELECT agent, count(*) AS n FROM advisor_votes WHERE account=? AND provider=? AND evaluated IS NULL GROUP BY agent
        """, (sid, provider))}
    result = []
    for agent in DIRECTIONAL:
        r = rows.get(agent)
        total, hits = (r["total"], r["hits"]) if r else (0, 0)
        result.append(dict(agent=agent, name=NAMES[agent], role=ROLES[agent], scored=total, hits=hits,
                           accuracy=round(100 * hits / total, 1) if total else None,
                           edgePerOz=round(r["edge"] / total, 3) if r and total else None,
                           pending=pending.get(agent, 0), weight=weight_for(hits, total)))
    return result


def weights(db, sid, provider, now):
    return {s["agent"]: s["weight"] for s in stats(db, sid, provider, now)}


def report(db, sid, provider, now, latest_round=None):
    votes = {v["agent"]: v for v in (latest_round or {}).get("votes", [])}
    bots = stats(db, sid, provider, now)
    for b in bots:
        v = votes.get(b["agent"])
        b.update(vote=v["vote"] if v else None, evidence=v["evidence"] if v else None)
    guards = [dict(agent=a, name=NAMES[a], role=ROLES[a], scored=None, weight=None,
                   vote=votes[a]["vote"] if a in votes else None,
                   evidence=votes[a]["evidence"] if a in votes else None) for a in ("risk", "skeptic")]
    return dict(horizonMinutes=HORIZON // 60, windowDays=WINDOW // 86400, bots=bots, guards=guards,
                recommendation=(latest_round or {}).get("recommendation"),
                reason=(latest_round or {}).get("reason"), roundTime=(latest_round or {}).get("time"))
