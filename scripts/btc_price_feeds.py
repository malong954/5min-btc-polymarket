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


# One keep-alive session per process: without it every poll pays a fresh
# TCP+TLS handshake (~100-300ms) — pure latency for zero benefit.
_SESSION = requests.Session()


def _get_json(url: str, params: Optional[dict[str, Any]] = None, timeout: float = 4.0) -> Any:
    r = _SESSION.get(url, params=params, timeout=timeout)
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
    # api.binance.com returns HTTP 451 for US IPs; the .vision data mirror is
    # open (same API, market data only). Same fallback order as btc_binance —
    # this feed being pinned to the blocked host is what silenced the recorder
    # for two days (spot/slot_open quietly None on a US network).
    BASES = [
        "https://data-api.binance.vision",
        "https://api.binance.com",
        "https://api.binance.us",
    ]

    def __init__(self, symbol: str = "BTCUSDT") -> None:
        self.symbol = symbol
        self._base: Optional[str] = None   # sticky: first base that answered

    def _get(self, path: str, params: dict) -> Any:
        order = ([self._base] if self._base else []) + [b for b in self.BASES if b != self._base]
        for b in order:
            try:
                j = _get_json(b + path, params)
                self._base = b
                return j
            except Exception:
                continue
        return None

    def spot(self) -> Optional[float]:
        try:
            j = self._get("/api/v3/ticker/price", {"symbol": self.symbol})
            return float(j["price"]) if j else None
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        try:
            kl = self._get(
                "/api/v3/klines",
                {
                    "symbol": self.symbol,
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


class CryptoCompareFeed(Feed):
    """Keyless (rate-limited). Provides both spot and 1m round-open, so it can
    be used as a full cross-check feed in the impulse gate."""
    name = "cryptocompare"

    def spot(self) -> Optional[float]:
        try:
            j = _get_json(
                "https://min-api.cryptocompare.com/data/price",
                {"fsym": "BTC", "tsyms": "USD"},
            )
            return float(j["USD"])
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        try:
            j = _get_json(
                "https://min-api.cryptocompare.com/data/v2/histominute",
                {"fsym": "BTC", "tsym": "USD", "limit": 6, "toTs": slot_start + SLOT_SECONDS},
            )
            for row in ((j or {}).get("Data") or {}).get("Data") or []:
                if int(row.get("time", -1)) == slot_start:
                    return float(row["open"])
        except Exception:
            return None
        return None


class CoinGeckoFeed(Feed):
    """Keyless live spot. No keyless 1m round-open, so this contributes to a
    live cross-check display but not to the round gate (slot_open -> None)."""
    name = "coingecko"

    def spot(self) -> Optional[float]:
        try:
            j = _get_json(
                "https://api.coingecko.com/api/v3/simple/price",
                {"ids": "bitcoin", "vs_currencies": "usd"},
            )
            return float(j["bitcoin"]["usd"])
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        return None


class DiaFeed(Feed):
    """Keyless live spot (DIAdata). Spot-only for the gate's purposes."""
    name = "dia"

    def spot(self) -> Optional[float]:
        try:
            j = _get_json("https://api.diadata.org/v1/assetQuotation/Bitcoin/0x0000000000000000000000000000000000000000")
            return float(j["Price"])
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        return None


class LiveCoinWatchFeed(Feed):
    """Live spot. Requires a free API key in env LIVECOINWATCH_API_KEY."""
    name = "livecoinwatch"

    def spot(self) -> Optional[float]:
        key = os.getenv("LIVECOINWATCH_API_KEY")
        if not key:
            return None
        try:
            r = requests.post(
                "https://api.livecoinwatch.com/coins/single",
                headers={"content-type": "application/json", "x-api-key": key},
                json={"currency": "USD", "code": "BTC", "meta": False},
                timeout=4.0,
            )
            r.raise_for_status()
            return float(r.json()["rate"])
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        return None


class CoinMarketCapFeed(Feed):
    """Live spot. Requires an API key in env COINMARKETCAP_API_KEY."""
    name = "coinmarketcap"

    def spot(self) -> Optional[float]:
        key = os.getenv("COINMARKETCAP_API_KEY")
        if not key:
            return None
        try:
            r = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={"X-CMC_PRO_API_KEY": key},
                params={"symbol": "BTC", "convert": "USD"},
                timeout=4.0,
            )
            r.raise_for_status()
            return float(r.json()["data"]["BTC"]["quote"]["USD"]["price"])
        except Exception:
            return None

    def slot_open(self, slot_start: int) -> Optional[float]:
        return None


FEEDS: dict[str, type[Feed]] = {
    "binance": BinanceFeed,
    "coinbase": CoinbaseFeed,
    "kraken": KrakenFeed,
    "cryptocompare": CryptoCompareFeed,
    "coingecko": CoinGeckoFeed,
    "dia": DiaFeed,
    "livecoinwatch": LiveCoinWatchFeed,
    "coinmarketcap": CoinMarketCapFeed,
}

# Feeds that can supply the current 5m round-open (usable as full gate feeds).
# The others are spot-only: fine for a live cross-check display, but they can't
# anchor the intra-round move so the gate ignores them.
GATE_CAPABLE_FEEDS = ["binance", "coinbase", "kraken", "cryptocompare"]


def build_feeds(names: list[str], symbol: str = "BTCUSDT") -> list[Feed]:
    """Build feed adapters. `symbol` selects the traded pair for feeds that
    support it (Binance); other adapters keep their hardcoded BTC pairs, so use
    provider=binance for non-BTC assets."""
    feeds: list[Feed] = []
    for n in names:
        cls = FEEDS.get(n.strip().lower())
        if cls is None:
            continue
        if cls is BinanceFeed:
            feeds.append(cls(symbol=symbol))
        else:
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
