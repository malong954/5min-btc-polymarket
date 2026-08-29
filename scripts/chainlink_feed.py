#!/usr/bin/env python3
"""Chainlink-source spot prices for Polymarket Up/Down bots.

Vendored from multi/docs/CHAINLINK_FEED_HANDOFF.md (branch
claude/btc-poly-parallel-testing-b2th32) — keep in sync with that doc.

Provider chain per call:
  live price : Streams (if creds+feed) -> Binance -> Kraken
  slot open  : Streams -> Candlestick API -> Binance -> Kraken
The Candlestick path never becomes the sticky provider (opens-only).

Env: CHAINLINK_CANDLE_BASE / CHAINLINK_CANDLE_LOGIN / CHAINLINK_CANDLE_API_KEY
     CHAINLINK_STREAMS_API_KEY / CHAINLINK_STREAMS_API_SECRET
     CHAINLINK_FEED_ID_BTC (0x..., only once a Streams feed is purchased)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Optional
from urllib.parse import urlencode

import requests

# --- Data Streams (HMAC) ---------------------------------------------------

STREAMS_BASE = os.getenv("CHAINLINK_STREAMS_BASE", "https://api.dataengine.chain.link")


def streams_creds() -> Optional[tuple[str, str]]:
    k = os.getenv("CHAINLINK_STREAMS_API_KEY")
    s = os.getenv("CHAINLINK_STREAMS_API_SECRET")
    return (k, s) if k and s else None


def streams_feed_id(asset: str) -> Optional[str]:
    return os.getenv(f"CHAINLINK_FEED_ID_{asset.upper()}")


def streams_get(path: str, params: dict | None = None, timeout: float = 8.0) -> dict:
    creds = streams_creds()
    if not creds:
        raise RuntimeError("Chainlink Streams credentials not configured")
    key, secret = creds
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
        "observations_ts": word(2),
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
    return _streams_extract(streams_get("/api/v1/reports/latest", {"feedID": feed}))


def streams_at(asset: str, ts: int) -> Optional[dict]:
    feed = streams_feed_id(asset)
    if not feed:
        return None
    return _streams_extract(
        streams_get("/api/v1/reports", {"feedID": feed, "timestamp": int(ts)}))


# --- Candlestick API (Bearer JWT) ------------------------------------------

CANDLE_BASE_MAINNET = "https://priceapi.dataengine.chain.link"
CANDLE_SYMBOLS = {"btc": "BTCUSD", "eth": "ETHUSD", "sol": "SOLUSD",
                  "xrp": "XRPUSD", "doge": "DOGEUSD"}


def candle_base() -> str:
    return os.getenv("CHAINLINK_CANDLE_BASE") or CANDLE_BASE_MAINNET


def candle_login() -> Optional[str]:
    return os.getenv("CHAINLINK_CANDLE_LOGIN") or os.getenv("CHAINLINK_STREAMS_API_KEY")


def candle_password() -> Optional[str]:
    return os.getenv("CHAINLINK_CANDLE_API_KEY")


_candle_tok: dict[str, tuple[str, float]] = {}


def candles_authorize() -> str:
    b, lg, pw = candle_base(), candle_login(), candle_password()
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


def candles_history(symbol: str, resolution: str, frm: int, to: int) -> dict:
    b = candle_base()
    for attempt in (1, 2):
        token = candles_authorize()
        r = requests.get(f"{b}/api/v1/history/rows",
                         params={"symbol": symbol, "resolution": resolution,
                                 "from": int(frm), "to": int(to)},
                         headers={"Authorization": f"Bearer {token}"}, timeout=8)
        if r.status_code == 401 and attempt == 1:
            _candle_tok.pop(f"{b}|{candle_login()}", None)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def _candle_first_open(j: dict, min_ts: int) -> Optional[float]:
    """Earliest candle open at/after min_ts. Handles array rows (NEWEST
    first, 18-decimal fixed point), dict rows, and columnar shapes."""
    ts_list, opens = None, None
    if isinstance(j, dict):
        if isinstance(j.get("t"), list) and isinstance(j.get("o"), list):
            ts_list, opens = j["t"], j["o"]
        else:
            rows = j.get("candles") or j.get("rows") or j.get("data")
            if isinstance(rows, list) and rows:
                if isinstance(rows[0], dict):
                    ts_list = [r.get("t") or r.get("time") or r.get("timestamp") for r in rows]
                    opens = [r.get("o") or r.get("open") for r in rows]
                elif isinstance(rows[0], (list, tuple)) and len(rows[0]) >= 2:
                    ts_list = [r[0] for r in rows]
                    opens = [r[1] for r in rows]
    if not ts_list or not opens:
        return None

    def scale(v: float) -> float:
        return v / 1e18 if abs(v) > 1e9 else v

    cands = []
    for t, o in zip(ts_list, opens):
        try:
            if o is not None:
                cands.append((float(t), scale(float(o))))
        except (TypeError, ValueError):
            continue
    if not cands:
        return None
    after = [c for c in cands if c[0] >= min_ts]
    if after:
        return min(after, key=lambda c: c[0])[1]
    return max(cands, key=lambda c: c[0])[1]


def candle_open_at(asset: str, ts: int) -> Optional[float]:
    sym = CANDLE_SYMBOLS.get(asset.lower())
    if not sym or not (candle_login() and candle_password()):
        return None
    j = candles_history(sym, "1m", int(ts), int(ts) + 120)
    return _candle_first_open(j, int(ts))


# --- Exchange fallbacks + provider chain -----------------------------------

SPOT_SYMBOLS = {
    "btc": {"binance": "BTCUSDT", "kraken": "XBTUSDT"},
    "eth": {"binance": "ETHUSDT", "kraken": "ETHUSDT"},
    "sol": {"binance": "SOLUSDT", "kraken": "SOLUSDT"},
    "xrp": {"binance": "XRPUSDT", "kraken": "XRPUSDT"},
    "doge": {"binance": "DOGEUSDT", "kraken": "DOGEUSDT"},
}
_spot_provider: dict[str, str] = {}


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
    if kind == "open" and candle_login() and candle_password():
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
                return v  # opens-only: never becomes the sticky provider
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
    """Open at a slot boundary — the reference the market resolves against."""
    return _spot_call(asset, "open", slot_start)


def spot_price(asset: str) -> Optional[float]:
    """Best-available live price (Streams if entitled, else Kraken/Binance)."""
    return _spot_call(asset, "price")


def settle_up(asset: str, slot_start: int, slot_end: int) -> Optional[bool]:
    """Provisional Up/Down grade: close := open of the candle AT slot_end.
    Market rule: Up iff close >= open. ALWAYS verify against the official
    Polymarket resolution afterward — this is a fast provisional label."""
    o = spot_open_at(asset, slot_start)
    c = spot_open_at(asset, slot_end)
    if o is None or c is None:
        return None
    return c >= o


def candle_creds_ok() -> bool:
    """True when the free Candlestick API (method A) is configured."""
    return bool(candle_login() and candle_password())
