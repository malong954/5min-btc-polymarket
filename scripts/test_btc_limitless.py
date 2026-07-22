#!/usr/bin/env python3
"""
Offline tests for the Limitless price/label feed. No network: the orderbook,
active-slugs, market-detail and oracle-candle responses are stubbed. Run:
    python3 scripts/test_btc_limitless.py
"""

from btc_limitless import (
    current_slug, bucket_window, prices_from_orderbook, resolve_active_slug,
    current_prices, settle_from_candles, resolved_outcome,
)


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def test_slug_and_bucket():
    ts = 1_700_000_123
    check("15m slug uses the confirmed convention + 900s bucket",
          current_slug(ts, "btc", "15m") == f"btc-up-or-down-15-min-{ts - (ts % 900)}")
    check("5m slug uses the 300s bucket",
          current_slug(ts, "eth", "5m") == f"eth-up-or-down-5-min-{ts - (ts % 300)}")
    check("hourly slug supported",
          current_slug(ts, "btc", "1h") == f"btc-up-or-down-hourly-{ts - (ts % 3600)}")
    check("bucket floors to the slot", bucket_window(950, "15m") == 900)


def test_complement_math():
    # YES book: best ask 0.63 (size 120), best bid 0.60 (size 200).
    raw = {"bids": [{"price": "0.58", "size": "50"}, {"price": "0.60", "size": "200"}],
           "asks": [{"price": "0.63", "size": "120"}, {"price": "0.66", "size": "80"}]}
    px = prices_from_orderbook(raw)
    check("UP ask = best YES ask", abs(px["UP"] - 0.63) < 1e-9)
    check("UP bid = best YES bid", abs(px["UP_bid"] - 0.60) < 1e-9)
    # DOWN is the complement: DOWN ask = 1 - YES bid, DOWN bid = 1 - YES ask.
    check("DOWN ask = 1 - best YES bid", abs(px["DOWN"] - 0.40) < 1e-9)
    check("DOWN bid = 1 - best YES ask", abs(px["DOWN_bid"] - 0.37) < 1e-9)
    # up_ask + down_ask should exceed 1 by the book's spread -> a real overround,
    # directly comparable to the Polymarket sum-of-asks monitor.
    check("UP ask + DOWN ask reflects the spread as overround",
          abs((px["UP"] + px["DOWN"]) - 1.03) < 1e-9)


def test_orderbook_shape_tolerance():
    # [[price, size], ...] pair form, and buy/sell key spelling.
    raw = {"buy": [[0.55, 10], [0.57, 5]], "sell": [[0.62, 7], [0.60, 9]]}
    px = prices_from_orderbook(raw)
    check("pair-form asks take the min price", abs(px["UP"] - 0.60) < 1e-9)
    check("pair-form bids take the max price", abs(px["UP_bid"] - 0.57) < 1e-9)
    check("empty book yields None, not a crash",
          prices_from_orderbook({})["UP"] is None)


def test_resolve_active_slug_picks_current_bucket():
    now = 1_700_000_123
    cur = bucket_window(int(now), "15m")
    # Live shape: list of DICTS with slug/strikePrice/ticker/deadline.
    slugs = [{"slug": f"btc-up-or-down-15-min-{cur - 900}", "ticker": "BTC"},
             {"slug": f"btc-up-or-down-15-min-{cur}", "ticker": "BTC"},
             {"slug": f"eth-up-or-down-15-min-{cur}", "ticker": "ETH"},
             {"slug": "btc-up-or-down-15-min-not-a-number"},
             "btc-up-or-down-hourly-1784682000"]

    def getter(url, params=None, timeout=8.0):
        check("active-slugs endpoint hit", url.endswith("/markets/active/slugs"))
        return slugs

    s = resolve_active_slug(now, "btc", "15m", getter)
    check("resolves the CURRENT-bucket btc slug", s == f"btc-up-or-down-15-min-{cur}")
    # Wrapped in {"slugs": [...]} it should still work.
    s2 = resolve_active_slug(now, "btc", "15m",
                             lambda u, params=None, timeout=8.0: {"slugs": slugs})
    check("tolerates a {slugs:[...]} envelope", s2 == f"btc-up-or-down-15-min-{cur}")
    # No match -> None (not a crash, not a wrong-asset slug).
    s3 = resolve_active_slug(now, "sol", "15m", getter)
    check("no matching asset -> None", s3 is None)


def test_current_prices_uses_resolved_slug():
    now = 1_700_000_123
    cur = bucket_window(int(now), "15m")
    live = f"btc-up-or-down-15-min-{cur}"

    def getter(url, params=None, timeout=8.0):
        if url.endswith("/markets/active/slugs"):
            return [{"slug": live, "ticker": "BTC"}]
        if url.endswith(f"/markets/{live}/orderbook"):
            return {"bids": [{"price": 0.70, "size": 100}],
                    "asks": [{"price": 0.72, "size": 100}]}
        raise AssertionError(f"unexpected url {url}")

    p = current_prices(now, getter, asset="btc", window="15m")
    check("current_prices resolves the live slug", p and p["slug"] == live)
    check("current_prices tags the venue", p["venue"] == "limitless")
    check("current_prices maps UP ask", abs(p["UP"] - 0.72) < 1e-9)
    check("current_prices maps DOWN ask (1 - YES bid)", abs(p["DOWN"] - 0.30) < 1e-9)


def test_settle_from_candles():
    rs = 1_700_000_100  # a 15m bucket start
    # Three 5m candles inside the round: open 100 -> close 130 => UP.
    up = [{"t": rs, "o": 100, "c": 110}, {"t": rs + 300, "o": 110, "c": 120},
          {"t": rs + 600, "o": 120, "c": 130}]
    check("close > open across the round -> UP", settle_from_candles(up, rs, "15m") == "UP")
    dn = [{"t": rs, "o": 130, "c": 120}, {"t": rs + 600, "o": 120, "c": 110}]
    check("close < open -> DOWN", settle_from_candles(dn, rs, "15m") == "DOWN")
    # Candle outside the round window is ignored -> no coverage -> None.
    check("round not covered -> None",
          settle_from_candles([{"t": rs + 5000, "o": 1, "c": 2}], rs, "15m") is None)
    # Millisecond timestamps are normalized.
    ms = [{"t": rs * 1000, "o": 100, "c": 90}, {"t": (rs + 600) * 1000, "o": 90, "c": 80}]
    check("millisecond stamps handled -> DOWN", settle_from_candles(ms, rs, "15m") == "DOWN")


def test_resolved_outcome_prefers_market_field():
    def getter_wi(url, params=None, timeout=8.0):
        return {"winningOutcomeIndex": 1,
                "tokens": [{"title": "Up"}, {"title": "Down"}]}
    check("winningOutcomeIndex maps through token labels",
          resolved_outcome("btc-up-or-down-15-min-1700000100", getter_wi) == "DOWN")

    def getter_wi_bare(url, params=None, timeout=8.0):
        return {"winningOutcomeIndex": 0}
    check("bare winningOutcomeIndex 0 -> UP (YES-book convention)",
          resolved_outcome("btc-up-or-down-15-min-1700000100", getter_wi_bare) == "UP")

    def getter(url, params=None, timeout=8.0):
        return {"winningOutcome": "Down"}
    check("resolved_outcome reads the winning-side field",
          resolved_outcome("btc-up-or-down-15-min-1700000100", getter) == "DOWN")

    def getter_prices(url, params=None, timeout=8.0):
        return {"outcomePrices": ["1.0", "0.0"]}
    check("decisive outcomePrices -> UP",
          resolved_outcome("btc-up-or-down-15-min-1700000100", getter_prices) == "UP")

    def getter_unresolved(url, params=None, timeout=8.0):
        if url.endswith("/oracle-candles"):
            rs = 1_700_000_100
            return [{"t": rs, "o": 100, "c": 130}]
        return {"status": "open"}   # not yet resolved -> fall through to candles
    check("unresolved market falls back to candle-settle",
          resolved_outcome("btc-up-or-down-15-min-1700000100", getter_unresolved) == "UP")


def main():
    test_slug_and_bucket()
    test_complement_math()
    test_orderbook_shape_tolerance()
    test_resolve_active_slug_picks_current_bucket()
    test_current_prices_uses_resolved_slug()
    test_settle_from_candles()
    test_resolved_outcome_prefers_market_field()
    print("\nAll Limitless feed tests passed.")


if __name__ == "__main__":
    main()
