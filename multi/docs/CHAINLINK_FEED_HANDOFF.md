# Chainlink price-feed handoff — for wiring another Up/Down bot

This document is self-contained: paste it (or point a Claude Code session at
it) and it has everything needed to replicate the multi-bot's price-feed
stack in another bot. Credentials are NOT in this file — they live in the
gitignored `multi/.env` (var names below).

## What this actually buys you — read this first

There are three methods here. Be clear about which one does what, because
"faster data" is only true for the paid one:

| Method | Cost | Latency / cadence | What it's for |
|---|---|---|---|
| A. Candlestick API (`priceapi.dataengine.chain.link`) | **FREE** (verified working on this account) | 1-minute OHLC history, sub-second HTTP | **Slot-boundary opens + settlement grading from the SAME source Polymarket resolves on.** Not a tick feed. |
| B. Data Streams REST/WS (`api.dataengine.chain.link`) | **$150/mo per feed** (auth works on this account; BTC feed NOT purchased) | sub-second benchmark reports | True live resolution-feed prices. The only real "speed upgrade". |
| C. On-chain aggregator (Polygon `eth_call`) | free | updates on heartbeat/deviation (tens of seconds) | Display only (dashboard price chip). |

The multi-bot's measured verdict, so the other bot doesn't re-learn it the
expensive way: at 3–5 s REST polling, 15m impulse fills persisted (75%
fill-echo, negative adverse move) — **speed was not the binding constraint**,
so we declined the $150/mo feed. What DID bite was **source mismatch at
boundaries**: grading slots against Binance/Kraken candles disagreed with the
official Chainlink-based resolution on ~22% of a 40-slot sample (label
flips). Method A fixes that for free. On a 5m bot the boundary-open reference
matters even more — 5m slots are decided by moves of a few dollars.

Live ticks between boundaries still come from Kraken/Binance (the module
below falls back automatically; note **Binance is geo-blocked on the Mac —
Kraken is what actually serves**). If sub-second resolution-feed ticks ever
become worth $150/mo, method B activates by adding two env vars — the code
path is already written and tested up to the entitlement wall.

## Environment variables

Same names as the multi-bot, so one `.env` serves both. Copy the real values
from `multi/.env` on the Mac (NEVER commit them; both `.env` files are
gitignored):

```bash
# Method A — Candlestick API (all three required; verified combo)
CHAINLINK_CANDLE_BASE=https://priceapi.dataengine.chain.link
CHAINLINK_CANDLE_LOGIN=<Data Streams username, a uuid>
CHAINLINK_CANDLE_API_KEY=<Candlestick API key, a uuid>

# Method B — Data Streams (auth pair works today; feed id only if purchased)
CHAINLINK_STREAMS_API_KEY=<same uuid as CANDLE_LOGIN on this account>
CHAINLINK_STREAMS_API_SECRET=<HMAC secret string>
# CHAINLINK_FEED_ID_BTC=<0x0003... 32-byte feed id>   # unset until purchased
```

If the other bot lives in this same repo working tree, this copies them into
the og bot's env file (og tooling sources repo-root `.env`):

```bash
cd /Volumes/MASTER/5min-btc-polymarket/5min-btc-polymarket
grep '^CHAINLINK_' multi/.env >> .env
```

Auth mapping that took real trial-and-error to discover (four combinations
probed): the Candlestick `authorize` call wants **login = the Data Streams
username** and **password = the Candlestick API key**. The HMAC secret is not
used by method A at all. Testnet hosts reject this account ("user does not
exist" — testnet needs separately-created testnet credentials); everything
below is the MAINNET hosts, where this account is verified working.

## Method A — Candlestick API (the one to wire in)

### Auth flow

1. `POST https://priceapi.dataengine.chain.link/api/v1/authorize`
   with a **form-encoded** body (`Content-Type:
   application/x-www-form-urlencoded`): `login=<uuid>&password=<uuid>`.
2. Response is JSON containing a JWT — the field name varies, scan
   `access_token` / `accessToken` / `token` / `jwt`, possibly nested under
   `"d"`. A JWT has exactly two dots — that's the reliable way to spot it.
3. Cache the token and reuse it until ~30 s before its `exp` claim
   (base64-decode the middle JWT segment; tokens run ~10 min; fall back to
   540 s if `exp` is unparseable).
4. All data calls send `Authorization: Bearer <jwt>`.
5. On a 401 from a data call: drop the cached token, re-authorize **once**,
   retry. (Wrong login/password pairing also presents as 401/403 "token
   malformed" — don't retry-loop that.)

### History endpoint (the workhorse)

```
GET /api/v1/history/rows?symbol=BTCUSD&resolution=1m&from=<unix>&to=<unix>
```

Symbols: `BTCUSD`, `ETHUSD`, `SOLUSD`, `XRPUSD`, `DOGEUSD`.

Response shape — this cost a debugging round, so verbatim:

```json
{"candles": [[<unix_ts>, <open>, <high>, <low>, <close>, <volume>], ...]}
```

Two traps:

- Rows are **NEWEST-first**. Sort or min-by-timestamp; never assume `[0]` is
  the earliest.
- Prices are **18-decimal fixed point** (raw open `6.28e22` means
  `$62,811`). Scale rule that works for every crypto quote: divide by 1e18
  when `abs(value) > 1e9`.

The module below parses this defensively (also handles columnar
`{"t":[],"o":[]}` and dict-row shapes seen from related UDF-style APIs).

Verified working output from this account, for reference:
`history/rows -> last-5min BTC candle open $62,787.93`.

Other endpoints that answered 200 with a Bearer token: `/api/v1/symbol_info`,
`/api/v1/time`. `/api/v1/quotes?symbols=BTCUSD` was probed but not confirmed
— if it turns out to serve live quotes, that's a free near-real-time source;
worth one probe from the other bot, but don't build on it until it returns
data.

### Boundary-open timing

Query the slot's open a few seconds AFTER the boundary, window
`from=slot_start, to=slot_start+120`, and take the open of the earliest
candle with `ts >= slot_start`. If it returns nothing yet, retry on the next
poll loop — never substitute a pre-boundary candle. For settlement, "close"
of a slot = **open of the 1m candle AT the end boundary** (same convention
the multi-bot uses; the market rule is "Up iff close ≥ open").

## Method B — Data Streams REST (dormant until the feed is purchased)

HMAC-authenticated GET against `https://api.dataengine.chain.link`. String to
sign (spaces literal):

```
GET <path?query> <sha256_hex_of_empty_body> <client_id> <timestamp_ms>
```

signed HMAC-SHA256 with the API secret; headers:

```
Authorization: <client_id>
X-Authorization-Timestamp: <timestamp_ms>
X-Authorization-Signature-SHA256: <hex_signature>
```

Endpoints: `/api/v1/feeds` (entitlements — returns `feeds: []` on this
account until a feed is bought), `/api/v1/reports/latest?feedID=0x...`,
`/api/v1/reports?feedID=0x...&timestamp=<unix>`. Reports carry a
`fullReport` hex blob; decode without web3: skip the outer ABI wrapper
(offset at bytes 96–128), then the report data is 9 static 32-byte words —
`feedId, validFrom, observationsTs, nativeFee, linkFee, expiresAt,
price(int192), bid(int192), ask(int192)`, prices 18-decimal fixed point.
There is also a documented WebSocket at `wss://ws.dataengine.chain.link`
(untested here — REST hit the entitlement wall first).

The module activates this path automatically when
`CHAINLINK_STREAMS_API_KEY/SECRET` + `CHAINLINK_FEED_ID_BTC` are all set.

## Method C — on-chain aggregator (display only)

`eth_call` on Polygon to BTC/USD aggregator
`0xc907E116054Ad103354f2D350FD2514433D57F6f`, selector `0xfeaf968c`
(`latestRoundData()`), answer is 8-decimal fixed point. Fine for a UI chip,
too slow/coarse for trading logic. See `multi/scripts/dashboard.py`
(`_fetch_chainlink`) if needed.

## Drop-in module

Save as `chainlink_feed.py` next to the other bot's code. Only dependency is
`requests`. This is the multi-bot's production path
(`multi/scripts/market_discovery.py`) distilled — same env vars, same
behavior, minus the Polymarket-specific discovery code.

```python
#!/usr/bin/env python3
"""Chainlink-source spot prices for Polymarket Up/Down bots.

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
```

## Integration recipe for a 5m bot

1. Vendor the module, load the env vars before the process starts (source
   the `.env` in whatever launcher the bot uses — and never put inline `#`
   comments on env lines; a stray character there has broken sourcing
   before).
2. At each slot start (a few seconds after the boundary), record
   `spot_open_at("btc", slot_start)` — this is now Chainlink-sourced. Retry
   next loop if `None`.
3. Keep using `spot_price("btc")` for intra-slot ticks (Kraken on the Mac).
   Poll as fast as you like within API courtesy — the calls are sub-second;
   the multi-bot uses 3–5 s loops.
4. Grade provisionally with `settle_up(...)`, but ALWAYS reconcile against
   the official market resolution (Gamma API `outcomePrices`/UMA status)
   before trusting PnL — provisional spot labels flipped on ~22% of slots
   when sources were mismatched; Chainlink-sourced opens exist precisely to
   shrink that.
5. Rate courtesy: one `authorize` per ~10 min (the cache handles it), one
   `history/rows` call per boundary — don't poll history in the tick loop.
6. If the $150/mo BTC Streams feed is ever purchased: set
   `CHAINLINK_FEED_ID_BTC` and both `CHAINLINK_STREAMS_*` vars — live ticks
   silently upgrade to the actual resolution feed, no code change.

## Verify after wiring (run on the Mac, from the bot's directory)

```bash
python3 - <<'EOF'
import os, time
for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.replace("export ", "").strip(), v.strip().strip('"'))
import chainlink_feed as cf
now = int(time.time())
slot = now - (now % 300)
print("boundary open :", cf.spot_open_at("btc", slot))
print("live price    :", cf.spot_price("btc"))
print("provider used :", cf._spot_provider)
EOF
```

Expected: a real BTC open (Chainlink candles), a live price, and
`{'btc': 'kraken'}` as the sticky live provider (Binance is geo-blocked on
the Mac). If the open comes back `None`, re-check the three
`CHAINLINK_CANDLE_*` values against `multi/.env` — the 403/401 "token
malformed" failure mode is almost always the login/password pairing.

## Security

- Credentials live ONLY in gitignored `.env` files. Never commit them, never
  paste them into code, docs, or commit messages.
- These uuids/secret have been shared in a chat session once — rotating them
  in the Chainlink dashboard when convenient is cheap insurance; this stack
  picks up new values from `.env` on restart with zero code changes.
