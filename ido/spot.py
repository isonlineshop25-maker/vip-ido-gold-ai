"""Public, indicative XAU/USD spot quotes and separate gold research candles."""

import json
import math
from datetime import datetime
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request

GOLD = "gold_api"
DAY = 86400
LOOKBACK = 30 * DAY
SPOT_URL = "https://api.gold-api.com/price/XAU"
HISTORY_URL = "https://xaus.com/api/v1/chart?symbol=xau&range=3mo&interval=1h"


class FeedError(ValueError):
    pass


def read_json(opener, url, headers=None):
    request = Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with opener.open(request, timeout=6) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise FeedError("Respons te groot")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise FeedError("Onverwacht feedformaat")
        return data
    except HTTPError as exc:
        raise FeedError({401: "Feedtoegang geweigerd", 403: "Feedtoegang geweigerd",
                         429: "Verzoeklimiet bereikt", 503: "Dataprovider onbeschikbaar"}.get(exc.code, "Dataprovider gaf een HTTP-fout")) from None
    except (OSError, HTTPException):
        raise FeedError("Dataprovider niet bereikbaar") from None
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise FeedError("Ongeldige feedrespons") from None


def price(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1_000_000:
        raise ValueError("Ongeldige prijs")
    return round(value, 6)


def decode_spot(body, now):
    try:
        if body["symbol"] != "XAU" or body["currency"] != "USD":
            raise ValueError
        date = datetime.fromisoformat(body["updatedAt"])
        if date.utcoffset() is None:
            raise ValueError
        ts = int(date.timestamp())
        if ts <= 0 or ts > now:
            raise ValueError
        return ts, price(body["price"])
    except (KeyError, ValueError, TypeError, OverflowError):
        raise FeedError("Ongeldige XAU/USD-prijs of tijdstempel") from None


def decode_history(body, now):
    try:
        if (body["symbol"], body["interval"], body["currency"]) != ("xau", "1h", "USD"):
            raise ValueError
        rows = {}
        for item in body["points"]:
            ts = item["t"]
            # Ignore irregular 'current quote' points and unclosed/future bars.
            if type(ts) is not int or ts % 3600 or not now - LOOKBACK <= ts <= now - 3600:
                continue
            o, h, l, c = (price(item[k]) for k in "ohlc")
            if not l <= min(o,c) <= max(o,c) <= h:
                raise ValueError
            rows[ts] = dict(time=ts, open=o, high=h, low=l, close=c)
        if not rows:
            raise ValueError
        return [rows[k] for k in sorted(rows)]
    except (KeyError, TypeError, ValueError, OverflowError):
        raise FeedError("Geen geldige gesloten goudcandles") from None


def init_spot(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS spot_quotes (
            time INTEGER PRIMARY KEY, price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS research_candles (
            time INTEGER PRIMARY KEY, open REAL NOT NULL, high REAL NOT NULL,
            low REAL NOT NULL, close REAL NOT NULL
        );
    """)


def saved_bars(db, since):
    rows = {}
    for ts, p in db.execute("SELECT time,price FROM spot_quotes WHERE time>=? ORDER BY time", (since,)):
        bucket = ts // 60 * 60
        if bucket not in rows:
            rows[bucket] = dict(time=bucket, open=p, high=p, low=p, close=p)
        else:
            b = rows[bucket]
            b.update(high=max(b["high"], p), low=min(b["low"], p), close=p)
    return list(rows.values())


def save_spot(db, ts, p, now):
    db.execute("INSERT OR IGNORE INTO spot_quotes VALUES(?,?)", (ts, p))
    db.execute("DELETE FROM spot_quotes WHERE time<?", (now - LOOKBACK - 86400,))


def save_history(db, bars, now):
    db.executemany("INSERT OR REPLACE INTO research_candles VALUES(?,?,?,?,?)",
                   [(b["time"], b["open"], b["high"], b["low"], b["close"]) for b in bars])
    db.execute("DELETE FROM research_candles WHERE time<?", (now - LOOKBACK,))
