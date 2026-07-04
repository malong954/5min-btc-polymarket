#!/usr/bin/env python3
"""
Real-time BTC price feeds + intra-round impulse gate.

Why two feeds:
    Single exchanges lag, freeze, or print bad ticks. By reading two
    independent spot sources and requiring them to agree, we (a) confirm the
    move is real (not one exchange glitching) and (b) detect when one source is
    lagging behind the other, which is exactly the edge case that burns a
    momentum-into-close trade.

This module measures the *actual* BTC move inside the current 5-minute round
(current spot minus the price at the start of the round), which the Polymarket
contract price only proxies. It implements the "$70-$100 move" impulse filter
that the strategy documents but the runner never enforced.

No API keys required. Default feeds: Binance + Coinbase. A Kraken adapter is
included so the feed set is configurable if a host is blocked on your network.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

# Kept in sync with bucket_5m() in the runner: rounds align to 5-minute UTC
# boundaries (…:00, :05, :10), which is also how these exchanges bucket their
# 5-minute candles, so the candle openTime == the round start.
SLOT_SECONDS = 300


def bucket_5m(ts: int) -> int:
    return ts - (ts % SLOT_SECONDS)


def _get_json(url: str, params: Optional[dict[str, Any]] = None, timeout: float = 4.0) -> Any:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Feed adapters. Each returns None on any failure so a single dead feed never
# raises into the trading loop; the impulse gate decides what to do with a
# missing feed.
# --------------------------------------------------------------------------
class Feed:
    name = "feed"

    def spot(self) -> Optional[float]:
        raise NotImplementedError

    def slot_open(self, slot_start: int) -> Optional[float]:
        raise NotImplementedError


class BinanceFeed(Feed):
    name = "binance"

    def spot(self) -> Optional[float]:
        try:
            j = _get_json(
                "https://api.binance.com/api/v3/ticker/price",
                {"symbol": "BTCUSDT"},
            )
            return float(j["price"])
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        try:
            kl = _get_json(
                "https://api.binance.com/api/v3/klines",
                {
                    "symbol": "BTCUSDT",
                    "interval": "5m",
                    "startTime": slot_start * 1000,
                    "limit": 1,
                },
            )
            # kline row: [openTime, open, high, low, close, ...]
            if kl and int(kl[0][0]) == slot_start * 1000:
                return float(kl[0][1])
        except Exception:
            return None
        return None


class CoinbaseFeed(Feed):
    name = "coinbase"

    def spot(self) -> Optional[float]:
        try:
            j = _get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            return float(j["price"])
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        try:
            rows = _get_json(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                {
                    "granularity": SLOT_SECONDS,
                    "start": slot_start,
                    "end": slot_start + SLOT_SECONDS,
                },
            )
            # candle row: [time, low, high, open, close, volume], newest first
            for row in rows or []:
                if int(row[0]) == slot_start:
                    return float(row[3])
        except Exception:
            return None
        return None


class KrakenFeed(Feed):
    name = "kraken"

    def spot(self) -> Optional[float]:
        try:
            j = _get_json(
                "https://api.kraken.com/0/public/Ticker",
                {"pair": "XBTUSD"},
            )
            result = (j or {}).get("result") or {}
            pair = next(iter(result.values()), None)
            if pair:
                # 'c' = last trade closed [price, lot volume]
                return float(pair["c"][0])
        except Exception:
            return None
        return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        try:
            j = _get_json(
                "https://api.kraken.com/0/public/OHLC",
                {"pair": "XBTUSD", "interval": 5, "since": slot_start - SLOT_SECONDS},
            )
            result = (j or {}).get("result") or {}
            for key, rows in result.items():
                if key == "last":
                    continue
                for row in rows or []:
                    # OHLC row: [time, open, high, low, close, vwap, volume, count]
                    if int(row[0]) == slot_start:
                        return float(row[1])
        except Exception:
            return None
        return None


FEEDS: dict[str, type[Feed]] = {
    "binance": BinanceFeed,
    "coinbase": CoinbaseFeed,
    "kraken": KrakenFeed,
}


def build_feeds(names: list[str]) -> list[Feed]:
    feeds: list[Feed] = []
    for n in names:
        cls = FEEDS.get(n.strip().lower())
        if cls is not None:
            feeds.append(cls())
    return feeds


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def evaluate_impulse(
    feeds: list[Feed],
    *,
    min_usd: float = 70.0,
    max_usd: float = 0.0,
    divergence_usd: float = 60.0,
    min_feeds: int = 2,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Measure the BTC move inside the current 5m round across multiple feeds.

    Returns a dict describing the decision. Key fields:
        ok          -> bool, all gates passed
        direction   -> 'UP' | 'DOWN' | None (consensus move direction)
        move_usd    -> signed consensus move (conservative magnitude, signed)
        reason      -> short machine reason when ok is False
        feeds       -> per-feed {open, spot, move} for logging
        spot_spread -> max-min of feed spots (the lag/divergence measure)

    Gates:
        1. At least `min_feeds` feeds return both round-open and spot.
        2. All valid feeds agree on move direction (up vs down).
        3. Feed spots agree within `divergence_usd` (guards against one lagging).
        4. Move magnitude >= min_usd  (impulse is meaningful).
        5. If max_usd > 0: move magnitude <= max_usd (not overextended).
    """
    now = int(now_ts if now_ts is not None else time.time())
    slot_start = bucket_5m(now)

    per_feed: dict[str, dict[str, Any]] = {}
    valid: list[tuple[str, float, float]] = []  # (name, spot, signed_move)
    for f in feeds:
        open_px = f.slot_open(slot_start)
        spot_px = f.spot()
        move = None
        if open_px is not None and spot_px is not None:
            move = spot_px - open_px
            valid.append((f.name, spot_px, move))
        per_feed[f.name] = {"open": open_px, "spot": spot_px, "move": move}

    base = {
        "slot_start": slot_start,
        "feeds": per_feed,
        "valid_feed_count": len(valid),
        "min_feeds": min_feeds,
        "min_usd": min_usd,
        "max_usd": max_usd,
        "divergence_usd": divergence_usd,
    }

    if len(valid) < min_feeds:
        return {**base, "ok": False, "direction": None, "move_usd": None,
                "spot_spread": None, "reason": "insufficient_feeds"}

    spots = [s for _, s, _ in valid]
    moves = [m for _, _, m in valid]
    spot_spread = max(spots) - min(spots)

    signs = {_sign(m) for m in moves}
    if len(signs) > 1 or signs == {0}:
        return {**base, "ok": False, "direction": None, "move_usd": None,
                "spot_spread": spot_spread, "reason": "feed_direction_conflict"}

    if spot_spread > divergence_usd:
        return {**base, "ok": False, "direction": None, "move_usd": None,
                "spot_spread": spot_spread, "reason": "feed_price_divergence"}

    direction = "UP" if moves[0] > 0 else "DOWN"
    mags = [abs(m) for m in moves]
    conservative_mag = min(mags)   # trust the slower feed for the floor
    extended_mag = max(mags)       # trust the faster feed for the cap
    signed_move = conservative_mag if direction == "UP" else -conservative_mag

    if conservative_mag < min_usd:
        return {**base, "ok": False, "direction": direction, "move_usd": signed_move,
                "spot_spread": spot_spread, "reason": "move_below_min"}

    if max_usd > 0 and extended_mag > max_usd:
        return {**base, "ok": False, "direction": direction, "move_usd": signed_move,
                "spot_spread": spot_spread, "reason": "move_above_max"}

    return {**base, "ok": True, "direction": direction, "move_usd": signed_move,
            "spot_spread": spot_spread, "reason": "impulse_ok"}
