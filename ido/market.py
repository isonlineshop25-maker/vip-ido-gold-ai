"""Read-only quote feeds; synthetic data is explicitly opt-in."""

import math
import os
import random
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.parse import urlencode
from books import rollup

from spot import (GOLD, HISTORY_URL, LOOKBACK, SPOT_URL, FeedError, decode_history,
                  decode_spot, read_json, save_history, save_spot, saved_bars)

SIM = "simulation"
REAL = "twelve_data"
PROVIDERS = (GOLD, REAL, SIM)
RANGES = {"1h": (60, 3600), "1d": (900, 86400), "7d": (3600, 604800), "30d": (14400, 2592000)}
ANALYSIS_STEP = 3600


def candle(ts, o, h, l, c):
    return dict(time=ts, open=round(o, 3), high=round(h, 3), low=round(l, 3), close=round(c, 3))


def sim_price(ts):
    tick = ts // 5
    jitter = random.Random(tick).uniform(-0.38, 0.38)
    return 2700 + 46 * math.sin(tick / 51433) + 18 * math.sin(tick / 3801) + 10 * math.sin(tick / 840) + 4 * math.sin(tick / 62) + 0.95 * math.sin(tick / 8) + jitter


@lru_cache(maxsize=10000)
def sim_bar(ts, step, until):
    # Minute bars capture every generated tick; larger bars sample their envelope.
    stride = max(5, step // 25 // 5 * 5)
    samples = [sim_price(x) for x in range(ts, until, stride)]
    samples.append(sim_price(until))
    return candle(ts, samples[0], max(samples), min(samples), samples[-1])


def sim_series(now, step, span):
    first = ((now - span) // step + 1) * step
    return [sim_bar(ts, step, min(ts + step, now // 5 * 5)) for ts in range(first, now + 1, step)]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Market:
    def __init__(self):
        self.key = os.getenv("IDO_TWELVE_DATA_KEY", "").strip()
        self.lock = threading.Lock()
        self.cache = {}
        self.attempted = {}
        self.checked = {}
        self.spot = None
        self.spot_bars = []
        self.research_bars = []
        self.opener = build_opener(NoRedirect())

    def restore(self, db, now):
        bars = saved_bars(db, now - LOOKBACK)
        research = [dict(row) for row in db.execute("SELECT * FROM research_candles WHERE time>=? ORDER BY time", (now - LOOKBACK,))]
        with self.lock:
            self.spot_bars, self.research_bars = bars, research
        # Stored prices can be charted, but are never re-used as a fresh fill after boot.

    def _due(self, key, ttl):
        now = time.monotonic()
        if key in self.attempted and now - self.attempted[key] < ttl:
            return False
        self.attempted[key] = now
        return True

    def _refresh_spot(self, db):
        if self._due(GOLD, 30):
            try:
                body = read_json(self.opener, SPOT_URL)
                now = int(time.time())
                ts, p = decode_spot(body, now)
                with self.lock:
                    previous = self.spot
                if previous and (ts < previous[0] or (ts == previous[0] and p != previous[1])):
                    raise FeedError("Koersvolgorde of tijdstempel klopt niet")
                with db:
                    if now - ts <= 120:
                        save_spot(db, ts, p, now)
                with self.lock:
                    self.spot = (ts, p)
                    self.checked[GOLD] = (now, True, None)
                    if now - ts <= 120 and previous != (ts, p):
                        self._ingest_sample(ts, p, now)
            except FeedError as exc:
                with self.lock:
                    self.checked[GOLD] = (int(time.time()), False, str(exc))

        if self._due("research", 900):
            try:
                body = read_json(self.opener, HISTORY_URL)
                now = int(time.time())
                bars = decode_history(body, now)
                with db:
                    save_history(db, bars, now)
                    retained = [dict(row) for row in db.execute("SELECT * FROM research_candles ORDER BY time")]
                with self.lock:
                    self.research_bars = retained
                    self.checked["research"] = (now, True, None)
            except FeedError as exc:
                with self.lock:
                    self.checked["research"] = (int(time.time()), False, str(exc))

    def _ingest_sample(self, ts, p, now):
        bucket = ts // 60 * 60
        bars = self.spot_bars
        if bars and bars[-1]["time"] == bucket:
            prev = bars[-1]
            self.spot_bars = bars[:-1] + [dict(prev, high=max(prev["high"], p), low=min(prev["low"], p), close=p)]
        elif not bars or bucket > bars[-1]["time"]:
            self.spot_bars = bars + [dict(time=bucket, open=p, high=p, low=p, close=p)]
        while self.spot_bars and self.spot_bars[0]["time"] < now - LOOKBACK:
            self.spot_bars = self.spot_bars[1:]

    def refresh(self, db=None):
        if db is not None:
            self._refresh_spot(db)
        if not self.key:
            return
        for seconds, interval, size, ttl in [(60, "1min", 180, 60), (3600, "1h", 750, 300)]:
            now = int(time.time())
            if not self._due(seconds, ttl):
                continue
            query = urlencode(dict(symbol="XAU/USD", interval=interval, outputsize=size, timezone="UTC"))
            bars = None
            error = "Geen geldige XAU/USD-candles ontvangen"
            try:
                body = read_json(self.opener, "https://api.twelvedata.com/time_series?" + query,
                                 {"Authorization": f"apikey {self.key}"})
                if body.get("status") == "error":
                    error = {401: "Feedtoegang geweigerd", 403: "XAU/USD niet beschikbaar met dit abonnement",
                             429: "Verzoeklimiet bereikt"}.get(body.get("code"), error)
                    continue
                if body.get("meta", {}).get("symbol") != "XAU/USD":
                    continue
                rows = {}
                for item in body["values"]:
                    ts = int(datetime.fromisoformat(item["datetime"]).replace(tzinfo=timezone.utc).timestamp())
                    o, h, l, c = [float(item[k]) for k in ["open", "high", "low", "close"]]
                    if ts <= now + 5 and ts % seconds == 0 and all(math.isfinite(x) and 0 < x < 1_000_000 for x in (o,h,l,c)) and l <= min(o,c) and h >= max(o,c):
                        rows[ts] = candle(ts, o, h, l, c)
                if rows:
                    bars = sorted(rows.values(), key=lambda b: b["time"])
            except FeedError as exc:
                error = str(exc)
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                # Do not emit provider headers, URL, response body or exceptions.
                continue
            finally:
                finished = int(time.time())
                with self.lock:
                    if bars:
                        self.cache[seconds] = (finished, bars)
                    self.checked[seconds] = (finished, bool(bars), None if bars else error)

    def _read(self, step):
        with self.lock:
            return self.cache.get(step, (None, []))

    def status(self, provider, now):
        if provider == SIM:
            return "simulated"
        return self.connection_check(now, provider)["status"].replace("waiting", "unavailable").replace("not_configured", "unavailable")

    @staticmethod
    def _minute_status(fetched, rows, now):
        if not rows:
            return "unavailable"
        return "connected" if 0 <= now - fetched <= 150 and 0 <= now - rows[-1]["time"] <= 180 else "stale"

    def connection_check(self, now, provider=REAL):
        with self.lock:
            if provider == GOLD:
                ts = self.spot[0] if self.spot else None
                checked_at, ok, error = self.checked.get(GOLD, (None, None, None))
                hist_at, _, hist_error = self.checked.get("research", (None, None, None))
                status = "waiting" if checked_at is None else "unavailable" if ts is None else (
                    "connected" if ok and 0 <= now - ts <= 120 else "stale")
                if status == "stale" and not error:
                    error = "Geen nieuwe spotprijs binnen 120 seconden"
                return dict(status=status, lastCheckAt=checked_at, lastCheckOk=ok,
                            lastCandleAt=self.spot_bars[-1]["time"] if self.spot_bars else None,
                            lastQuoteAt=ts, error=error, historyCheckedAt=hist_at, historyError=hist_error)
            fetched, rows = self.cache.get(60, (None, []))
            checked_at, check_ok, error = self.checked.get(60, (None, None, None))
            hist_at, _, hist_error = self.checked.get(3600, (None, None, None))
        status = "not_configured" if not self.key else "waiting" if checked_at is None else (
            "stale" if rows and not check_ok else self._minute_status(fetched, rows, now))
        ts = rows[-1]["time"] if rows else None
        return dict(status=status, lastCheckAt=checked_at, lastCheckOk=check_ok,
                    lastCandleAt=ts, lastQuoteAt=ts, error=error or ("Geen recente minuutcandle" if status == "stale" else None),
                    historyCheckedAt=hist_at, historyError=hist_error)

    def mark(self, provider, now):
        if provider == SIM:
            return round(sim_price(now), 3)
        if provider == GOLD:
            with self.lock:
                return self.spot[1] if self.spot else None
        _, rows = self._read(60)
        return rows[-1]["close"] if rows else None

    def execution_quote(self, provider, now):
        """Capture price, freshness and decision bars from one feed snapshot."""
        if provider == SIM:
            return dict(price=self.mark(provider, now), time=now // 5 * 5,
                        fresh=True, status="simulated", bars=None)
        if provider == GOLD:
            with self.lock:
                ts, p = self.spot or (None, None)
                ok = self.checked.get(GOLD, (None, None, None))[1]
                bars = self.spot_bars
            fresh = bool(ok and ts is not None and 0 <= now - ts <= 120)
            return dict(price=p, time=ts, fresh=fresh, status="connected" if fresh else "stale" if p else "unavailable",
                        bars=[b for b in bars[-90:] if ts and b["time"] + 60 <= ts][-30:])
        _, rows = self._read(60)
        last = rows[-1] if rows else None
        status = self.connection_check(now, provider)["status"]
        if status in ("waiting", "not_configured"):
            status = "unavailable"
        return dict(price=last["close"] if last else None, time=last["time"] if last else None,
                    fresh=status == "connected", status=status,
                    bars=[b for b in rows if now - 3600 <= b["time"] <= now - 60][-30:])

    def strategy_hours(self, provider, cutoff):
        if provider == SIM:
            rows = sim_series(cutoff, 3600, 32 * 3600)
        elif provider == GOLD:
            with self.lock:
                minutes = self.spot_bars[-(32 * 60 + 120):]
            return rollup(minutes, 60, 3600, cutoff)
        else:
            _, latest = self._read(3600)
            rows = latest[-34:]
        return [b for b in rows if b["time"] + 3600 <= cutoff]

    def analysis_bars(self, provider, now):
        if provider == SIM:
            return [b for b in sim_series(now, ANALYSIS_STEP, 2592000) if b["time"] + ANALYSIS_STEP <= now]
        if provider == GOLD:
            with self.lock:
                rows = self.research_bars
        else:
            _, rows = self._read(ANALYSIS_STEP)
        return [b for b in rows if now - 2592000 <= b["time"] <= now - ANALYSIS_STEP]

    def learning_bars(self, provider, now, cutoff=None):
        if provider != GOLD:
            return ()
        with self.lock:
            cutoff = min(cutoff if cutoff is not None else now, (self.spot or (now, None))[0])
            bars = self.spot_bars
        return tuple((b["time"], b["open"], b["close"]) for b in bars[-2000:]
                     if b["time"] + 60 <= cutoff and b["time"] >= now - LOOKBACK)

    def entry_bars(self, provider, now):
        if provider == SIM:
            return [b for b in sim_series(now, 60, 1800) if b["time"] + 60 <= now]
        _, rows = self._read(60)
        return [b for b in rows if now - 3600 <= b["time"] <= now - 60][-30:]

    def chart(self, provider, range_key, now):
        step, span = RANGES[range_key]
        source = "Simulatie · synthetisch" if provider == SIM else "Twelve Data · XAU/USD"
        if provider == SIM:
            rows = sim_series(now, step, span)
            updated = now // 5 * 5
        else:
            if provider == GOLD:
                with self.lock:
                    raw = self.spot_bars
                    source = "Gold API · vastgelegde XAU/USD-spotsamples"
                    updated = self.spot[0] if self.spot else None
            else:
                raw_step = 60 if range_key in ("1h", "1d") else 3600
                _, raw = self._read(raw_step)
                updated = raw[-1]["time"] if raw else None
            if provider == REAL and range_key == "1d":
                # Use minute data for recent updates, hour candles for the earlier part.
                fallback_updated, hours = self._read(3600)
                if hours:
                    step = 3600
                    updated = updated or fallback_updated
                    cut = raw[0]["time"] // step * step + step if raw else now + step
                    raw = [b for b in hours if b["time"] < cut] + [b for b in raw if b["time"] >= cut]
            elif provider == REAL and range_key in ("7d", "30d"):
                minute_updated, minute_rows = self._read(60)
                if minute_rows:
                    # Merge live minute candles into the latest hour only when the
                    # full minute prefix is available; never count it twice.
                    hour = minute_rows[-1]["time"] // 3600 * 3600
                    if minute_rows[0]["time"] <= hour:
                        raw = [b for b in raw if b["time"] < hour] + [b for b in minute_rows if b["time"] >= hour]
                        updated = max(updated or 0, minute_updated)
            buckets = {}
            for b in raw:
                ts = b["time"] // step * step
                if ts < (now-span)//step*step or ts > now:
                    continue
                prev = buckets.get(ts)
                buckets[ts] = candle(ts, prev["open"] if prev else b["open"],
                                     max(prev["high"], b["high"]) if prev else b["high"],
                                     min(prev["low"], b["low"]) if prev else b["low"], b["close"])
            rows = sorted(buckets.values(), key=lambda b: b["time"])
        return dict(symbol="XAU/USD", provider=provider, status=self.status(provider, now),
                    intervalSeconds=step,
                    windowStart=(now-span)//step*step, windowEnd=(now//step+1)*step,
                    source=source,
                    interval={60: "1m", 900: "15m", 3600: "1u", 14400: "4u"}[step],
                    updatedAt=updated, candles=rows)
