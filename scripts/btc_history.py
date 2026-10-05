#!/usr/bin/env python3
"""
Multi-provider historical 1-minute OHLCV for the backtester.

Why only these two providers for *history*:
    The backtest needs 1-minute OHLCV (ideally with volume). Among the common
    free APIs, only a few actually serve keyless minute bars:
      - Binance        : full OHLCV, keyless, best coverage (scripts/btc_binance.py)
      - CryptoCompare  : full OHLCV incl. volume, keyless (rate-limited)
    CoinGecko's free history is too coarse (>=30-min candles), and CoinMarketCap
    / LiveCoinWatch gate minute history behind an API key — so they're good for
    *live spot* (see btc_price_feeds.py) but not for a 1-minute backtest.

All providers return the same row shape as scripts/btc_binance.py, so the output
drops straight into btc_backtest.py (--csv or --fetch).

    row = {"time": unix_seconds, "open","high","low","close","volume": float}
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import requests


def _get_json(url: str, params: Optional[dict[str, Any]] = None, timeout: float = 15.0) -> Any:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# CryptoCompare: keyless minute OHLCV, paginated backward via toTs.
# --------------------------------------------------------------------------
def history_cryptocompare(
    days: float = 7.0,
    now_ts: Optional[int] = None,
    getter: Callable[..., Any] = _get_json,
    sleep_sec: float = 0.2,
) -> list[dict[str, Any]]:
    end = int(now_ts if now_ts is not None else time.time())
    start = end - int(days * 86_400)
    to_ts = end
    by_time: dict[int, dict[str, Any]] = {}
    guard = 0
    while to_ts > start and guard < 5000:
        guard += 1
        j = getter(
            "https://min-api.cryptocompare.com/data/v2/histominute",
            {"fsym": "BTC", "tsym": "USD", "limit": 2000, "toTs": to_ts},
        )
        rows = ((j or {}).get("Data") or {}).get("Data") or []
        if not rows:
            break
        for r in rows:
            t = int(r["time"])
            # volumeto is quote-volume (USD); use it as the volume proxy.
            by_time[t] = {
                "time": t,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volumeto") or r.get("volumefrom") or 0.0),
            }
        earliest = int(rows[0]["time"])
        if earliest >= to_ts:  # no backward progress
            break
        to_ts = earliest - 60
        if sleep_sec:
            time.sleep(sleep_sec)
    return [by_time[t] for t in sorted(by_time) if t >= start]


# --------------------------------------------------------------------------
# Binance: delegate to the dedicated connector.
# --------------------------------------------------------------------------
def history_binance(days: float = 7.0, symbol: str = "BTCUSDT") -> list[dict[str, Any]]:
    from btc_binance import download_history

    return download_history(symbol=symbol, interval="1m", days=days)


PROVIDERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "binance": history_binance,
    "cryptocompare": history_cryptocompare,
}


def fetch_history(provider: str, days: float = 7.0, **kw: Any) -> list[dict[str, Any]]:
    p = provider.strip().lower()
    fn = PROVIDERS.get(p)
    if fn is None:
        raise ValueError(f"unknown history provider {provider!r}; choose from {sorted(PROVIDERS)}")
    return fn(days=days, **kw)
