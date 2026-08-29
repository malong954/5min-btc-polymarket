#!/usr/bin/env python3
"""
Offline unit tests for the two-feed BTC impulse gate.

Runs with fake feeds (no network) so the decision logic is verified
deterministically. Run: python3 scripts/test_btc_impulse_feeds.py
"""

from btc_price_feeds import evaluate_impulse


class FakeFeed:
    def __init__(self, name, open_px, spot_px):
        self.name = name
        self._open = open_px
        self._spot = spot_px

    def spot(self):
        return self._spot

    def slot_open(self, slot_start):
        return self._open


def check(desc, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {desc}")
    if not cond:
        raise SystemExit(1)


def main():
    NOW = 1_700_000_000  # fixed timestamp; slot logic is deterministic

    # 1. Clean $85 up-move, both feeds agree -> enter UP.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_085), FakeFeed("coinbase", 100_002, 100_086)],
        min_usd=70, max_usd=0, divergence_usd=60, min_feeds=2, now_ts=NOW,
    )
    check("clean $85 up-move -> ok/UP", r["ok"] and r["direction"] == "UP")

    # 2. Move too small ($40 < 70) -> reject.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_040), FakeFeed("coinbase", 100_000, 100_041)],
        min_usd=70, max_usd=0, divergence_usd=60, min_feeds=2, now_ts=NOW,
    )
    check("small $40 move -> reject move_below_min", not r["ok"] and r["reason"] == "move_below_min")

    # 3. Uses the SLOWER feed for the floor: one feed +90, other +30 -> min 30 < 70 reject.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_090), FakeFeed("coinbase", 100_000, 100_030)],
        min_usd=70, max_usd=0, divergence_usd=120, min_feeds=2, now_ts=NOW,
    )
    check("conservative floor uses lagging feed -> reject", not r["ok"] and r["reason"] == "move_below_min")

    # 4. Feeds disagree on direction (one up, one down) -> reject.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_080), FakeFeed("coinbase", 100_000, 99_920)],
        min_usd=70, max_usd=0, divergence_usd=500, min_feeds=2, now_ts=NOW,
    )
    check("direction conflict -> reject", not r["ok"] and r["reason"] == "feed_direction_conflict")

    # 5. Feeds too far apart in spot (lag) -> reject even if both up.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_090), FakeFeed("coinbase", 100_000, 100_200)],
        min_usd=70, max_usd=0, divergence_usd=60, min_feeds=2, now_ts=NOW,
    )
    check("feed spot divergence -> reject", not r["ok"] and r["reason"] == "feed_price_divergence")

    # 6. Overextension cap: $150 move with max_usd=100 -> reject.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_150), FakeFeed("coinbase", 100_000, 100_150)],
        min_usd=70, max_usd=100, divergence_usd=60, min_feeds=2, now_ts=NOW,
    )
    check("overextended $150 with cap 100 -> reject", not r["ok"] and r["reason"] == "move_above_max")

    # 7. Only one feed available but min_feeds=2 -> reject (can't cross-check).
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_090), FakeFeed("coinbase", None, None)],
        min_usd=70, max_usd=0, divergence_usd=60, min_feeds=2, now_ts=NOW,
    )
    check("one dead feed, min_feeds=2 -> reject", not r["ok"] and r["reason"] == "insufficient_feeds")

    # 8. Down-move confirmed -> DOWN.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 99_915), FakeFeed("coinbase", 100_000, 99_916)],
        min_usd=70, max_usd=0, divergence_usd=60, min_feeds=2, now_ts=NOW,
    )
    check("clean $85 down-move -> ok/DOWN", r["ok"] and r["direction"] == "DOWN")

    # 9. Single-feed mode (min_feeds=1) works when only one feed is up.
    r = evaluate_impulse(
        [FakeFeed("binance", 100_000, 100_090), FakeFeed("coinbase", None, None)],
        min_usd=70, max_usd=0, divergence_usd=60, min_feeds=1, now_ts=NOW,
    )
    check("min_feeds=1 single feed -> ok/UP", r["ok"] and r["direction"] == "UP")

    print("\nAll impulse-gate tests passed.")


if __name__ == "__main__":
    main()
