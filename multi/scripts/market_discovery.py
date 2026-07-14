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
import json
import time
from typing import Any, Optional

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
        "{asset_name}-up-or-down-{month}-{day}-{hour12}{ampm}-et",
        "{asset_name}-up-or-down-{month}-{day}-{year}-{hour12}{ampm}-et",
    ],
    "4h": ["{asset}-updown-4h-{bucket}"],
    "1d": [
        "{asset}-updown-1d-{bucket}",
        "{asset_name}-up-or-down-on-{month}-{day}",
        "{asset_name}-up-or-down-{month}-{day}",
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
        # Daily markets run midnight-to-midnight Eastern Time (DST-aware).
        et_now = dt.datetime.fromtimestamp(t, ET)
        start_et = et_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_et = start_et + dt.timedelta(days=1)
        return int(start_et.timestamp()), int(end_et.timestamp())
    start = t - t % dur
    return start, start + dur


def next_slot_bounds(tf: str, ts: Optional[float] = None) -> tuple[int, int]:
    _, end = slot_bounds(tf, ts)
    return slot_bounds(tf, end + 1)


def _fmt_map(asset: str, tf: str, slot_start: int) -> dict[str, Any]:
    et_start = dt.datetime.fromtimestamp(slot_start, ET)
    return {
        "asset": asset.lower(),
        "asset_name": ASSET_NAMES.get(asset.lower(), asset.lower()),
        "tf": tf,
        "bucket": slot_start,
        "month": et_start.strftime("%B").lower(),
        "day": et_start.day,
        "year": et_start.year,
        "hour12": int(et_start.strftime("%I")),
        "ampm": et_start.strftime("%p").lower(),
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
