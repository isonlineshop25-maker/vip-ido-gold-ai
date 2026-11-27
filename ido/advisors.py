"""Pure, local advisors. None has database, network or order access."""

from dataclasses import asdict, dataclass
from statistics import mean, median

POLICY = "panel-discussion-3.0"
VERSIONS = {"trend": "structure-1.0", "momentum": "rsi-roc-1.0",
            "volatility": "range-shape-1.0", "candle": "patterns-1.0", "breakout": "channel-1.0",
            "pullback": "dip-1.0", "spike": "burst-1.0", "session": "london-ny-1.0", "bigtrend": "h1-h4-1.0",
            "risk": "paper-limits-1.0", "skeptic": "countercheck-1.0"}
# Bots that vote for a direction. Their weight comes from their own scored track record.
DIRECTIONAL = ("trend", "momentum", "volatility", "candle", "breakout", "pullback", "spike", "session", "bigtrend")
MIN_SUPPORT_WEIGHT = 2.0
MAX_OPPOSE_SHARE = 0.4


@dataclass(frozen=True, slots=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class Risk:
    fresh: bool
    costs_valid: bool
    capacity: bool
    daily_room: bool
    size_ok: bool
    cost_to_stop: float | None
    stop_distance: float | None = None


def causal_bars(rows, now, quote_time, fresh):
    if not fresh or quote_time is None:
        return ()
    cutoff = min(now, quote_time)
    closed = [r for r in (rows or []) if r["time"] + 60 <= cutoff][-30:]
    if not closed or cutoff - (closed[-1]["time"] + 60) > 120:
        return ()
    start = len(closed) - 1
    while start > 0 and closed[start]["time"] - closed[start - 1]["time"] == 60:
        start -= 1
    return tuple(Bar(**{k: row[k] for k in ("time", "open", "high", "low", "close")}) for row in closed[start:])


def vote(agent, action, confidence, evidence, weight=1.0):
    return dict(agent=agent, version=VERSIONS[agent], vote=action, confidence=confidence, evidence=evidence,
                weight=weight)


def strength(checks):
    return "high" if checks >= 3 else "medium" if checks == 2 else "low"


def insufficient(agent, available, required):
    return vote(agent, "WAIT", "none", f"{available}/{required} aaneengesloten, gesloten minuutcandles.")


def trend(bars):
    if len(bars) < 12:
        return insufficient("trend", len(bars), 12)
    prev, recent = bars[-12:-6], bars[-6:]
    mid_old, mid_new = mean(b.close for b in prev), mean(b.close for b in recent)
    ceiling = max(b.high for b in recent) - max(b.high for b in prev)
    floor = min(b.low for b in recent) - min(b.low for b in prev)
    direction = 1 if mid_new > mid_old else -1 if mid_new < mid_old else 0
    # Structural breaks require an actual move in both extremes or a close following the shift.
    checks = sum(direction * metric > 0 for metric in (mid_new-mid_old, ceiling, floor))
    action = ("BUY" if direction > 0 else "SELL") if checks >= 2 else "WAIT"
    return vote("trend", action, strength(checks),
                f"Gemiddelde van 6 candles: {mid_new-mid_old:+.2f} USD; top {ceiling:+.2f}, bodem {floor:+.2f} vs. vorig blok.")


def momentum(bars):
    if len(bars) < 15:
        return insufficient("momentum", len(bars), 15)
    closes = [b.close for b in bars[-15:]]
    moves = [b-a for a, b in zip(closes, closes[1:])]
    gross = sum(map(abs, moves))
    rsi = 100 * sum(max(0, x) for x in moves) / gross if gross else 50
    roc5, roc10 = closes[-1] - closes[-6], closes[-1] - closes[-11]
    up = sum((54 <= rsi <= 76, roc5 > 0, roc10 > 0))
    down = sum((24 <= rsi <= 46, roc5 < 0, roc10 < 0))
    action = "BUY" if up >= 2 and 50 < rsi <= 80 else "SELL" if down >= 2 and 20 <= rsi < 50 else "WAIT"
    return vote("momentum", action, strength(max(up, down)) if action != "WAIT" else "low",
                f"RSI(14) {rsi:.1f}; koersverandering 5/10 min {roc5:+.2f}/{roc10:+.2f} USD.")


def pullback(bars):
    if len(bars) < 20:
        return insufficient("pullback", len(bars), 20)
    closes = [b.close for b in bars[-20:]]
    old, new = mean(closes[:10]), mean(closes[10:])
    dip = closes[-3] - closes[-5]
    if new > old and dip < 0 and closes[-1] > closes[-2]:
        return vote("pullback", "BUY", "medium", f"Stijgende lijn ({new-old:+.2f} USD) met een dipje ({dip:+.2f}) dat nu herstelt.")
    if new < old and dip > 0 and closes[-1] < closes[-2]:
        return vote("pullback", "SELL", "medium", f"Dalende lijn ({new-old:+.2f} USD) met een opveer ({dip:+.2f}) die nu terugzakt.")
    return vote("pullback", "WAIT", "low", f"Geen dipje om op in te stappen (lijn {new-old:+.2f} USD).")


def spike(bars):
    if len(bars) < 15:
        return insufficient("spike", len(bars), 15)
    base = ranges(bars[:-3])
    avg = mean(base) if base else 0
    last3 = bars[-3:]
    move = last3[-1].close - last3[0].open
    top, bottom = max(b.high for b in last3), min(b.low for b in last3)
    span = top - bottom
    if not avg or not span or abs(move) < 2.5 * avg:
        return vote("spike", "WAIT", "low", f"Rustige koers: {move:+.2f} USD in 3 minuten.")
    place = (last3[-1].close - bottom) / span
    if move > 0 and place >= 0.7:
        return vote("spike", "BUY", "medium", f"Plotselinge stijging {move:+.2f} USD in 3 minuten, sluit dicht bij de top.")
    if move < 0 and place <= 0.3:
        return vote("spike", "SELL", "medium", f"Plotselinge daling {move:+.2f} USD in 3 minuten, sluit dicht bij de bodem.")
    return vote("spike", "WAIT", "low", "Plotselinge beweging, maar de candle sluit zwak: niet meegaan.")


def ranges(bars):
    return [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close)) for a, b in zip(bars, bars[1:])]


def volatility(bars):
    if len(bars) < 12:
        return insufficient("volatility", len(bars), 12)
    last, previous = bars[-1], bars[-4:-1]
    typical = median(ranges(bars[-12:-1]))
    width = last.high - last.low
    body = last.close - last.open
    share = abs(body) / width if width > 0 else 0
    expansion = width / typical if typical > 0 else 0
    breaks = (body > 0 and last.close > max(b.high for b in previous)) or (
        body < 0 and last.close < min(b.low for b in previous))
    if expansion > 3:
        return vote("volatility", "WAIT", "medium", f"Uitschieter: candlebereik is {expansion:.1f}× het normale bereik.")
    checks = sum((share >= 0.55, expansion >= 0.8, breaks))
    action = ("BUY" if body > 0 else "SELL") if breaks and checks >= 2 else "WAIT"
    return vote("volatility", action, strength(checks),
                f"Candlebody {share:.0%}; bereik {expansion:.1f}× mediaan; {'uitbraak' if breaks else 'geen uitbraak'}.")


def candle(bars):
    """Reads the last closed candle like a chart trader: pattern + location + confirmation."""
    if len(bars) < 16:
        return insufficient("candle", len(bars), 16)
    last, prev = bars[-1], bars[-2]
    width = last.high - last.low
    if width <= 0:
        return vote("candle", "WAIT", "low", "Platte candle zonder bereik.")
    body = abs(last.close - last.open)
    upper, lower = last.high - max(last.open, last.close), min(last.open, last.close) - last.low
    typical = max(0.01, median(ranges(bars[-15:])))
    context = bars[-21:-1]
    support, resistance = min(b.low for b in context), max(b.high for b in context)
    if body <= 0.1 * width:
        return vote("candle", "WAIT", "low", f"Doji: markt twijfelt (body {body:.2f} van {width:.2f} USD).")
    bull_engulf = prev.close < prev.open and last.close > last.open and last.open <= prev.close and last.close >= prev.open
    bear_engulf = prev.close > prev.open and last.close < last.open and last.open >= prev.close and last.close <= prev.open
    hammer = lower >= 0.6 * width and upper <= 0.22 * width
    star = upper >= 0.6 * width and lower <= 0.22 * width
    bull = "bullish engulfing" if bull_engulf else "hamer (pin bar omhoog)" if hammer else None
    bear = "bearish engulfing" if bear_engulf else "shooting star (pin bar omlaag)" if star else None
    if bool(bull) == bool(bear):
        return vote("candle", "WAIT", "low", "Geen duidelijk candlepatroon op de laatste gesloten candle.")
    side = 1 if bull else -1
    name = bull or bear
    near = (last.low - support <= typical) if side > 0 else (resistance - last.high <= typical)
    confirm = last.close > prev.high if side > 0 else last.close < prev.low
    size = width >= 0.8 * typical
    checks = 1 + sum((near, confirm, size))
    action = ("BUY" if side > 0 else "SELL") if checks >= 3 else "WAIT"
    spot = "bij steun" if side > 0 else "bij weerstand"
    return vote("candle", action, strength(checks),
                f"{name.capitalize()}; {spot if near else 'niet ' + spot}; "
                f"{'slot voorbij vorige candle' if confirm else 'geen bevestigend slot'}; steun {support:.2f}, weerstand {resistance:.2f}.")


def breakout(bars):
    """Channel breakout of the previous 8 closed minutes (was the separate worker)."""
    if len(bars) < 10:
        return insufficient("breakout", len(bars), 10)
    prior, last = bars[-9:-1], bars[-1]
    ceiling, floor = max(b.high for b in prior), min(b.low for b in prior)
    typical = median(b.high - b.low for b in prior)
    current = last.high - last.low
    if typical <= 0 or current > 2.5 * typical:
        return vote("breakout", "WAIT", "medium", "Uitschieter of onbruikbaar bereik: geen uitbraak vertrouwen.")
    side = 1 if last.close > ceiling else -1 if last.close < floor else 0
    if not side:
        return vote("breakout", "WAIT", "low", f"Koers {last.close:.2f} binnen kanaal {floor:.2f}–{ceiling:.2f}.")
    share = abs(last.close - last.open) / current if current > 0 else 0
    margin = (last.close - ceiling) if side > 0 else (floor - last.close)
    checks = 1 + sum((share >= 0.5, margin >= 0.25 * typical))
    action = ("BUY" if side > 0 else "SELL") if checks >= 2 else "WAIT"
    return vote("breakout", action, strength(checks),
                f"Slot {last.close:.2f} {'boven' if side > 0 else 'onder'} kanaal {floor:.2f}–{ceiling:.2f}; body {share:.0%}.")


def session_of(ts):
    hour = (ts // 3600) % 24
    london, new_york = 7 <= hour < 16, 13 <= hour < 21
    return ("Londen + New York" if london and new_york else "Londen" if london else
            "New York" if new_york else None)


def session(bars, now):
    """Gold moves most in London/New York. Outside those hours costs weigh heavier."""
    name = session_of(bars[-1].time + 60 if bars else now)
    if name is None:
        return vote("session", "WAIT", "medium", "Rustige (Aziatische) sessie: weinig beweging, kosten wegen zwaar. Uren in UTC.")
    if len(bars) < 16:
        return insufficient("session", len(bars), 16)
    typical = max(0.01, median(ranges(bars[-16:])))
    drift = bars[-1].close - bars[-16].close
    if abs(drift) < 2 * typical:
        return vote("session", "WAIT", "low", f"{name}-sessie open, maar geen duidelijke drift ({drift:+.2f} USD in 15 min).")
    confidence = "high" if "+" in name else "medium"
    return vote("session", "BUY" if drift > 0 else "SELL", confidence,
                f"{name}-sessie actief; drift {drift:+.2f} USD in 15 min (normaal bereik {typical:.2f}).")


def _drift(closes):
    moves = [b - a for a, b in zip(closes, closes[1:])]
    total = sum(map(abs, moves))
    return (sum(moves), sum(moves) / total if total else 0)


def bigtrend(hours):
    """The bigger picture: closed H1 candles and the 4-hour direction must agree."""
    hours = [h for h in (hours or [])][-13:]
    if len(hours) < 13 or any(b["time"] - a["time"] != 3600 for a, b in zip(hours, hours[1:])):
        return vote("bigtrend", "WAIT", "none", f"{len(hours)}/13 aaneengesloten gesloten uurcandles.")
    closes = [h["close"] for h in hours]
    h1_move, h1_eff = _drift(closes[-7:])
    h4_move, h4_eff = _drift(closes[::4])
    price = closes[-1]
    up = h1_move > 0 and h4_move > 0 and h1_eff >= .25 and h4_move / price >= .001
    down = h1_move < 0 and h4_move < 0 and h1_eff >= .25 and -h4_move / price >= .001
    if not (up or down):
        return vote("bigtrend", "WAIT", "low", f"Uur- en 4-uurrichting niet gelijk (6u {h1_move:+.2f}, 12u {h4_move:+.2f} USD).")
    checks = 2 + (abs(h4_eff) >= .5)
    return vote("bigtrend", "BUY" if up else "SELL", strength(checks),
                f"Grote trend {'omhoog' if up else 'omlaag'}: 6 uur {h1_move:+.2f}, 12 uur {h4_move:+.2f} USD.")


def risk(bars, r):
    for okay, reason in (
        (r.fresh, "Geen actuele, controleerbare spotkoers."),
        (r.costs_valid, "Het kostenmodel ontbreekt of is ongeldig."),
        (r.capacity, "Maximum aantal open posities bereikt."),
        (r.daily_room, "Geen ruimte binnen het maximale dagverlies."),
        (r.size_ok, "Geen geldige positieomvang binnen de serverlimieten."),
        (r.cost_to_stop is not None and r.cost_to_stop < 1, "Rondekosten bereiken de afstand tot de stop-loss."),
    ):
        if not okay:
            return vote("risk", "VETO", "high", reason)
    if len(bars) < 12:
        return insufficient("risk", len(bars), 12)
    recent = ranges(bars[-12:])
    typical = median(recent)
    if r.stop_distance is not None and (typical >= r.stop_distance or recent[-1] >= 1.5 * r.stop_distance):
        return vote("risk", "VETO", "high",
                    f"Beweging te groot voor de stopafstand ({r.stop_distance:.2f} USD); mediaan {typical:.2f}, laatste {recent[-1]:.2f}.")
    return vote("risk", "PASS", "high",
                f"Harde grenzen gehaald; kosten {r.cost_to_stop:.0%} van de stopafstand, normaal minuutbereik {typical:.2f} USD.")


def skeptic(bars, proposal):
    if len(bars) < 12:
        return insufficient("skeptic", len(bars), 12)
    if proposal not in ("BUY", "SELL"):
        return vote("skeptic", "WAIT", "none", "Geen enginevoorstel om tegen te toetsen.")
    last = bars[-1]
    typical = max(0.01, median(ranges(bars[-12:])))
    sign = 1 if proposal == "BUY" else -1
    recent = bars[-6:-1]
    opposition = sign * (last.close - bars[-3].close) < -typical
    rejection = (last.high - max(last.open, last.close)) if sign > 0 else (min(last.open, last.close)-last.low)
    stretch = sign * (last.close-mean(b.close for b in recent)) > 3 * typical
    concerns = [reason for flag, reason in (
        (opposition, "laatste twee minuten bewegen tegen de opzet"),
        (rejection > 1.4 * typical, "lange afwijzingswick tegen de opzet"),
        (stretch, "instap volgt een uitgerekte koersbeweging"),
    ) if flag]
    if concerns:
        return vote("skeptic", "FLAG", strength(len(concerns)), "; ".join(concerns).capitalize() + ".")
    return vote("skeptic", "PASS", "high", "Geen sterke tegenbeweging, afwijzingswick of uitgerekte koers in deze drie toetsen.")


def recommend(proposed, votes, learning=False):
    directional = [v for v in votes if v["agent"] in DIRECTIONAL]
    buy, sell, wait = ([v["vote"] for v in directional].count(s) for s in ("BUY", "SELL", "WAIT"))
    risk_vote = next(v["vote"] for v in votes if v["agent"] == "risk")
    reviewer = next(v for v in votes if v["agent"] == "skeptic")
    if risk_vote != "PASS":
        return "WAIT", ("Risicoveto: geen nieuwe positie." if risk_vote == "VETO" else
                        "Risicobeoordeling wacht op voldoende candles."), buy, sell, wait
    if proposed == "WAIT":
        return "WAIT", "De hoofdstrategie heeft geen instapsignaal.", buy, sell, wait
    against = "SELL" if proposed == "BUY" else "BUY"
    supporters = [v for v in directional if v["vote"] == proposed]
    support = sum(v.get("weight", 1.0) for v in supporters)
    oppose = sum(v.get("weight", 1.0) for v in directional if v["vote"] == against)
    # Learning (paper trading): "two average bots" instead of a fixed 2.0: bots with a poor track record sit at the weight floor, and a fixed
    # threshold would then make a trade impossible however many of them agree. With default weights (1.0) this is still 2.0.
    average = sum(v.get("weight", 1.0) for v in directional) / len(directional) if directional else 1.0
    need = MIN_SUPPORT_WEIGHT * min(1.0, average) if learning else MIN_SUPPORT_WEIGHT
    if len(supporters) < 2 or support < need:
        return "WAIT", f"Te weinig steun: {len(supporters)} bots, stemgewicht {support:.1f} (minimaal 2 bots en {need:.1f}).", buy, sell, wait
    if oppose > MAX_OPPOSE_SHARE * support:
        return "WAIT", f"Te veel tegenstemmen: gewicht {oppose:.1f} tegen {support:.1f}.", buy, sell, wait
    big = next((v for v in directional if v["agent"] == "bigtrend"), None)
    if big and big["vote"] == against:
        return "WAIT", "Niet tegen de grote trend (uur/4-uur) in handelen.", buy, sell, wait
    if reviewer["vote"] == "WAIT" or (reviewer["vote"] == "FLAG" and reviewer["confidence"] in ("medium", "high")):
        return "WAIT", "De sceptische toets mist bewijs of signaleert meerdere bezwaren.", buy, sell, wait
    return proposed, ("Door de bots bevestigd; scepticus waarschuwt." if reviewer["vote"] == "FLAG" else
                      f"Bots bevestigen de instap: stemgewicht {support:.1f} voor, {oppose:.1f} tegen."), buy, sell, wait


def assess(bars, proposed, inputs, now, quote_time, context=None):
    context = context or {}
    weights = context.get("weights") or {}
    votes = [trend(bars), momentum(bars), volatility(bars), candle(bars), breakout(bars),
             pullback(bars), spike(bars), session(bars, now), bigtrend(context.get("hours")), risk(bars, inputs), skeptic(bars, proposed)]
    for v in votes:
        if v["agent"] in DIRECTIONAL:
            v["weight"] = round(float(weights.get(v["agent"], 1.0)), 2)
    action, reason, buy, sell, wait = recommend(proposed, votes, bool(context.get("learning")))
    return dict(version=POLICY, time=now, quoteTime=quote_time,
                candleTime=bars[-1].time if bars else None, candlesUsed=len(bars),
                proposed=proposed, recommendation=action, executed=False, reason=reason,
                buy=buy, sell=sell, wait=wait, votes=[dict(v, time=now) for v in votes])


def frozen_input(bars, risk_input, quote_price, model):
    return dict(candles=[asdict(b) for b in bars], risk=asdict(risk_input), quote=quote_price, model=model)
