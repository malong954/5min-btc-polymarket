#!/usr/bin/env python3
"""Minimal Limitless.exchange public-API client for the arb venue test.

The exact API shapes are probed at runtime (limitless_probe.py prints what
the live host actually serves; this module tries the same candidate variants
and remembers the first one that works). Everything is defensive: unknown
shapes degrade to "no data" rather than crashes.

Env:
  LIMITLESS_BASE   override the API base (default https://api.limitless.exchange)
"""
from __future__ import annotations

import datetime as dt
import os
import time
from typing import Any, Optional

import requests

BASE = os.getenv("LIMITLESS_BASE", "https://api.limitless.exchange")
UA = {"User-Agent": "btc-multi-arb-monitor/0.1 (research; contact via github)"}

# every network attempt gets recorded here so the probe can print the trail
ATTEMPTS: list[dict] = []

LIST_VARIANTS: list[tuple[str, Optional[dict]]] = [
    ("/markets/active", {"page": 1, "limit": 200}),
    ("/markets/active", None),
    ("/markets", {"page": 1, "limit": 200}),
    ("/markets", {"limit": 200}),
    ("/markets", None),
]
_working_list_variant: Optional[tuple[str, Optional[dict]]] = None

OB_VARIANTS = [
    "/markets/{slug}/orderbook",
    "/markets/{address}/orderbook",
    "/orderbook/{slug}",
    "/markets/{slug}/order-book",
]
_working_ob_variant: Optional[str] = None


def _get(path: str, params: Optional[dict] = None, timeout: float = 10.0):
    r = requests.get(BASE + path, params=params, headers=UA, timeout=timeout)
    ATTEMPTS.append({"path": path, "params": params, "status": r.status_code})
    return r


def _rows(j: Any) -> Optional[list]:
    """Normalize a list response: bare list, or {data|markets|items|...: [...]}."""
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in ("data", "markets", "items", "results", "rows"):
            v = j.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("markets"), list):
                return v["markets"]
    return None


def list_markets(max_pages: int = 5) -> list[dict]:
    """Fetch active markets, walking pagination when the variant supports it."""
    global _working_list_variant
    variants = ([_working_list_variant] if _working_list_variant else []) + \
        [v for v in LIST_VARIANTS if v != _working_list_variant]
    for path, params in variants:
        try:
            r = _get(path, params)
            if r.status_code != 200:
                continue
            rows = _rows(r.json())
            if rows is None:
                continue
            _working_list_variant = (path, params)
            out = list(rows)
            if params and "page" in params:
                for page in range(2, max_pages + 1):
                    p2 = dict(params, page=page)
                    try:
                        r2 = _get(path, p2)
                        more = _rows(r2.json()) if r2.status_code == 200 else None
                    except Exception:
                        break
                    if not more:
                        break
                    out.extend(more)
                    if len(more) < int(params.get("limit") or 0):
                        break
            return out
        except Exception as e:
            ATTEMPTS.append({"path": path, "params": params, "error": str(e)[:120]})
            continue
    return []


def title_of(m: dict) -> str:
    return str(m.get("title") or m.get("question") or m.get("name") or "")


def slug_of(m: dict) -> Optional[str]:
    v = m.get("slug") or m.get("address") or m.get("id")
    return str(v) if v is not None else None


def address_of(m: dict) -> Optional[str]:
    v = m.get("address") or m.get("marketAddress") or m.get("id")
    return str(v) if v is not None else None


def deadline_ts(m: dict) -> Optional[float]:
    """Market end time as unix seconds, from whichever field exists."""
    for k in ("deadline", "expirationDate", "expiration", "expiresAt",
              "closeDate", "endDate", "expirationTimestamp", "deadlineTimestamp"):
        v = m.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            f = float(v)
            return f / 1000.0 if f > 1e12 else f
        if isinstance(v, str):
            s = v.strip()
            try:
                return float(s) / 1000.0 if float(s) > 1e12 else float(s)
            except ValueError:
                pass
            try:
                return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def is_btc(m: dict) -> bool:
    t = (title_of(m) + " " + str(m.get("slug") or "")).lower()
    return "btc" in t or "bitcoin" in t


def is_openish(m: dict) -> bool:
    for k in ("expired", "closed", "resolved", "isResolved"):
        if m.get(k) is True:
            return False
    st = str(m.get("status") or "").upper()
    if st in ("RESOLVED", "CLOSED", "EXPIRED", "LOCKED"):
        return False
    return True


def _price_norm(v: Any) -> Optional[float]:
    """Map a raw price to (0, 1]: handles 0-1 floats, percents, 1e6/1e18 fixed."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    for scale in (1.0, 100.0, 1e6, 1e18):
        x = f / scale
        if 0.0 < x <= 1.0:
            return x
    return None


def _size_norm(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f > 1e12:
        return f / 1e18
    if f > 1e7:
        return f / 1e6
    return f


def _levels(side: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if not isinstance(side, list):
        return out
    for r in side:
        if isinstance(r, dict):
            p = _price_norm(r.get("price") if r.get("price") is not None else r.get("p"))
            s = r.get("size")
            if s is None:
                s = r.get("amount")
            if s is None:
                s = r.get("quantity")
            if s is None:
                s = r.get("s")
        elif isinstance(r, (list, tuple)) and len(r) >= 1:
            p = _price_norm(r[0])
            s = r[1] if len(r) > 1 else 0
        else:
            continue
        if p is not None:
            out.append((p, _size_norm(s)))
    return out


def _book_side(j: Any) -> Optional[dict]:
    """Parse one {bids, asks} book into best levels."""
    if not isinstance(j, dict):
        return None
    bids = _levels(j.get("bids"))
    asks = _levels(j.get("asks"))
    if not bids and not asks:
        return None
    out: dict = {"bid": None, "ask": None, "bid_size": 0.0, "ask_size": 0.0}
    if bids:
        out["bid"], out["bid_size"] = max(bids, key=lambda x: x[0])
    if asks:
        out["ask"], out["ask_size"] = min(asks, key=lambda x: x[0])
    return out


def normalize_book(j: Any) -> Optional[dict]:
    """Normalize any observed orderbook shape.

    Returns {two_sided, yes_bid, yes_ask, yes_ask_size, no_bid, no_ask,
    no_ask_size, raw_keys}. two_sided=True means the venue serves SEPARATE
    YES and NO books — the precondition for complete-set (crossed-book) arb.
    A single shared book can never sum below $1 (that's just the spread).
    """
    if not isinstance(j, dict):
        return None
    for wrap in ("orderbook", "data", "book", "result"):
        if isinstance(j.get(wrap), (dict, list)):
            j = j[wrap] if isinstance(j[wrap], dict) else {"tokens": j[wrap]}
            break
    raw_keys = sorted(j.keys()) if isinstance(j, dict) else []

    # shape 1: explicit yes/no sub-books
    for yk, nk in (("yes", "no"), ("YES", "NO"), ("up", "down")):
        y, n = _book_side(j.get(yk)), _book_side(j.get(nk))
        if y or n:
            return {"two_sided": bool(y and n), "raw_keys": raw_keys,
                    "yes_bid": (y or {}).get("bid"), "yes_ask": (y or {}).get("ask"),
                    "yes_ask_size": (y or {}).get("ask_size", 0.0),
                    "no_bid": (n or {}).get("bid"), "no_ask": (n or {}).get("ask"),
                    "no_ask_size": (n or {}).get("ask_size", 0.0)}

    # shape 2: token list with outcome labels
    toks = j.get("tokens")
    if isinstance(toks, list) and toks and isinstance(toks[0], dict):
        y = n = None
        for t in toks:
            label = str(t.get("outcome") or t.get("side") or t.get("name") or "").lower()
            side = _book_side(t)
            if side is None:
                continue
            if label.startswith(("yes", "up")):
                y = side
            elif label.startswith(("no", "down")):
                n = side
        if y or n:
            return {"two_sided": bool(y and n), "raw_keys": raw_keys,
                    "yes_bid": (y or {}).get("bid"), "yes_ask": (y or {}).get("ask"),
                    "yes_ask_size": (y or {}).get("ask_size", 0.0),
                    "no_bid": (n or {}).get("bid"), "no_ask": (n or {}).get("ask"),
                    "no_ask_size": (n or {}).get("ask_size", 0.0)}

    # shape 3: one shared {bids, asks} book (YES-denominated)
    one = _book_side(j)
    if one:
        return {"two_sided": False, "raw_keys": raw_keys,
                "yes_bid": one["bid"], "yes_ask": one["ask"],
                "yes_ask_size": one["ask_size"],
                "no_bid": None, "no_ask": None, "no_ask_size": 0.0}
    return None


def fetch_book(m: dict) -> Optional[dict]:
    """Try orderbook endpoint variants for a market; normalize the winner."""
    global _working_ob_variant
    slug, addr = slug_of(m), address_of(m)
    variants = ([_working_ob_variant] if _working_ob_variant else []) + \
        [v for v in OB_VARIANTS if v != _working_ob_variant]
    for v in variants:
        path = v.replace("{slug}", slug or "").replace("{address}", addr or "")
        if "//" in path.replace("://", ""):
            continue
        try:
            r = _get(path)
            if r.status_code != 200:
                continue
            book = normalize_book(r.json())
            if book:
                _working_ob_variant = v
                return book
        except Exception as e:
            ATTEMPTS.append({"path": path, "error": str(e)[:120]})
            continue
    return None


def amm_prices(m: dict) -> Optional[tuple[float, float]]:
    """AMM markets carry mid prices in the market object (usually percents)."""
    pr = m.get("prices")
    if isinstance(pr, list) and len(pr) >= 2:
        a, b = _price_norm(pr[0]), _price_norm(pr[1])
        if a is not None and b is not None:
            return (a, b)
    return None


def short_dated_btc(markets: list[dict], horizon_sec: float = 4 * 3600) -> list[dict]:
    """Open BTC markets ending within the horizon, soonest first."""
    now = time.time()
    out = []
    for m in markets:
        if not (is_btc(m) and is_openish(m)):
            continue
        d = deadline_ts(m)
        if d is None or d <= now or d > now + horizon_sec:
            continue
        out.append((d, m))
    out.sort(key=lambda x: x[0])
    return [m for _, m in out]
