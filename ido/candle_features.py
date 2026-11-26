"""Point-in-time OHLC features. Callers supply only closed, contiguous candles."""

from statistics import mean

from advisors import ranges
from strategy import signal


def describe(bars):
    if not bars:
        return None
    b = bars[-1]
    body = abs(b.close - b.open)
    upper, lower = b.high - max(b.open, b.close), min(b.open, b.close) - b.low
    width = b.high - b.low
    doji = width > 0 and body <= 0.1 * width
    pin = ("up" if lower >= 0.6 * width and upper <= 0.22 * width else
           "down" if upper >= 0.6 * width and lower <= 0.22 * width else None) if width > 0 else None
    engulf = None
    if len(bars) >= 2:
        p = bars[-2]
        if p.close < p.open and b.close > b.open and b.open <= p.close and b.close >= p.open:
            engulf = "up"
        elif p.close > p.open and b.close < b.open and b.open >= p.close and b.close <= p.open:
            engulf = "down"
    context = bars[-21:-1]
    atr = mean(ranges(bars[-15:])) if len(bars) >= 15 else None
    _, rsi, fast, slow = signal([{"close": item.close} for item in bars])
    trend = None if fast is None else "up" if fast > slow else "down" if fast < slow else "flat"
    momentum = b.close - bars[-6].close if len(bars) >= 6 else None
    return dict(candleTime=b.time, count=len(bars), body=body, upperWick=upper, lowerWick=lower,
                engulfing=engulf, pinBar=pin, doji=doji, trend=trend, momentum=momentum, rsi=rsi, atr=atr,
                support=min(x.low for x in context) if context else None,
                resistance=max(x.high for x in context) if context else None)
