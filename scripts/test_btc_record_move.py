#!/usr/bin/env python3
"""Offline tests: the recorder's `move` must come from ONE price feed.

Regression for the 2026-10-05 finding: `move` was Binance spot minus the
round open, and the open switched ~2 min into each round from the Binance
kline to the Chainlink candle, injecting the Binance-vs-Chainlink basis
(median +$27) into every later sample. Here a fake Binance feed and a fake
Chainlink feed sit $30 apart and lag realistically; the recorder runs a full
round on a fake clock, and every sample's move must equal Binance spot minus
the Binance open, with the basis visible only in the audit field.

Run: python3 scripts/test_btc_record_move.py
"""
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

R = 1_800_000_000          # round start, aligned to 300s
OPEN = 100_000.0           # Binance price at R
BASIS = 30.0               # Chainlink sits $30 below Binance
DRIFT = 0.5                # Binance rises $0.50/s


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def binance_px(t):
    return OPEN + DRIFT * (t - R)


def run(start_offset, slot_open_works=True, chainlink=True, steps=80):
    clock = {"t": float(R + start_offset)}

    class FakeFeed:
        def spot(self):
            return binance_px(clock["t"])

        def slot_open(self, r):
            return binance_px(r) if slot_open_works else None

    feeds = types.ModuleType("btc_price_feeds")
    feeds.build_feeds = lambda providers, symbol=None: [FakeFeed()]
    cl = types.ModuleType("chainlink_feed")
    cl.candle_creds_ok = lambda: chainlink

    def candle_open_at(asset, ts):
        # Chainlink publishes a candle ~20s after its boundary.
        if clock["t"] < ts + 20:
            return None
        return binance_px(ts) - BASIS

    cl.candle_open_at = candle_open_at
    pm = types.ModuleType("btc_polymarket")
    pm.current_prices = lambda now, asset="btc": {
        "UP": 0.6, "DOWN": 0.42, "UP_size": 50, "DOWN_size": 50,
        "UP_bid": 0.58, "DOWN_bid": 0.4, "UP_bid_size": 50, "DOWN_bid_size": 50}
    pm.current_slug = lambda rs, asset="btc": f"btc-{rs}"
    pm.resolved_outcome = lambda slug: None
    hist = types.ModuleType("btc_history")

    def no_history(*a, **k):
        raise RuntimeError("offline")

    hist.fetch_history = no_history
    for name, mod in (("btc_price_feeds", feeds), ("chainlink_feed", cl),
                      ("btc_polymarket", pm), ("btc_history", hist)):
        sys.modules[name] = mod

    real_time, real_sleep = time.time, time.sleep
    time.time = lambda: clock["t"]

    def fake_sleep(s):
        clock["t"] += s

    time.sleep = fake_sleep
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        import btc_record
        btc_record.main(["--log", path, "--max-steps", str(steps), "--poll", "5"])
    finally:
        time.time, time.sleep = real_time, real_sleep
    events = [json.loads(line) for line in open(path)]
    os.remove(path)
    return events


def test_move_is_same_feed_through_the_chainlink_switch():
    ev = run(start_offset=2)
    samples = [e for e in ev if e["type"] == "sample" and e["round"] == R]
    check("recorder produced samples for the round", len(samples) > 20)
    exact = all(abs(s["move"] - (s["spot"] - OPEN)) < 0.02 for s in samples)
    check("every move == Binance spot - Binance open (no basis)", exact)
    check("move_src marks the kline open", all(s.get("move_src") == "kline" for s in samples))
    switched = [s for s in samples if s.get("move_xfeed") is not None
                and abs(s["move_xfeed"] - s["move"] - BASIS) < 0.02]
    check("the old mixed value still carries the $30 basis (audit field)", len(switched) > 10)
    res = [e for e in ev if e["type"] == "result" and e["round"] == R]
    check("result grades on the Chainlink open", res and abs(res[0]["open"] - (OPEN - BASIS)) < 0.02)
    check("result records the same-feed open too", res and abs(res[0]["open_feed"] - OPEN) < 0.02)


def test_spot_standin_only_in_the_first_seconds():
    ev = run(start_offset=2, slot_open_works=False, chainlink=False)
    samples = [e for e in ev if e["type"] == "sample" and e["round"] == R]
    check("young round with no kline uses the first spot", len(samples) > 20
          and all(s.get("move_src") == "spot" for s in samples))
    check("spot stand-in is the price at that first poll",
          all(abs(s["move"] - (s["spot"] - binance_px(R + 2))) < 0.02 for s in samples))


def test_no_fabricated_open_when_joining_late():
    ev = run(start_offset=60, slot_open_works=False, chainlink=False, steps=30)
    samples = [e for e in ev if e["type"] == "sample" and e["round"] == R]
    check("joining a round late with no kline emits no samples (no fake open)", len(samples) == 0)


def main():
    test_move_is_same_feed_through_the_chainlink_switch()
    sys.modules.pop("btc_record", None)
    test_spot_standin_only_in_the_first_seconds()
    sys.modules.pop("btc_record", None)
    test_no_fabricated_open_when_joining_late()
    print("all tests passed")


if __name__ == "__main__":
    main()
