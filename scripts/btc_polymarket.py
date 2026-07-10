#!/usr/bin/env python3
"""
Fetch the REAL Polymarket 5-minute BTC Up/Down contract price.

The paper trader's default $0.85 entry price is only a placeholder. This module
pulls the actual cost to enter — the CLOB best ask of a side — so paper P&L uses
the price you would truly pay, and the breakeven line reflects reality.

Pipeline (matches the live runner):
  1. slug   = btc-updown-5m-<5m-bucket>
  2. gamma  = https://gamma-api.polymarket.com/events?slug=<slug>  -> token ids
  3. book   = https://clob.polymarket.com/book?token_id=<id>       -> best ask

Stdlib + requests only (no py_clob_client). Every network call goes through an
injectable getter so the parsing is unit-tested offline. Reachable on a normal
network; blocked by restricted egress policies (that's fine — paper trades then
fall back to the fixed --entry-price).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import requests

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"
SLUG_PREFIX = "btc-updown-5m-"


def _get_json(url: str, params: Optional[dict[str, Any]] = None, timeout: float = 8.0) -> Any:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def current_slug(now_ts: float) -> str:
    return f"{SLUG_PREFIX}{bucket_5m(int(now_ts))}"


def parse_json_field(v: Any) -> Any:
    """Gamma returns outcomes / outcomePrices / clobTokenIds as JSON-encoded
    strings; decode them, but pass through values that are already lists."""
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def fetch_event(slug: str, getter: Callable[..., Any] = _get_json) -> Optional[dict[str, Any]]:
    arr = getter(GAMMA, {"slug": slug})
    if isinstance(arr, list) and arr:
        return arr[0]
    return None


def clob_best_ask_level(token_id: str, getter: Callable[..., Any] = _get_json) -> tuple[Optional[float], Optional[float]]:
    """(price, size) of the lowest ask from the CLOB order book. Size matters:
    a cheap best ask for dust shares looks like edge in analysis but cannot be
    bought in practice — record it so the analyzers can filter phantom quotes."""
    try:
        book = getter(CLOB_BOOK, {"token_id": str(token_id)})
    except Exception:
        return None, None
    asks = (book or {}).get("asks") or []
    best: Optional[float] = None
    best_sz: Optional[float] = None
    for a in asks:
        try:
            p = float(a["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if best is None or p < best:
            best = p
            try:
                best_sz = float(a.get("size"))
            except (TypeError, ValueError):
                best_sz = None
    return best, best_sz


def clob_best_ask(token_id: str, getter: Callable[..., Any] = _get_json) -> Optional[float]:
    """Lowest ask (the price you pay to BUY this token) from the CLOB order book."""
    return clob_best_ask_level(token_id, getter)[0]


def _side_tokens(market: dict[str, Any]) -> Optional[tuple[str, str, str]]:
    outcomes = parse_json_field(market.get("outcomes")) or []
    token_ids = parse_json_field(market.get("clobTokenIds")) or []
    if len(token_ids) < 2:
        return None
    up_i, down_i = 0, 1
    labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
    if len(labs) >= 2 and ("up" in labs[1] or "yes" in labs[1]):
        up_i, down_i = 1, 0
    end_iso = str(market.get("endDate") or market.get("endDateIso") or "")
    return str(token_ids[up_i]), str(token_ids[down_i]), end_iso


def current_prices(now_ts: float, getter: Callable[..., Any] = _get_json) -> Optional[dict[str, Any]]:
    """Return the live UP/DOWN best-ask prices for the current 5m market, or None
    if the market can't be resolved. Prices are floats in (0,1) or None per side."""
    slug = current_slug(now_ts)
    ev = fetch_event(slug, getter)
    if not ev:
        return None
    markets = ev.get("markets") or []
    if not markets:
        return None
    tokens = _side_tokens(markets[0])
    if tokens is None:
        return None
    up_t, dn_t, end_iso = tokens
    up_p, up_sz = clob_best_ask_level(up_t, getter)
    dn_p, dn_sz = clob_best_ask_level(dn_t, getter)
    return {
        "slug": slug,
        "up_token": up_t,
        "down_token": dn_t,
        "end_iso": end_iso,
        "UP": up_p,
        "DOWN": dn_p,
        "UP_size": up_sz,
        "DOWN_size": dn_sz,
    }


def main() -> int:
    import sys
    import time

    now = time.time()
    p = current_prices(now)
    if not p:
        print("no current 5m market reachable (egress blocked? run on a normal network)",
              file=sys.stderr)
        return 2
    print(f"slug {p['slug']}")
    print(f"  UP  ask: {p['UP']}")
    print(f"  DOWN ask: {p['DOWN']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
