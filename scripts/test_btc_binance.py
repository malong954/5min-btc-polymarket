#!/usr/bin/env python3
"""
Offline tests for the Binance connector. No network: a fake page-fetcher
generates deterministic klines so pagination, de-dup, CSV writing, and
integration with the backtest loader are all verified without hitting Binance.
Run: python3 scripts/test_btc_binance.py
"""

import csv
import os
import tempfile

from btc_binance import (
    INTERVAL_MS,
    download_history,
    parse_klines,
    write_csv,
)
from btc_backtest import load_csv, run_backtest


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def make_fake_fetcher(total_bars, step_ms, start_ms, price0=100_000.0):
    """Return a page_fetcher that serves `total_bars` 1m klines from a synthetic
    ramp, paginating at 1000 rows per call like the real API."""
    all_rows = []
    px = price0
    for i in range(total_bars):
        o = px
        c = px + (1.0 if i % 2 == 0 else -1.0)
        h, l = max(o, c) + 0.5, min(o, c) - 0.5
        all_rows.append([start_ms + i * step_ms, f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}", "3.0",
                         start_ms + (i + 1) * step_ms - 1, "0", 0, "0", "0", "0"])
        px = c

    def fetcher(symbol, interval, start, base_list, limit):
        # Serve up to `limit` rows whose openTime >= start.
        page = [r for r in all_rows if r[0] >= start][:limit]
        return page

    return fetcher, all_rows


def test_parse_klines():
    raw = [[1_700_000_000_000, "100.5", "101.0", "100.0", "100.8", "12.5",
            1_700_000_059_999, "0", 0, "0", "0", "0"]]
    p = parse_klines(raw)
    check("kline openTime ms -> unix seconds", p[0]["time"] == 1_700_000_000)
    check("kline OHLCV parsed as floats",
          p[0]["open"] == 100.5 and p[0]["close"] == 100.8 and p[0]["volume"] == 12.5)


def test_pagination_and_dedup():
    step = INTERVAL_MS["1m"]
    start = 1_699_999_800_000  # aligned to a 5m boundary (300_000 ms), like real klines
    total = 2500  # forces 3 pages (1000 + 1000 + 500)
    fetcher, all_rows = make_fake_fetcher(total, step, start)
    # end pinned so `days` covers exactly the generated span.
    now_ms = start + total * step
    days = (total * step) / 86_400_000
    rows = download_history("BTCUSDT", "1m", days=days, now_ms=now_ms,
                            page_fetcher=fetcher, sleep_sec=0.0)
    check("all bars fetched across pages", len(rows) == total)
    times = [r["time"] for r in rows]
    check("bars sorted ascending", times == sorted(times))
    check("no duplicate timestamps", len(set(times)) == len(times))
    check("spacing is exactly 60s", all(times[i + 1] - times[i] == 60 for i in range(len(times) - 1)))


def test_no_forward_progress_stops():
    """If the API keeps returning the same last bar, the loop must terminate."""
    start = 1_699_999_800_000  # aligned to a 5m boundary (300_000 ms), like real klines

    def stuck_fetcher(symbol, interval, s, base_list, limit):
        # Always returns the same single bar regardless of cursor.
        return [[start, "1", "1", "1", "1", "1", start + 59_999, "0", 0, "0", "0", "0"]]

    rows = download_history("BTCUSDT", "1m", days=5, now_ms=start + 10 * 86_400_000,
                            page_fetcher=stuck_fetcher, sleep_sec=0.0)
    check("stuck API terminates without hanging", len(rows) == 1)


def test_csv_roundtrip_into_backtest():
    step = INTERVAL_MS["1m"]
    start = 1_699_999_800_000  # aligned to a 5m boundary (300_000 ms), like real klines
    total = 1500
    fetcher, _ = make_fake_fetcher(total, step, start)
    now_ms = start + total * step
    days = (total * step) / 86_400_000
    rows = download_history("BTCUSDT", "1m", days=days, now_ms=now_ms,
                            page_fetcher=fetcher, sleep_sec=0.0)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "btc_1m.csv")
        n = write_csv(rows, path)
        check("csv wrote every row", n == total)
        with open(path) as f:
            header = next(csv.reader(f))
        check("csv header matches backtest loader", header == ["time", "open", "high", "low", "close", "volume"])
        # The written CSV must load and backtest without error.
        bars = load_csv(path)
        check("backtest loader reads connector CSV", len(bars) == total)
        res = run_backtest(bars, entry_threshold=0.15)
        check("backtest runs on connector data", res["rounds_5m"] > 0 and 0.0 <= res["directional_accuracy"] <= 1.0)


def main():
    test_parse_klines()
    test_pagination_and_dedup()
    test_no_forward_progress_stops()
    test_csv_roundtrip_into_backtest()
    print("\nAll Binance connector tests passed.")


if __name__ == "__main__":
    main()
