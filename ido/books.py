"""Causal signals and entry limits for two tagged paper books."""

HOUR = 3600
SWING_MINUTES = 3 * 24 * 60
DEFAULT_PREFS = dict(scalpEnabled=True, swingEnabled=False, scalpMinutes=15)
SCALP_HORIZONS = (5, 15, 30, 60)


def prefs(state):
    return state.get("strategies", DEFAULT_PREFS)


def book_of(trade):
    return trade.get("strategy", "scalp")


def rollup(rows, base, step, cutoff):
    """Never use unfinished candles or fabricate missing intervals."""
    buckets = {}
    for row in rows:
        t = row["time"]
        start = t // step * step
        if t % base == 0 and start + step <= cutoff:
            buckets.setdefault(start, {})[t] = row
    output = []
    for start, group in sorted(buckets.items()):
        if len(group) != step // base:
            continue
        ordered = [group[t] for t in range(start, start + step, base)]
        output.append(dict(time=start, open=ordered[0]["open"], close=ordered[-1]["close"],
                           high=max(b["high"] for b in ordered), low=min(b["low"] for b in ordered)))
    return output


def tail(rows, step, count, cutoff, age):
    bars = [b for b in rows if b["time"] + step <= cutoff][-count:]
    if (len(bars) < count or cutoff - bars[-1]["time"] - step > age
            or any(b["time"] - a["time"] != step for a, b in zip(bars, bars[1:]))):
        return []
    return bars


def direction(bars, minimum):
    if not bars:
        return None
    moves = [b["close"] - a["close"] for a, b in zip(bars, bars[1:])]
    total, move = sum(map(abs, moves)), sum(moves)
    if not total or abs(move) / total < .25 or abs(move) / bars[0]["close"] < minimum:
        return None
    return "BUY" if move > 0 else "SELL"


def atr(bars):
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
              for a, b in zip(bars, bars[1:])]
    return sum(ranges) / len(ranges) if ranges else 0


def scalp_filter(rows, cutoff, price, proposed):
    if proposed not in ("BUY", "SELL"):
        return None
    fast = tail(rows, 60, 10, cutoff, 120)
    slow = tail(rollup(rows, 60, 300, cutoff), 300, 3, cutoff, 600)
    if not fast or not slow:
        return "Nog onvoldoende aaneengesloten gesloten M1- en M5-candles."
    if direction(slow, .00005) != proposed:
        return "De gesloten M5-candles bevestigen de M1-richting niet."
    if not .000015 <= atr(fast) / price <= .003:
        return "De M1-volatiliteit ligt buiten het scalpbereik."
    return None


def swing_setup(hours, cutoff, price):
    hourly = tail(hours, HOUR, 8, cutoff, 2 * HOUR)
    higher = tail(rollup(hours, HOUR, 4 * HOUR, cutoff), 4 * HOUR, 4, cutoff, 4 * HOUR)
    if not hourly or not higher:
        return "WAIT", "Nog onvoldoende aaneengesloten gesloten H1- en H4-candles.", None, None
    action = direction(higher, .001)
    if not action or direction(hourly, .0005) != action:
        return "WAIT", "H1 en H4 geven geen gedeelde trendrichting.", None, None
    volatility = atr(hourly) / price
    if not .0003 <= volatility <= .025:
        return "WAIT", "De uurvolatiliteit ligt buiten het swingbereik.", None, None
    distance = round(max(.004 * price, 1.8 * volatility * price), 3)
    return action, f"Gesloten H4-trend en H1 bevestigen {action}.", distance, ("high" if volatility >= .003 else "low")


def entry_limit(db, sid, book, now):
    gap, cap = (300, 8) if book == "scalp" else (4 * HOUR, 2)
    start = now // 86400 * 86400
    count, latest = db.execute("""
        SELECT count(*) FILTER (WHERE opened>=?), max(opened)
        FROM trades WHERE account=? AND opened>=? AND coalesce(json_extract(data,'$.strategy'),'scalp')=?
        """, (start, sid, min(start, now - gap), book)).fetchone()
    if count >= cap:
        return f"Dagmaximum van {cap} nieuwe {book}-posities bereikt."
    if latest is not None and now - latest < gap:
        return "Afkoelperiode na de vorige instap."
    return None


def init_swing(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS swing_calls (
            trade TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL,
            made INTEGER NOT NULL, target INTEGER NOT NULL, side TEXT NOT NULL,
            evaluated INTEGER, hit INTEGER
        );
        CREATE INDEX IF NOT EXISTS swing_pending ON swing_calls(account,provider,target) WHERE evaluated IS NULL;
        CREATE INDEX IF NOT EXISTS swing_marks ON swing_calls(account,provider,target) WHERE hit IS NOT NULL;
        """)


def freeze_swing(db, sid, provider, trade, now):
    target = (now // HOUR + 1) * HOUR
    db.execute("INSERT INTO swing_calls(trade,account,provider,made,target,side) VALUES(?,?,?,?,?,?)",
               (trade["id"], sid, provider, now, target, trade["side"]))


def settle_swing(db, sid, provider, hours, cutoff, now, cancel=False):
    pending = db.execute("SELECT trade,target,side FROM swing_calls WHERE account=? AND provider=? AND evaluated IS NULL",
                         (sid, provider)).fetchall()
    if not pending:
        return
    visible = {b["time"]: b for b in hours if b["time"] + HOUR <= cutoff}
    for trade, target, side in pending:
        bar = visible.get(target)
        deadline = target + 4 * HOUR
        if bar is not None and now <= deadline and not cancel:
            hit = None if bar["close"] == bar["open"] else (bar["close"] > bar["open"]) == (side == "long")
        elif cancel or now > deadline:
            hit = None
        else:
            continue
        db.execute("UPDATE swing_calls SET evaluated=?,hit=? WHERE trade=?", (now, hit, trade))
