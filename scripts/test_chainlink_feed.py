#!/usr/bin/env python3
"""Offline tests for the vendored Chainlink feed module. Run:
    python3 scripts/test_chainlink_feed.py
Network paths are not exercised — these pin the parsing/decoding traps the
handoff doc paid for: newest-first rows, 18-decimal fixed point, boundary
selection, v3 report layout, and the settle tie rule (Up iff close >= open).
"""

import chainlink_feed as cf


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def test_candle_parse_array_newest_first_and_scaling():
    # Rows NEWEST-first, prices 18-decimal fixed point (raw 6.28e22 = $62,811).
    j = {"candles": [
        [1000 + 120, 6.30e22, 0, 0, 0, 0],
        [1000 + 60, 6.29e22, 0, 0, 0, 0],
        [1000, 6.28e22, 0, 0, 0, 0],
        [1000 - 60, 6.27e22, 0, 0, 0, 0],   # pre-boundary: must not be chosen
    ]}
    v = cf._candle_first_open(j, 1000)
    check("earliest candle at/after boundary chosen (not rows[0])", v is not None)
    check("18-decimal scaling applied", abs(v - 62800.0) < 1.0)
    check("pre-boundary candle not substituted while later ones exist",
          abs(v - 62700.0) > 50)


def test_candle_parse_other_shapes():
    col = {"t": [2000, 1940], "o": [101.5, 100.5]}
    check("columnar shape parsed", cf._candle_first_open(col, 2000) == 101.5)
    rows = {"rows": [{"t": 3060, "o": 55.5}, {"t": 3000, "o": 54.5}]}
    check("dict-row shape parsed", cf._candle_first_open(rows, 3000) == 54.5)
    check("small prices not rescaled", cf._candle_first_open(col, 2000) < 1e6)
    check("garbage returns None", cf._candle_first_open({"x": 1}, 0) is None)
    check("empty returns None", cf._candle_first_open({"candles": []}, 0) is None)
    # nothing at/after min_ts -> latest available (caller decides to retry)
    late = {"candles": [[500, 42.0, 0, 0, 0, 0]]}
    check("falls back to latest pre-boundary candle", cf._candle_first_open(late, 900) == 42.0)


def test_decode_v3_report():
    # Build a synthetic fullReport: 96 bytes of ABI head, offset word (=128),
    # padding to the offset, length word (=288), then 9 static 32-byte words.
    def w(v, signed=False):
        return int(v).to_bytes(32, "big", signed=signed)

    feed_id = bytes.fromhex("00" * 31 + "42")
    words = (feed_id + w(1111) + w(1784000000) + w(0) + w(0) + w(1784000600)
             + w(int(62787.93e18), signed=True)
             + w(int(62787.00e18), signed=True)
             + w(int(62789.00e18), signed=True))
    blob = b"\x00" * 96 + w(128) + w(len(words)) + words
    rep = cf.decode_v3_report(blob.hex())
    check("v3 price decoded", abs(rep["price"] - 62787.93) < 0.01)
    check("v3 bid/ask decoded", rep["bid"] < rep["price"] < rep["ask"])
    check("v3 observation ts decoded", rep["observations_ts"] == 1784000000)


def test_settle_up_tie_rule():
    # Market rule: Up iff close >= open — ties are UP (our old spot rule broke
    # ties DOWN, which caused measured label flips).
    orig = cf.spot_open_at
    try:
        prices = {}
        cf.spot_open_at = lambda a, ts: prices.get(ts)
        prices.update({100: 50.0, 400: 51.0})
        check("up settle", cf.settle_up("btc", 100, 400) is True)
        prices.update({100: 50.0, 400: 50.0})
        check("tie settles UP (market rule)", cf.settle_up("btc", 100, 400) is True)
        prices.update({100: 50.0, 400: 49.0})
        check("down settle", cf.settle_up("btc", 100, 400) is False)
        prices.clear()
        check("missing data -> None (caller retries)", cf.settle_up("btc", 100, 400) is None)
    finally:
        cf.spot_open_at = orig


def test_creds_detection_off_by_default():
    import os
    for k in ("CHAINLINK_CANDLE_LOGIN", "CHAINLINK_CANDLE_API_KEY",
              "CHAINLINK_STREAMS_API_KEY"):
        os.environ.pop(k, None)
    check("no creds -> candle path off", cf.candle_creds_ok() is False)
    check("no creds -> candle_open_at returns None (no network attempted)",
          cf.candle_open_at("btc", 1784000000) is None)
    check("no feed id -> streams settle None", cf.streams_latest("btc") is None)


def main():
    test_candle_parse_array_newest_first_and_scaling()
    test_candle_parse_other_shapes()
    test_decode_v3_report()
    test_settle_up_tie_rule()
    test_creds_detection_off_by_default()
    print("\nAll chainlink feed tests passed.")


if __name__ == "__main__":
    main()
