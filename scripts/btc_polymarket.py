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


def clob_book_top(token_id: str, getter: Callable[..., Any] = _get_json) -> dict[str, Optional[float]]:
    """Top of book for a token: best ask (price you pay to BUY), best bid (price
    you receive to SELL), and the size at each. Sizes matter: dust quotes look
    like edge in analysis but cannot be traded. Bids enable exit/maker studies."""
    out: dict[str, Optional[float]] = {"ask": None, "ask_sz": None, "bid": None, "bid_sz": None}
    try:
        book = getter(CLOB_BOOK, {"token_id": str(token_id)})
    except Exception:
        return out
    for side, key, better in (("asks", "ask", min), ("bids", "bid", max)):
        best: Optional[float] = None
        best_sz: Optional[float] = None
        for lvl in (book or {}).get(side) or []:
            try:
                p = float(lvl["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if best is None or better(p, best) == p:
                best = p
                try:
                    best_sz = float(lvl.get("size"))
                except (TypeError, ValueError):
                    best_sz = None
        out[key] = best
        out[key + "_sz"] = best_sz
    return out


def clob_best_ask_level(token_id: str, getter: Callable[..., Any] = _get_json) -> tuple[Optional[float], Optional[float]]:
    top = clob_book_top(token_id, getter)
    return top["ask"], top["ask_sz"]


def clob_best_ask(token_id: str, getter: Callable[..., Any] = _get_json) -> Optional[float]:
    """Lowest ask (the price you pay to BUY this token) from the CLOB order book."""
    return clob_book_top(token_id, getter)["ask"]


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
    up_top = clob_book_top(up_t, getter)
    dn_top = clob_book_top(dn_t, getter)
    return {
        "slug": slug,
        "up_token": up_t,
        "down_token": dn_t,
        "end_iso": end_iso,
        "UP": up_top["ask"],
        "DOWN": dn_top["ask"],
        "UP_size": up_top["ask_sz"],
        "DOWN_size": dn_top["ask_sz"],
        "UP_bid": up_top["bid"],
        "DOWN_bid": dn_top["bid"],
        "UP_bid_size": up_top["bid_sz"],
        "DOWN_bid_size": dn_top["bid_sz"],
    }


def resolved_outcome(slug: str, getter: Callable[..., Any] = _get_json) -> Optional[str]:
    """OFFICIAL Polymarket resolution for a (closed) 5m market: 'UP' / 'DOWN',
    or None if it hasn't decisively resolved yet. This is the ground-truth
    label — our spot-feed settle can disagree with the Chainlink resolution on
    near-flat rounds, silently grading our own homework."""
    try:
        ev = fetch_event(slug, getter)
    except Exception:
        return None
    if not ev:
        return None
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    outcomes = parse_json_field(m.get("outcomes")) or []
    prices = parse_json_field(m.get("outcomePrices")) or []
    if not isinstance(prices, list) or len(prices) < 2:
        return None
    try:
        p0, p1 = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    # Only trust a decisive settlement (1/0), not live mid-round prices.
    if not ((p0 > 0.99 and p1 < 0.01) or (p1 > 0.99 and p0 < 0.01)):
        return None
    win_i = 0 if p0 > p1 else 1
    labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
    up_i = 0
    if len(labs) >= 2 and ("up" in labs[1] or "yes" in labs[1]):
        up_i = 1
    return "UP" if win_i == up_i else "DOWN"


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
