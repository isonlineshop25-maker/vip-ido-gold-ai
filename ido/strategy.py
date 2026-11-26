"""Causal trend rules; variant selection on past, separately tested outcomes."""

from functools import lru_cache

from costs import COSTS
from market import GOLD, SIM

START_BALANCE = 50000.0
STRATEGY = "SMA + RSI · richtingsfilter v3"
BASELINE = (5, 15)
VARIANTS = (BASELINE, (3, 10), (8, 20))
WARMUP = 20
HOLDOUT = 80
FIT = 160
MIN_TRADES = 8


def market_regime(bars):
    """Classify the last 16 visible closes, independently of the entry signal."""
    if len(bars) < 16:
        return None
    closes = [b["close"] for b in bars[-16:]]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    movement, drift = sum(abs(x) for x in changes), closes[-1] - closes[0]
    if not movement or abs(drift) / movement < .25 or abs(drift) / closes[0] < .0001:
        return "sideways"
    return "bullish" if drift > 0 else "bearish"


def signal(bars, variant=BASELINE):
    fast_n, slow_n = variant
    length = max(16, slow_n)
    if len(bars) < length:
        return "unavailable", None, None, None
    closes = [b["close"] for b in bars[-length:]]
    fast, slow = sum(closes[-fast_n:]) / fast_n, sum(closes[-slow_n:]) / slow_n
    changes = [b - a for a, b in zip(closes[-15:], closes[-14:])]
    gain, loss = sum(max(0, d) for d in changes), sum(max(0, -d) for d in changes)
    rsi = 100 * gain / (gain + loss) if gain + loss else 50
    regime = market_regime(bars)
    direction = ("long" if regime == "bullish" and fast > slow and 51 <= rsi <= 78 else
                 "short" if regime == "bearish" and fast < slow and 22 <= rsi <= 49 else "neutral")
    return direction, round(rsi, 1), round(fast, 2), round(slow, 2)


def metrics(pnls):
    up = sum(x for x in pnls if x > 0)
    down = -sum(x for x in pnls if x < 0)
    equity = peak = START_BALANCE
    drawdown = 0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(trades=len(pnls), winRate=round(100 * sum(x > 0 for x in pnls) / len(pnls), 1) if pnls else None,
                netPnl=round(up - down, 2), maxDrawdown=round(drawdown, 2),
                profitFactor=round(up / down, 2) if down else None)


def outcomes(values, start, end, variant, interval, cost):
    result = []
    for i in range(max(start, WARMUP), min(end, len(values))):
        past = values[i - WARMUP:i]
        current = values[i]
        if any(b[0] - a[0] != interval for a, b in zip(past, (*past[1:], current))):
            continue
        direction, *_ = signal([{"close": v[2]} for v in past], variant)
        if direction in ("long", "short"):
            _, opened, closed = current
            result.append(round((closed - opened) * (1 if direction == "long" else -1) - cost, 4))
    return result


def choose(values, start, end, interval, cost):
    best = BASELINE
    benchmark = outcomes(values, start, end, best, interval, cost)
    best_total = max(0, sum(benchmark))
    for variant in VARIANTS[1:]:
        trial = outcomes(values, start, end, variant, interval, cost)
        if len(trial) >= MIN_TRADES and sum(trial) > best_total:
            best, best_total = variant, sum(trial)
    return best


def variant_name(variant):
    return f"SMA {variant[0]}/{variant[1]} + RSI 14"


@lru_cache(maxsize=16)
def learned(values):
    end = len(values)
    default = dict(model=variant_name(BASELINE), state="baseline", candles=end, heldOutTrades=0, evaluatedAt=None)
    if end < WARMUP + FIT + 2 * HOLDOUT or COSTS is None:
        return BASELINE, default
    cost = sum(COSTS)
    check = end - 2 * HOLDOUT
    candidate = choose(values, check - FIT, check, 60, cost)
    if candidate == BASELINE:
        tested = [outcomes(values, start, start + HOLDOUT, BASELINE, 60, cost) for start in (check, check + HOLDOUT)]
        return BASELINE, dict(default, state="tested" if all(len(t) >= MIN_TRADES for t in tested) else "baseline",
                              heldOutTrades=sum(map(len, tested)), evaluatedAt=values[-1][0] + 60)
    blocks = [(outcomes(values, start, start + HOLDOUT, candidate, 60, cost),
               outcomes(values, start, start + HOLDOUT, BASELINE, 60, cost)) for start in (check, check + HOLDOUT)]
    use = all(len(test) >= MIN_TRADES and sum(test) > max(0, sum(baseline)) for test, baseline in blocks)
    winner = candidate if use else BASELINE
    return winner, dict(model=variant_name(winner), state="adapted" if use else "tested", candles=end,
                        heldOutTrades=sum(len(test) for test, _ in blocks), evaluatedAt=values[-1][0] + 60)


@lru_cache(maxsize=16)
def replay(values):
    if COSTS is None:
        return metrics([]), 0
    pnl, blocks = [], 0
    cost = sum(COSTS)
    for start in range(WARMUP + FIT, len(values) - HOLDOUT + 1, HOLDOUT):
        best = choose(values, start - FIT, start, 3600, cost)
        pnl.extend(outcomes(values, start, start + HOLDOUT, best, 3600, cost))
        blocks += 1
    return metrics(pnl), blocks


def analysis(market, provider, now, current=None):
    bars = market.analysis_bars(provider, now)
    status = market.status(provider, now)
    quote = market.execution_quote(provider, now)
    live_bars = market.entry_bars(provider, now) if provider == SIM else contiguous_recent(quote["bars"], now)
    _, historical = learned(market.learning_bars(provider, now))
    if current is not None and provider != SIM:
        direction, rsi, fast, slow, model_name = current(quote)
        learning = dict(historical, model=model_name, state="baseline")
    else:
        direction, rsi, fast, slow = signal(live_bars or [], BASELINE)
        learning = dict(historical, model=variant_name(BASELINE), state="disabled")
    if status in ("stale", "unavailable"):
        direction = "unavailable"
    summary = {
        "long": "Het korte gemiddelde ligt boven het lange. Het regelmodel ziet opwaarts momentum.",
        "short": "Het korte gemiddelde ligt onder het lange. Het regelmodel ziet neerwaarts momentum.",
        "neutral": "Zijwaartse markt of onvoldoende bevestiging van trend en momentum: WAIT.",
        "unavailable": "Er zijn onvoldoende actuele gegevens voor een signaal. Eventuele historische evaluatie blijft zichtbaar.",
    }
    result, blocks = replay(tuple((b["time"], b["open"], b["close"]) for b in bars))
    return dict(signal=direction, summary=summary[direction] + " Geen winstgarantie.",
                strategy=STRATEGY, sampleFrom=bars[0]["time"] if bars else None,
                sampleTo=bars[-1]["time"] + 3600 if bars else None, candlesAnalyzed=len(bars),
                metrics=result, testBlocks=blocks, learning=learning,
                historySource=("XAUS · goudhistorie (kan futures bevatten)" if provider == GOLD else
                               "Synthetische candles" if provider == SIM else "Twelve Data · XAU/USD"),
                provider=provider, status=status, rsi=rsi, fastAverage=fast, slowAverage=slow)


def contiguous_recent(bars, now):
    if not bars or not 60 <= now - bars[-1]["time"] <= 180:
        return []
    tail = bars[-30:]
    for i in range(len(tail) - 1, 0, -1):
        if tail[i]["time"] - tail[i - 1]["time"] != 60:
            return tail[i:]
    return tail
