#!/usr/bin/env python3
"""
Binance connector: historical 1m klines -> CSV, and a live price feed.

Runs anywhere with outbound HTTPS to Binance. (In a restricted sandbox whose
egress policy blocks api.binance.com it will fail fast with a clear message —
run it on your own machine, or widen the environment's network policy.)

No API key required: klines and ticker are public market-data endpoints.
Kept to the stdlib + `requests` (already a repo dependency).

Endpoints and a regional fallback are tried in order, so it works from most
locations without configuration:
    1. https://data-api.binance.vision   (public market-data mirror, no geo-block)
    2. https://api.binance.com            (main)
    3. https://api.binance.us             (US)
Override with --base or env BINANCE_BASE.

CLI
---
    # Download 30 days of 1m OHLCV to a backtest-ready CSV:
    python3 scripts/btc_binance.py history --days 30 --out data/btc_1m.csv

    # Live price, polling every 2s (Ctrl-C to stop):
    python3 scripts/btc_binance.py live --poll 2

    # Just check connectivity / which base works from here:
    python3 scripts/btc_binance.py check
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from typing import Any, Callable, Optional

import requests

DEFAULT_BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api.binance.us",
]

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def bases(base: Optional[str]) -> list[str]:
    if base:
        return [base.rstrip("/")]
    env = os.environ.get("BINANCE_BASE")
    if env:
        return [env.rstrip("/")]
    return list(DEFAULT_BASES)


def _get(path: str, params: dict[str, Any], base_list: list[str], timeout: float = 15.0) -> Any:
    """GET `path` against the first base that answers; raises the last error."""
    last_err: Optional[Exception] = None
    for base in base_list:
        try:
            r = requests.get(base + path, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # network, HTTP, or JSON error -> try next base
            last_err = e
    raise RuntimeError(f"all Binance bases failed for {path}: {last_err}")


def working_base(base: Optional[str] = None) -> Optional[str]:
    """Return the first reachable base URL, or None if none respond."""
    for b in bases(base):
        try:
            requests.get(b + "/api/v3/time", timeout=8).raise_for_status()
            return b
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# Historical klines
# --------------------------------------------------------------------------
def parse_klines(raw: list[list[Any]]) -> list[dict[str, Any]]:
    """Convert raw Binance kline rows into OHLCV dicts (time in unix seconds)."""
    out = []
    for k in raw:
        out.append({
            "time": int(k[0]) // 1000,  # openTime ms -> s
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return out


def fetch_klines_page(
    symbol: str, interval: str, start_ms: int, base_list: list[str], limit: int = 1000
) -> list[list[Any]]:
    return _get("/api/v3/klines", {
        "symbol": symbol, "interval": interval, "startTime": start_ms, "limit": limit,
    }, base_list)


def download_history(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    days: float = 30.0,
    base: Optional[str] = None,
    now_ms: Optional[int] = None,
    page_fetcher: Optional[Callable[[str, str, int, list[str], int], list[list[Any]]]] = None,
    sleep_sec: float = 0.15,
    progress: bool = False,
) -> list[dict[str, Any]]:
    """Fetch `days` of history by paginating klines forward.

    `page_fetcher` is injectable for testing (defaults to the live HTTP call);
    `now_ms` pins the end time so runs are deterministic under test.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval!r}")
    step = INTERVAL_MS[interval]
    end = int(now_ms if now_ms is not None else time.time() * 1000)
    cursor = end - int(days * 86_400_000)
    base_list = bases(base)
    fetch = page_fetcher or fetch_klines_page

    rows: list[dict[str, Any]] = []
    seen_last = None
    while cursor < end:
        page = fetch(symbol, interval, cursor, base_list, 1000)
        if not page:
            break
        rows.extend(parse_klines(page))
        last_open = int(page[-1][0])
        if last_open == seen_last:  # no forward progress -> stop
            break
        seen_last = last_open
        cursor = last_open + step
        if progress:
            done = min(1.0, (cursor - (end - int(days * 86_400_000))) / max(1, days * 86_400_000))
            print(f"\r  fetched {len(rows)} bars ({done:.0%})", end="", file=sys.stderr, flush=True)
        if len(page) < 1000:
            break
        if sleep_sec:
            time.sleep(sleep_sec)
    if progress:
        print("", file=sys.stderr)

    # De-dup and sort by time (pages can overlap on boundaries).
    dedup: dict[int, dict[str, Any]] = {r["time"]: r for r in rows}
    return [dedup[t] for t in sorted(dedup)]


def write_csv(rows: list[dict[str, Any]], path: str) -> int:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow([r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]])
    return len(rows)


# --------------------------------------------------------------------------
# Live feed
# --------------------------------------------------------------------------
def live_spot(symbol: str = "BTCUSDT", base: Optional[str] = None) -> float:
    j = _get("/api/v3/ticker/price", {"symbol": symbol}, bases(base))
    return float(j["price"])


def stream(
    symbol: str = "BTCUSDT",
    poll_sec: float = 2.0,
    base: Optional[str] = None,
    on_tick: Optional[Callable[[float, float], None]] = None,
    max_ticks: Optional[int] = None,
) -> None:
    """REST-poll live price loop. `on_tick(price, unix_ts)` is called per tick;
    the default prints. This mirrors how the trading loop consumes prices — a
    steady poll, no persistent socket. (For a push stream, Binance also offers
    wss://stream.binance.com:9443/ws/<sym>@kline_1m, but that needs a WebSocket
    client; REST polling keeps this dependency-free and is plenty for 5m rounds.)
    """
    base_list = bases(base)
    n = 0
    while max_ticks is None or n < max_ticks:
        try:
            j = _get("/api/v3/ticker/price", {"symbol": symbol}, base_list)
            px = float(j["price"])
            ts = time.time()
            if on_tick:
                on_tick(px, ts)
            else:
                print(f"{time.strftime('%H:%M:%S', time.gmtime(ts))}Z  {symbol}  ${px:,.2f}")
        except Exception as e:
            print(f"tick error: {e}", file=sys.stderr)
        n += 1
        if max_ticks is not None and n >= max_ticks:
            break
        time.sleep(poll_sec)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Binance historical + live BTC connector")
    ap.add_argument("--base", help="Override base URL (else tries the fallback list)")
    ap.add_argument("--symbol", default="BTCUSDT")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ph = sub.add_parser("history", help="Download klines to CSV")
    ph.add_argument("--interval", default="1m", choices=sorted(INTERVAL_MS))
    ph.add_argument("--days", type=float, default=30.0)
    ph.add_argument("--out", default="data/btc_1m.csv")

    pl = sub.add_parser("live", help="Poll live price")
    pl.add_argument("--poll", type=float, default=2.0)
    pl.add_argument("--max-ticks", type=int, default=None)

    sub.add_parser("check", help="Report which base URL is reachable")

    args = ap.parse_args(argv)

    if args.cmd == "check":
        b = working_base(args.base)
        if b:
            print(f"reachable: {b}")
            print(f"spot {args.symbol}: ${live_spot(args.symbol, b):,.2f}")
            return 0
        print("no Binance base reachable from here (egress blocked?). "
              "Run on a machine with open outbound HTTPS, or widen the network policy.",
              file=sys.stderr)
        return 2

    if args.cmd == "history":
        if not working_base(args.base):
            print("Binance is not reachable from this environment (egress policy). "
                  "Run this on your PC; the CSV format matches btc_backtest.py --csv.",
                  file=sys.stderr)
            return 2
        rows = download_history(args.symbol, args.interval, args.days, args.base, progress=True)
        n = write_csv(rows, args.out)
        span = ""
        if rows:
            span = f"  [{time.strftime('%Y-%m-%d', time.gmtime(rows[0]['time']))} .. " \
                   f"{time.strftime('%Y-%m-%d', time.gmtime(rows[-1]['time']))}]"
        print(f"wrote {n} {args.interval} bars -> {args.out}{span}")
        return 0

    if args.cmd == "live":
        if not working_base(args.base):
            print("Binance is not reachable from this environment (egress policy).", file=sys.stderr)
            return 2
        stream(args.symbol, args.poll, args.base, max_ticks=args.max_ticks)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
