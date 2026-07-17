#!/usr/bin/env python3
"""
Market discovery for Polymarket Up/Down series across timeframes and assets.

Slug conventions observed on Polymarket (validated via probe_markets.py):
  5m  : btc-updown-5m-<unix slot start>      (300s buckets, UTC-aligned)
  15m : btc-updown-15m-<unix slot start>     (900s buckets, UTC-aligned)
  1h  : btc-updown-1h-<unix slot start>      plus legacy ET-date slugs
  4h  : btc-updown-4h-<unix slot start>      (14400s buckets, UTC-aligned)
  1d  : btc-updown-1d-<ET midnight epoch>    plus legacy ET-date slugs

Because Polymarket has shipped slug variants over time (with/without year,
unix vs ET-date), resolution works from an ordered list of candidate
templates per timeframe and validates each against the Gamma API. Templates
are overridable per timeframe in the multi profiles config.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - very old python fallback
    ET = dt.timezone(dt.timedelta(hours=-5))

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

TIMEFRAME_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Ordered candidate templates per timeframe. First one that resolves wins.
# Placeholders: {asset} {asset_name} {tf} {bucket} {month} {day} {year}
#               {hour12} {ampm}
DEFAULT_SLUG_TEMPLATES: dict[str, list[str]] = {
    "5m": ["{asset}-updown-5m-{bucket}"],
    "15m": ["{asset}-updown-15m-{bucket}"],
    "1h": [
        "{asset}-updown-1h-{bucket}",
        # Year-qualified form first: the bare (no-year) slug collides with a
        # stale/closed market from a prior year sharing the same month-day,
        # confirmed in production probes (see multi/README.md market table).
        "{asset_name}-up-or-down-{month}-{day}-{year}-{hour12}{ampm}-et",
        "{asset_name}-up-or-down-{month}-{day}-{hour12}{ampm}-et",
    ],
    "4h": ["{asset}-updown-4h-{bucket}"],
    "1d": [
        "{asset}-updown-1d-{bucket}",
        # Daily markets run noon-to-noon ET and are named for the END
        # (resolution) day — e.g. the market active at 8pm ET July 14 is
        # "bitcoin-up-or-down-on-july-15-2026" ending noon ET July 15.
        "{asset_name}-up-or-down-on-{end_month}-{end_day}-{end_year}",
        "{asset_name}-up-or-down-{end_month}-{end_day}-{end_year}",
        "{asset_name}-up-or-down-on-{end_month}-{end_day}",
        "{asset_name}-up-or-down-{end_month}-{end_day}",
    ],
}

ASSET_NAMES: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
    "doge": "dogecoin",
}


def timeframe_seconds(tf: str) -> int:
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"unknown timeframe: {tf} (known: {sorted(TIMEFRAME_SECONDS)})")
    return TIMEFRAME_SECONDS[tf]


def slot_bounds(tf: str, ts: Optional[float] = None) -> tuple[int, int]:
    """Return (slot_start, slot_end) unix timestamps for the slot containing ts."""
    t = int(ts if ts is not None else time.time())
    dur = timeframe_seconds(tf)
    if tf == "1d":
        # Daily markets run NOON-to-NOON Eastern Time (DST-aware), confirmed
        # by live probe: "bitcoin-up-or-down-on-july-15-2026" ends 12:00 ET.
        # The market is named for the day it RESOLVES (the end boundary).
        et_now = dt.datetime.fromtimestamp(t, ET)
        noon = et_now.replace(hour=12, minute=0, second=0, microsecond=0)
        start_et = noon if et_now >= noon else noon - dt.timedelta(days=1)
        end_et = start_et + dt.timedelta(days=1)
        return int(start_et.timestamp()), int(end_et.timestamp())
    start = t - t % dur
    return start, start + dur


def _slot_end_ts(tf: str, slot_start: int) -> int:
    """End timestamp for a slot given its start (ET-aware for 1d)."""
    if tf == "1d":
        start_et = dt.datetime.fromtimestamp(slot_start, ET)
        return int((start_et + dt.timedelta(days=1)).timestamp())
    return slot_start + timeframe_seconds(tf)


def next_slot_bounds(tf: str, ts: Optional[float] = None) -> tuple[int, int]:
    _, end = slot_bounds(tf, ts)
    return slot_bounds(tf, end + 1)


def _fmt_map(asset: str, tf: str, slot_start: int) -> dict[str, Any]:
    et_start = dt.datetime.fromtimestamp(slot_start, ET)
    et_end = dt.datetime.fromtimestamp(_slot_end_ts(tf, slot_start), ET)
    return {
        "asset": asset.lower(),
        "asset_name": ASSET_NAMES.get(asset.lower(), asset.lower()),
        "tf": tf,
        "bucket": slot_start,
        # start-boundary date/time (hourly markets are named by start hour)
        "month": et_start.strftime("%B").lower(),
        "day": et_start.day,
        "year": et_start.year,
        "hour12": int(et_start.strftime("%I")),
        "ampm": et_start.strftime("%p").lower(),
        # end-boundary date (daily markets are named by resolution day)
        "end_month": et_end.strftime("%B").lower(),
        "end_day": et_end.day,
        "end_year": et_end.year,
    }


def candidate_slugs(
    asset: str,
    tf: str,
    slot_start: int,
    templates: Optional[list[str]] = None,
) -> list[str]:
    tpls = templates or DEFAULT_SLUG_TEMPLATES.get(tf) or []
    fmt = _fmt_map(asset, tf, slot_start)
    out = []
    for tpl in tpls:
        try:
            out.append(tpl.format(**fmt))
        except (KeyError, IndexError):
            continue
    return out


def fetch_event(slug: str, timeout: float = 12.0) -> Optional[dict[str, Any]]:
    r = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=timeout)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None


def parse_json_field(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def market_side_info(market: dict[str, Any]) -> dict[str, Any]:
    """Extract UP/DOWN gamma prices and CLOB token ids from a gamma market."""
    outcomes = parse_json_field(market.get("outcomes")) or []
    prices = parse_json_field(market.get("outcomePrices")) or []
    token_ids = parse_json_field(market.get("clobTokenIds")) or []
    if len(prices) < 2 or len(token_ids) < 2:
        raise RuntimeError("missing outcomePrices/clobTokenIds")

    up_i, down_i = 0, 1
    labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
    if len(labs) >= 2 and ("up" in labs[1] or "yes" in labs[1]):
        up_i, down_i = 1, 0

    return {
        "up_price": float(prices[up_i]),
        "down_price": float(prices[down_i]),
        "up_token": str(token_ids[up_i]),
        "down_token": str(token_ids[down_i]),
    }


def _end_ts_of(market: dict[str, Any]) -> Optional[float]:
    end_iso = str(market.get("endDate") or market.get("endDateIso") or "")
    try:
        return dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def validate_and_annotate(
    market: dict[str, Any],
    slug: str,
    slot_start: int,
    min_seconds_left: float = 5.0,
) -> Optional[dict[str, Any]]:
    if market.get("closed") is True:
        return None
    if market.get("active") is False:
        return None
    end_ts = _end_ts_of(market)
    if end_ts is None:
        return None
    sec_left = end_ts - time.time()
    if sec_left <= min_seconds_left:
        return None
    m = dict(market)
    m["_slug"] = slug
    m["_slot_start"] = slot_start
    m["_end_ts"] = end_ts
    m["_seconds_left"] = sec_left
    try:
        m.update(market_side_info(market))
    except Exception:
        return None
    return m


def resolve_market(
    asset: str,
    tf: str,
    slot: str = "current",
    templates: Optional[list[str]] = None,
    min_seconds_left: float = 5.0,
) -> Optional[dict[str, Any]]:
    """Resolve the active Up/Down market for (asset, timeframe) in the given slot."""
    if slot == "next":
        slot_start, _ = next_slot_bounds(tf)
    else:
        slot_start, _ = slot_bounds(tf)

    for slug in candidate_slugs(asset, tf, slot_start, templates):
        try:
            ev = fetch_event(slug)
        except Exception:
            continue
        if not ev:
            continue
        mkts = ev.get("markets") or []
        if not mkts:
            continue
        m = validate_and_annotate(mkts[0], slug, slot_start, min_seconds_left)
        if m is not None:
            return m
    return None


def get_side_price(slug: str, side: str) -> Optional[float]:
    """Current gamma outcome price for one side of a market (og-compatible)."""
    try:
        ev = fetch_event(slug)
        if not ev:
            return None
        mkts = ev.get("markets") or []
        if not mkts:
            return None
        info = market_side_info(mkts[0])
        return info["up_price"] if side == "UP" else info["down_price"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Chainlink Data Streams (credential-gated, sub-second): the markets' actual
# resolution feed. When CHAINLINK_STREAMS_API_KEY/SECRET and a per-asset
# CHAINLINK_FEED_ID_<ASSET> are set (e.g. in multi/.env), Streams becomes the
# PRIMARY spot provider for signals and settlement; Binance/Kraken remain the
# no-credential fallbacks. Verify credentials with: multibot_ctl.sh streams
# ---------------------------------------------------------------------------

STREAMS_BASE = os.getenv("CHAINLINK_STREAMS_BASE", "https://api.dataengine.chain.link")


def streams_creds() -> Optional[tuple[str, str]]:
    k = os.getenv("CHAINLINK_STREAMS_API_KEY")
    s = os.getenv("CHAINLINK_STREAMS_API_SECRET")
    return (k, s) if k and s else None


def streams_feed_id(asset: str) -> Optional[str]:
    return os.getenv(f"CHAINLINK_FEED_ID_{asset.upper()}")


def streams_get(path: str, params: dict | None = None, timeout: float = 8.0,
                api_key: Optional[str] = None) -> dict:
    """HMAC-authenticated GET against the Data Streams API (per the Streams
    auth spec: sign 'METHOD path sha256(body) clientId timestampMs').
    api_key overrides the client id (e.g. the candlestick API key) while
    signing with the same shared secret."""
    creds = streams_creds()
    if not creds:
        raise RuntimeError("Chainlink Streams credentials not configured")
    key, secret = creds
    if api_key:
        key = api_key
    qs = urlencode(params or {})
    full_path = f"{path}?{qs}" if qs else path
    ts_ms = int(time.time() * 1000)
    body_hash = hashlib.sha256(b"").hexdigest()
    to_sign = f"GET {full_path} {body_hash} {key} {ts_ms}"
    sig = hmac.new(secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    r = requests.get(STREAMS_BASE + full_path, headers={
        "Authorization": key,
        "X-Authorization-Timestamp": str(ts_ms),
        "X-Authorization-Signature-SHA256": sig,
    }, timeout=timeout)
    r.raise_for_status()
    return r.json()


def decode_v3_report(full_report_hex: str) -> dict:
    """Decode a Streams v3 report blob without web3: outer ABI wraps
    (bytes32[3] ctx, bytes reportData, ...); reportData is 9 static words:
    feedId, validFrom, observationsTs, nativeFee, linkFee, expiresAt,
    price(int192), bid(int192), ask(int192) — 18-decimal fixed point."""
    b = bytes.fromhex(full_report_hex.removeprefix("0x"))
    off = int.from_bytes(b[96:128], "big")
    ln = int.from_bytes(b[off:off + 32], "big")
    rep = b[off + 32:off + 32 + ln]
    if len(rep) < 9 * 32:
        raise ValueError("short v3 report")

    def word(i: int, signed: bool = False) -> int:
        return int.from_bytes(rep[32 * i:32 * (i + 1)], "big", signed=signed)

    return {
        "feed_id": "0x" + rep[0:32].hex(),
        "valid_from": word(1),
        "observations_ts": word(2),
        "expires_at": word(5),
        "price": word(6, signed=True) / 1e18,
        "bid": word(7, signed=True) / 1e18,
        "ask": word(8, signed=True) / 1e18,
    }


def _streams_extract(j: dict) -> Optional[dict]:
    rep = j.get("report")
    if rep is None:
        reps = j.get("reports") or []
        rep = reps[0] if reps else None
    if not rep or not rep.get("fullReport"):
        return None
    return decode_v3_report(rep["fullReport"])


def streams_latest(asset: str) -> Optional[dict]:
    feed = streams_feed_id(asset)
    if not feed:
        return None
    j = streams_get("/api/v1/reports/latest", {"feedID": feed})
    return _streams_extract(j)


def streams_at(asset: str, ts: int) -> Optional[dict]:
    feed = streams_feed_id(asset)
    if not feed:
        return None
    j = streams_get("/api/v1/reports", {"feedID": feed, "timestamp": int(ts)})
    return _streams_extract(j)


# ---------------------------------------------------------------------------
# Chainlink Candlestick API (Bearer-auth, separate host from Streams):
# OHLC history at 1m resolution. Used for slot-boundary OPEN prices —
# resolution-network-sourced data for settlement without the mainnet Streams
# subscription. Set CHAINLINK_CANDLE_API_KEY (+ optional CHAINLINK_CANDLE_BASE
# once the probe identifies the working host).
# ---------------------------------------------------------------------------

CANDLE_BASE_MAINNET = "https://priceapi.dataengine.chain.link"
CANDLE_BASE_TESTNET = "https://priceapi.testnet-dataengine.chain.link"
CANDLE_SYMBOLS = {"btc": "BTCUSD", "eth": "ETHUSD", "sol": "SOLUSD",
                  "xrp": "XRPUSD", "doge": "DOGEUSD"}


def candle_api_key() -> Optional[str]:
    return os.getenv("CHAINLINK_CANDLE_API_KEY") or os.getenv("CHAINLINK_STREAMS_API_KEY")


def candle_base() -> Optional[str]:
    return os.getenv("CHAINLINK_CANDLE_BASE")


def candle_login() -> Optional[str]:
    return os.getenv("CHAINLINK_CANDLE_LOGIN") or os.getenv("CHAINLINK_STREAMS_API_KEY")


_candle_tok: dict[str, tuple[str, float]] = {}


def candles_authorize(base: Optional[str] = None, login: Optional[str] = None,
                      password: Optional[str] = None) -> str:
    """POST /api/v1/authorize (login+password form) -> Bearer JWT, cached
    until shortly before its exp claim."""
    b = base or candle_base() or CANDLE_BASE_TESTNET
    lg = login or candle_login()
    pw = password or os.getenv("CHAINLINK_CANDLE_API_KEY")
    if not lg or not pw:
        raise RuntimeError("candlestick login/password not configured")
    ck = f"{b}|{lg}"
    cached = _candle_tok.get(ck)
    if cached and cached[1] > time.time() + 30:
        return cached[0]
    r = requests.post(f"{b}/api/v1/authorize",
                      data={"login": lg, "password": pw},
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      timeout=8)
    r.raise_for_status()
    j = r.json()
    token = None
    for src in (j, j.get("d") if isinstance(j.get("d"), dict) else {}):
        for k in ("access_token", "accessToken", "token", "jwt"):
            v = (src or {}).get(k)
            if isinstance(v, str) and v.count(".") == 2:
                token = v
                break
        if token:
            break
    if not token:
        raise RuntimeError(f"no token in authorize response: {str(j)[:160]}")
    exp = time.time() + 540
    try:
        import base64
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        exp = float(json.loads(base64.urlsafe_b64decode(seg)).get("exp", exp))
    except Exception:
        pass
    _candle_tok[ck] = (token, exp)
    return token


def candles_history(symbol: str, resolution: str, frm: int, to: int,
                    base: Optional[str] = None, login: Optional[str] = None,
                    password: Optional[str] = None) -> dict:
    b = base or candle_base() or CANDLE_BASE_TESTNET
    for attempt in (1, 2):
        token = candles_authorize(b, login, password)
        r = requests.get(f"{b}/api/v1/history/rows",
                         params={"symbol": symbol, "resolution": resolution,
                                 "from": int(frm), "to": int(to)},
                         headers={"Authorization": f"Bearer {token}"}, timeout=8)
        if r.status_code == 401 and attempt == 1:
            _candle_tok.pop(f"{b}|{login or candle_login()}", None)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def _candle_first_open(j: dict, min_ts: int) -> Optional[float]:
    """Parse either columnar ({'t':[...],'o':[...]}) or row-list
    ({'candles':[{'t':..,'o':..}]}) shapes; return the open of the first
    candle at/after min_ts."""
    ts_list, opens = None, None
    if isinstance(j, dict):
        if isinstance(j.get("t"), list) and isinstance(j.get("o"), list):
            ts_list, opens = j["t"], j["o"]
        else:
            rows = j.get("candles") or j.get("rows") or j.get("data")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                ts_list = [r.get("t") or r.get("time") or r.get("timestamp") for r in rows]
                opens = [r.get("o") or r.get("open") for r in rows]
    if not ts_list or not opens:
        return None
    for t, o in zip(ts_list, opens):
        try:
            if o is not None and float(t) >= min_ts:
                return float(o)
        except (TypeError, ValueError):
            continue
    try:  # fall back to the first parseable open
        return float(next(o for o in opens if o is not None))
    except (StopIteration, TypeError, ValueError):
        return None


def candle_open_at(asset: str, ts: int) -> Optional[float]:
    sym = CANDLE_SYMBOLS.get(asset.lower())
    if not sym or not (candle_base() and candle_api_key()):
        return None
    j = candles_history(sym, "1m", int(ts), int(ts) + 120)
    return _candle_first_open(j, int(ts))


# ---------------------------------------------------------------------------
# Spot price feed (for move-size filters). Chainlink Streams (when creds are
# configured) is the markets' actual resolution feed; the Candlestick API
# serves slot-boundary opens; Binance/Kraken are the no-credential fallbacks.
# Both open-price and live-price come from the SAME provider so the basis
# cancels.
# ---------------------------------------------------------------------------

SPOT_SYMBOLS = {
    "btc": {"binance": "BTCUSDT", "kraken": "XBTUSDT"},
    "eth": {"binance": "ETHUSDT", "kraken": "ETHUSDT"},
    "sol": {"binance": "SOLUSDT", "kraken": "SOLUSDT"},
    "xrp": {"binance": "XRPUSDT", "kraken": "XRPUSDT"},
    "doge": {"binance": "DOGEUSDT", "kraken": "DOGEUSDT"},
}
_spot_provider: dict[str, str] = {}  # asset -> provider that worked


def _binance_open_at(sym: str, ts: int) -> float:
    r = requests.get("https://api.binance.com/api/v3/klines",
                     params={"symbol": sym, "interval": "1m",
                             "startTime": ts * 1000, "limit": 1}, timeout=8)
    r.raise_for_status()
    return float(r.json()[0][1])


def _binance_price(sym: str) -> float:
    r = requests.get("https://api.binance.com/api/v3/ticker/price",
                     params={"symbol": sym}, timeout=8)
    r.raise_for_status()
    return float(r.json()["price"])


def _kraken_open_at(sym: str, ts: int) -> float:
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": sym, "interval": 1, "since": ts - 60}, timeout=8)
    r.raise_for_status()
    res = r.json().get("result") or {}
    rows = next((v for k, v in res.items() if k != "last"), [])
    for row in rows:
        if int(row[0]) >= ts:
            return float(row[1])
    raise RuntimeError("kraken: no candle at slot start")


def _kraken_price(sym: str) -> float:
    r = requests.get("https://api.kraken.com/0/public/Ticker",
                     params={"pair": sym}, timeout=8)
    r.raise_for_status()
    res = r.json().get("result") or {}
    row = next(iter(res.values()), None)
    if not row:
        raise RuntimeError("kraken: empty ticker")
    return float(row["c"][0])


def _spot_call(asset: str, kind: str, ts: int = 0) -> Optional[float]:
    a = asset.lower()
    order = [_spot_provider.get(a)] if _spot_provider.get(a) else []
    if streams_creds() and streams_feed_id(a):
        order += [p for p in ("chainlink",) if p not in order]
    order += [p for p in ("binance", "kraken") if p not in order]
    syms = SPOT_SYMBOLS.get(a)
    # candle API serves boundary opens only (1m history) — try it for 'open'
    # right after Streams, before the exchange fallbacks
    if kind == "open" and candle_base() and candle_api_key():
        idx = 1 if "chainlink" in order else 0
        order.insert(idx, "cl_candles")
    for prov in order:
        try:
            if prov == "chainlink":
                rep = streams_at(a, ts) if kind == "open" else streams_latest(a)
                if rep is None or not rep.get("price"):
                    continue
                v = float(rep["price"])
            elif prov == "cl_candles":
                if kind != "open":
                    continue
                v = candle_open_at(a, ts)
                if v is None:
                    continue
                return v  # not cached as the sticky provider: opens-only
            elif not syms:
                continue
            elif kind == "open":
                v = (_binance_open_at if prov == "binance" else _kraken_open_at)(syms[prov], ts)
            else:
                v = (_binance_price if prov == "binance" else _kraken_price)(syms[prov])
            _spot_provider[a] = prov
            return v
        except Exception:
            continue
    return None


def spot_open_at(asset: str, slot_start: int) -> Optional[float]:
    """Spot open price at slot start (the reference the market resolves against)."""
    return _spot_call(asset, "open", slot_start)


def spot_price(asset: str) -> Optional[float]:
    return _spot_call(asset, "price")


# ---------------------------------------------------------------------------
# Public CLOB orderbook access (REST, no auth, no py_clob_client dependency)
# ---------------------------------------------------------------------------

def get_order_book(token_id: str, timeout: float = 10.0) -> dict[str, Any]:
    r = requests.get(f"{CLOB_BASE}/book", params={"token_id": str(token_id)}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def top_of_book(token_id: str) -> tuple[Optional[float], Optional[float], float, float]:
    """Return (best_bid, best_ask, best_bid_size, best_ask_size) for a token."""
    book = get_order_book(token_id)
    best_bid, best_ask = None, None
    bid_sz, ask_sz = 0.0, 0.0
    for b in book.get("bids") or []:
        try:
            p, s = float(b.get("price")), float(b.get("size"))
        except (TypeError, ValueError):
            continue
        if best_bid is None or p > best_bid:
            best_bid, bid_sz = p, s
    for a in book.get("asks") or []:
        try:
            p, s = float(a.get("price")), float(a.get("size"))
        except (TypeError, ValueError):
            continue
        if best_ask is None or p < best_ask:
            best_ask, ask_sz = p, s
    return best_bid, best_ask, bid_sz, ask_sz
