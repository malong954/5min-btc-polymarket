#!/usr/bin/env python3
"""
Fetch REAL Limitless Exchange BTC/ETH/SOL/XRP up-down contract prices + the
oracle settle reference — the cross-venue sibling of scripts/btc_polymarket.py.

Why this exists (from four analyze reports on the Polymarket side):
  1. Our recorder settles off Binance spot and measured 7-46% label
     disagreement vs the official Chainlink resolution ("reference errors").
     Limitless exposes the SAME Chainlink data stream it settles on via
     `/markets/{slug}/oracle-candles` — a settle reference at the venue's own
     oracle, which is exactly the fix the reports keep recommending.
  2. Every Polymarket segment pays the ~1% taker overround + fees. Limitless
     pays MAKER REBATES in USDC, flipping that sign for a maker exit.

This module is the price/label FEED only — it does not trade. It mirrors
btc_polymarket.py's shape (current_slug / current_prices / resolved_outcome +
an oracle-candle settle) so the existing recorder + analyzer can consume a
Limitless market with no new plumbing once the live JSON shapes are confirmed.

API (public, no auth), reverse-engineered by moondevonyt/Limitless-Prediction-
Market-Bots and confirmed against the live OpenAPI at
https://api.limitless.exchange/api-json :
  REST base : https://api.limitless.exchange
  slugs     : GET /markets/active/slugs                 -> all active ids
  book      : GET /markets/{slug}/orderbook             -> YES book; NO = 1-YES
  detail    : GET /markets/{slug}                       -> tokens/prices/status
  candles   : GET /markets/{slug}/oracle-candles?interval=5m&from=&to=  (OHLCV)

Stdlib + requests only. Every network call goes through an injectable getter so
the parsing is unit-tested offline (scripts/test_btc_limitless.py). Blocked by
restricted egress (that's fine — this is validated live on a normal network).

CONFIRM-ON-FIRST-RUN: the exact orderbook JSON shape and the slug timestamp
convention are reverse-engineered, not guaranteed. `python3 scripts/
btc_limitless.py --discover btc` hits the live API, prints the raw shapes, and
tells you whether the built slug matched a live market — run that once on your
Mac and paste the output back so any field-name drift can be corrected before
the feed is wired into the recorder.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

import requests

REST = "https://api.limitless.exchange"
WINDOW_SLOT = {"5m": 300, "15m": 900}

# One keep-alive session (same latency argument as btc_polymarket.py).
_SESSION = requests.Session()


def _get_json(url: str, params: Optional[dict[str, Any]] = None, timeout: float = 8.0) -> Any:
    r = _SESSION.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def bucket_window(ts: int, window: str = "15m") -> int:
    slot = WINDOW_SLOT[window]
    return ts - (ts % slot)


def current_slug(now_ts: float, asset: str = "btc", window: str = "15m") -> str:
    """Best-guess slug of the asset's live up-down market, mirroring the
    Polymarket convention (`{asset}-updown-{window}-{bucket}`). Limitless slugs
    are reverse-engineered, so prefer resolve_active_slug() (which matches
    against the live /markets/active/slugs list) over trusting this directly."""
    return f"{asset.lower()}-updown-{window}-{bucket_window(int(now_ts), window)}"


def _to_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _best(levels: Any, side: str) -> tuple[Optional[float], Optional[float]]:
    """Best (price, size) from a list of order-book levels. Accepts the common
    shapes: [{"price":..,"size":..}], [[price,size], ...], or {"price","size"}.
    `side`='ask' takes the min price, 'bid' the max — so it is correct whichever
    order the venue returns levels in."""
    if levels is None:
        return None, None
    if isinstance(levels, dict):
        levels = [levels]
    better = min if side == "ask" else max
    best_p: Optional[float] = None
    best_sz: Optional[float] = None
    for lvl in levels:
        if isinstance(lvl, dict):
            p = _to_float(lvl.get("price") if "price" in lvl else lvl.get("p"))
            sz = _to_float(lvl.get("size") if "size" in lvl else lvl.get("s"))
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 1:
            p = _to_float(lvl[0])
            sz = _to_float(lvl[1]) if len(lvl) >= 2 else None
        else:
            continue
        if p is None:
            continue
        if best_p is None or better(p, best_p) == p:
            best_p, best_sz = p, sz
    return best_p, best_sz


def _yes_book(raw: dict[str, Any]) -> tuple[Any, Any]:
    """Pull (bids, asks) arrays out of an orderbook payload, tolerant of the
    likely key spellings (bids/asks, buy/sell, yesBids/yesAsks)."""
    def pick(*keys):
        for k in keys:
            if isinstance(raw.get(k), (list, dict)):
                return raw[k]
        return None
    bids = pick("bids", "buys", "buy", "yesBids", "bidLevels")
    asks = pick("asks", "sells", "sell", "yesAsks", "askLevels")
    return bids, asks


def prices_from_orderbook(raw: dict[str, Any]) -> dict[str, Optional[float]]:
    """Turn a Limitless YES orderbook into UP/DOWN asks + bids (+ sizes).

    Limitless returns only the YES book; "NO is 1 - YES". For an up-down market
    YES == UP, so DOWN is the complement of the YES book:
        UP   ask = best YES ask          UP   bid = best YES bid
        DOWN ask = 1 - best YES bid      DOWN bid = 1 - best YES ask
    (To BUY the DOWN side you take the other leg of a YES bid, hence 1 - YES_bid;
    this is the standard binary complement, and it is what makes the up+dn sum
    and overround directly comparable to the Polymarket side.)"""
    bids, asks = _yes_book(raw)
    y_ask, y_ask_sz = _best(asks, "ask")
    y_bid, y_bid_sz = _best(bids, "bid")
    comp = lambda p: (round(1.0 - p, 6) if p is not None else None)
    return {
        "UP": y_ask, "UP_size": y_ask_sz,
        "UP_bid": y_bid, "UP_bid_size": y_bid_sz,
        "DOWN": comp(y_bid), "DOWN_size": y_bid_sz,
        "DOWN_bid": comp(y_ask), "DOWN_bid_size": y_ask_sz,
    }


def resolve_active_slug(now_ts: float, asset: str = "btc", window: str = "15m",
                        getter: Callable[..., Any] = _get_json) -> Optional[str]:
    """Find the asset's CURRENT up-down slug from the live active-slugs list,
    instead of trusting the constructed one. Matches `{asset}-updown-{window}`
    and picks the slug whose trailing bucket timestamp is the current slot (or,
    failing an exact bucket match, the newest such slug)."""
    try:
        slugs = getter(f"{REST}/markets/active/slugs")
    except Exception:
        return None
    if isinstance(slugs, dict):
        slugs = slugs.get("slugs") or slugs.get("data") or []
    if not isinstance(slugs, list):
        return None
    pat = re.compile(rf"^{re.escape(asset.lower())}-updown-{re.escape(window)}-(\d+)$")
    cur = bucket_window(int(now_ts), window)
    matches: list[tuple[int, str]] = []
    for s in slugs:
        s = str(s)
        m = pat.match(s)
        if m:
            matches.append((int(m.group(1)), s))
    if not matches:
        return None
    for ts_, s in matches:
        if ts_ == cur:
            return s
    return max(matches, key=lambda x: x[0])[1]


def current_prices(now_ts: float, getter: Callable[..., Any] = _get_json,
                   asset: str = "btc", window: str = "15m",
                   slug: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Live UP/DOWN best-ask (and bid) prices for the current up-down market,
    or None if it can't be resolved. Same output keys as
    btc_polymarket.current_prices so the recorder can treat the two venues
    identically."""
    if slug is None:
        slug = resolve_active_slug(now_ts, asset, window, getter) or current_slug(now_ts, asset, window)
    try:
        raw = getter(f"{REST}/markets/{slug}/orderbook")
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    px = prices_from_orderbook(raw)
    return {"slug": slug, "venue": "limitless", **px}


def oracle_candles(slug: str, interval: str = "5m", frm: Optional[int] = None,
                   to: Optional[int] = None, getter: Callable[..., Any] = _get_json) -> list[dict[str, Any]]:
    """Chainlink-sourced OHLCV for an oracle market — the SAME data stream the
    market resolves on. This is the settle reference our Binance-spot labels
    keep disagreeing with; grading a round off these candles removes the
    'reference error' class entirely."""
    params: dict[str, Any] = {"interval": interval}
    if frm is not None:
        params["from"] = int(frm)
    if to is not None:
        params["to"] = int(to)
    try:
        raw = getter(f"{REST}/markets/{slug}/oracle-candles", params)
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("candles") or raw.get("data") or []
    return raw if isinstance(raw, list) else []


def _candle_ohlc(c: Any) -> Optional[tuple[int, float, float]]:
    """(timestamp, open, close) from a candle in the common shapes:
    {"t"/"time"/"timestamp", "o"/"open", "c"/"close"} or [t,o,h,l,c]."""
    if isinstance(c, dict):
        t = c.get("t") or c.get("time") or c.get("timestamp")
        o = c.get("o") if "o" in c else c.get("open")
        cl = c.get("c") if "c" in c else c.get("close")
    elif isinstance(c, (list, tuple)) and len(c) >= 5:
        t, o, cl = c[0], c[1], c[4]
    else:
        return None
    t, o, cl = _to_float(t), _to_float(o), _to_float(cl)
    if t is None or o is None or cl is None:
        return None
    return int(t), o, cl


def settle_from_candles(candles: list[dict[str, Any]], round_start: int,
                        window: str = "15m") -> Optional[str]:
    """OFFICIAL-style up/down label for a round, computed from the oracle
    candles: open of the round's first candle vs close of its last. Returns
    'UP'/'DOWN'/'FLAT', or None if the round isn't covered by the candles.

    Timestamps may be seconds or milliseconds; normalized before comparison."""
    slot = WINDOW_SLOT[window]
    parsed = [p for p in (_candle_ohlc(c) for c in candles) if p is not None]
    if not parsed:
        return None
    # Normalize ms -> s if the stamps look like milliseconds.
    if parsed and parsed[0][0] > 10_000_000_000:
        parsed = [(t // 1000, o, cl) for (t, o, cl) in parsed]
    in_round = [(t, o, cl) for (t, o, cl) in parsed if round_start <= t < round_start + slot]
    if not in_round:
        return None
    in_round.sort(key=lambda x: x[0])
    o = in_round[0][1]
    cl = in_round[-1][2]
    return "UP" if cl > o else "DOWN" if cl < o else "FLAT"


def resolved_outcome(slug: str, getter: Callable[..., Any] = _get_json,
                     window: str = "15m") -> Optional[str]:
    """OFFICIAL Limitless resolution for a (closed) up-down market: 'UP'/'DOWN',
    or None if it hasn't decisively resolved. Prefers the market's own resolved
    outcome field; falls back to grading off the oracle candles."""
    try:
        m = getter(f"{REST}/markets/{slug}")
    except Exception:
        m = None
    if isinstance(m, dict):
        # A resolved binary market reports its winning side one of several ways.
        for key in ("winningOutcome", "resolvedOutcome", "outcome", "result"):
            v = m.get(key)
            if isinstance(v, str) and v.strip().lower() in ("up", "yes"):
                return "UP"
            if isinstance(v, str) and v.strip().lower() in ("down", "no"):
                return "DOWN"
        prices = m.get("outcomePrices") or m.get("prices")
        if isinstance(prices, (list, tuple)) and len(prices) >= 2:
            p0, p1 = _to_float(prices[0]), _to_float(prices[1])
            if p0 is not None and p1 is not None:
                if p0 > 0.99 and p1 < 0.01:
                    return "UP"
                if p1 > 0.99 and p0 < 0.01:
                    return "DOWN"
    # Fall back to the oracle candles for the round encoded in the slug.
    m2 = re.search(r"-(\d+)$", slug)
    if m2:
        rs = int(m2.group(1))
        return settle_from_candles(oracle_candles(slug, getter=getter), rs, window)
    return None


def _discover(asset: str, window: str) -> int:
    """Live reconnaissance: dump what the API ACTUALLY returns so the slug
    convention and JSON shapes can be learned from one run on a normal network.
    (First probe showed Limitless does NOT use the Polymarket-style
    `btc-updown-15m-<ts>` slugs — this prints the real naming.)"""
    a = asset.lower()

    # 1) The active-slugs list: shape + everything that looks like our asset.
    try:
        slugs = _get_json(f"{REST}/markets/active/slugs")
        print(f"# /markets/active/slugs -> type={type(slugs).__name__}", end="")
        if isinstance(slugs, dict):
            print(f" keys={list(slugs.keys())[:8]}")
            for k in ("slugs", "data", "markets", "items"):
                if isinstance(slugs.get(k), list):
                    slugs = slugs[k]
                    break
        else:
            print()
        if isinstance(slugs, list):
            print(f"#   {len(slugs)} entries; first 10:")
            for s in slugs[:10]:
                print(f"#     {json.dumps(s) if not isinstance(s, str) else s}")
            strs = [str(s) for s in slugs]
            hits = [s for s in strs if a in s.lower()][:20]
            print(f"#   entries containing '{a}' ({len(hits)} shown):")
            for s in hits:
                print(f"#     {s}")
            w15 = [s for s in strs if "15" in s][:10]
            print(f"#   entries containing '15' (first 10):")
            for s in w15:
                print(f"#     {s}")
    except Exception as e:
        print(f"# /markets/active/slugs failed: {e}")

    # 2) A metadata page: real market objects with their field names.
    try:
        page = _get_json(f"{REST}/markets/active", {"page": 1, "limit": 25})
        items = page
        if isinstance(page, dict):
            print(f"# /markets/active keys: {list(page.keys())[:10]}")
            for k in ("markets", "data", "items", "results"):
                if isinstance(page.get(k), list):
                    items = page[k]
                    break
        if isinstance(items, list) and items:
            m0 = items[0]
            if isinstance(m0, dict):
                print(f"# market object keys: {list(m0.keys())}")
            crypto = [m for m in items if isinstance(m, dict)
                      and a in json.dumps(m).lower()][:5]
            print(f"#   {a}-related markets on page 1 ({len(crypto)}):")
            for m in crypto:
                brief = {k: m.get(k) for k in ("slug", "title", "name", "ticker",
                                               "deadline", "expirationDate", "endDate",
                                               "duration", "marketType", "category")
                         if k in m}
                print(f"#     {json.dumps(brief)[:300]}")
        else:
            print(f"# /markets/active page unparsed: {json.dumps(page)[:400]}")
    except Exception as e:
        print(f"# /markets/active failed: {e}")

    # 3) If anything matched, probe the first asset-hit's orderbook + candles.
    probe = None
    try:
        if isinstance(slugs, list):
            for s in slugs:
                if a in str(s).lower():
                    probe = str(s)
                    break
    except Exception:
        pass
    if probe:
        print(f"# probing orderbook of first {a} hit: {probe}")
        try:
            raw = _get_json(f"{REST}/markets/{probe}/orderbook")
            print(f"#   orderbook keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")
            print(json.dumps(raw, indent=2)[:1200])
            print("#   parsed:", json.dumps(prices_from_orderbook(raw) if isinstance(raw, dict) else None))
        except Exception as e:
            print(f"#   orderbook failed: {e}")
        try:
            cs = oracle_candles(probe, interval="5m")
            print(f"#   oracle candles: {len(cs)}; first: {json.dumps(cs[0])[:300] if cs else '-'}")
        except Exception as e:
            print(f"#   oracle-candles failed: {e}")
    else:
        print(f"# no '{a}' slug found in the active list — see dumps above for the real naming")
    return 0


def main() -> int:
    import sys
    args = sys.argv[1:]
    if args and args[0] == "--discover":
        asset = args[1] if len(args) > 1 else "btc"
        window = args[2] if len(args) > 2 else "15m"
        return _discover(asset, window)
    now = time.time()
    p = current_prices(now, asset="btc", window="15m")
    if not p:
        print("no current Limitless 15m market reachable (egress blocked? run on a "
              "normal network, or `--discover btc 15m` to inspect the live API)",
              file=sys.stderr)
        return 2
    print(f"slug {p['slug']}  (venue={p['venue']})")
    print(f"  UP   ask: {p['UP']}   bid: {p['UP_bid']}")
    print(f"  DOWN ask: {p['DOWN']}   bid: {p['DOWN_bid']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
