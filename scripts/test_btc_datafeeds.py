#!/usr/bin/env python3
"""
Offline tests for the multi-provider data layer (history + live feeds).
No network: HTTP is stubbed. Run: python3 scripts/test_btc_datafeeds.py
"""

import btc_price_feeds as pf
from btc_history import history_cryptocompare, fetch_history
from btc_price_feeds import (
    CryptoCompareFeed,
    CoinGeckoFeed,
    DiaFeed,
    build_feeds,
    evaluate_impulse,
    GATE_CAPABLE_FEEDS,
    FEEDS,
)


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def test_history_cryptocompare_parsing_and_pagination():
    # Two pages of minute bars, newest-first API returns ascending within page.
    step = 60
    base_t = 1_700_000_000 - (1_700_000_000 % 300)
    total = 3000
    all_bars = [{
        "time": base_t + i * step, "open": 100000 + i, "high": 100000 + i + 1,
        "low": 100000 + i - 1, "close": 100000 + i + 0.5, "volumeto": 10.0,
    } for i in range(total)]

    def fake_getter(url, params=None, timeout=15.0):
        to_ts = params["toTs"]
        limit = params["limit"]
        upto = [b for b in all_bars if b["time"] <= to_ts]
        page = upto[-limit:] if len(upto) > limit else upto
        return {"Data": {"Data": page}}

    now_ts = base_t + total * step
    days = (total * step) / 86_400
    rows = history_cryptocompare(days=days, now_ts=now_ts, getter=fake_getter, sleep_sec=0.0)
    times = [r["time"] for r in rows]
    check("cryptocompare fetched all minute bars", len(rows) == total)
    check("cryptocompare rows sorted & deduped", times == sorted(set(times)))
    check("cryptocompare 60s spacing", all(times[i + 1] - times[i] == 60 for i in range(len(times) - 1)))
    check("cryptocompare maps volumeto -> volume", rows[0]["volume"] == 10.0)


def test_history_cryptocompare_into_backtest():
    from btc_backtest import Bar, run_backtest
    step = 60
    base_t = 1_700_000_000 - (1_700_000_000 % 300)
    total = 1500
    bars_src = [{
        "time": base_t + i * step, "open": 100000 + (i % 7), "high": 100000 + (i % 7) + 2,
        "low": 100000 + (i % 7) - 2, "close": 100000 + ((i + 1) % 7), "volumeto": 5.0,
    } for i in range(total)]

    def fake_getter(url, params=None, timeout=15.0):
        to_ts, limit = params["toTs"], params["limit"]
        upto = [b for b in bars_src if b["time"] <= to_ts]
        return {"Data": {"Data": upto[-limit:] if len(upto) > limit else upto}}

    rows = history_cryptocompare(days=(total * step) / 86_400, now_ts=base_t + total * step,
                                 getter=fake_getter, sleep_sec=0.0)
    bars = [Bar(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]
    res = run_backtest(bars, entry_threshold=0.15)
    check("backtest runs on cryptocompare data", res["rounds_5m"] > 0)


def test_fetch_history_unknown_provider():
    try:
        fetch_history("coingecko", days=1)  # not a valid *history* provider
        check("unknown history provider raises", False)
    except ValueError:
        check("unknown history provider raises", True)


def test_feed_registry_and_gate_capability():
    for name in ["binance", "coinbase", "kraken", "cryptocompare",
                 "coingecko", "dia", "livecoinwatch", "coinmarketcap"]:
        check(f"feed '{name}' registered", name in FEEDS)
    check("gate-capable list is a subset of registry",
          all(n in FEEDS for n in GATE_CAPABLE_FEEDS))
    check("spot-only feeds excluded from gate-capable",
          "coingecko" not in GATE_CAPABLE_FEEDS and "dia" not in GATE_CAPABLE_FEEDS)


def test_feed_parsers_stubbed(monkeypatch_target=pf):
    # Stub the shared JSON getter used by GET-based feeds.
    slot = 1_700_000_000 - (1_700_000_000 % 300)

    def fake_get_json(url, params=None, timeout=4.0):
        if "cryptocompare" in url and "histominute" in url:
            return {"Data": {"Data": [{"time": slot, "open": 100000.0, "high": 1, "low": 1, "close": 1}]}}
        if "cryptocompare" in url:  # price
            return {"USD": 100050.0}
        if "coingecko" in url:
            return {"bitcoin": {"usd": 100025.0}}
        if "diadata" in url:
            return {"Price": 99990.0}
        raise AssertionError(f"unexpected url {url}")

    orig = pf._get_json
    pf._get_json = fake_get_json
    try:
        cc = CryptoCompareFeed()
        check("cryptocompare spot parsed", cc.spot() == 100050.0)
        check("cryptocompare slot_open parsed", cc.slot_open(slot) == 100000.0)
        check("coingecko spot parsed", CoinGeckoFeed().spot() == 100025.0)
        check("coingecko has no slot_open", CoinGeckoFeed().slot_open(slot) is None)
        check("dia spot parsed", DiaFeed().spot() == 99990.0)
    finally:
        pf._get_json = orig


def test_gate_uses_cryptocompare_when_selected():
    """A CryptoCompare + Binance gate should evaluate using stubbed feeds."""
    slot = 1_700_000_000 - (1_700_000_000 % 300)

    def fake_get_json(url, params=None, timeout=4.0):
        # Both feeds: round opened at 100000, now +80.
        if "binance" in url and "klines" in url:
            return [[slot * 1000, "100000.0", "1", "1", "1", "1"]]
        if "binance" in url and "ticker" in url:
            return {"price": "100080.0"}
        if "cryptocompare" in url and "histominute" in url:
            return {"Data": {"Data": [{"time": slot, "open": 100000.0, "high": 1, "low": 1, "close": 1}]}}
        if "cryptocompare" in url:
            return {"USD": 100082.0}
        raise AssertionError(url)

    orig = pf._get_json
    pf._get_json = fake_get_json
    try:
        feeds = build_feeds(["binance", "cryptocompare"])
        r = evaluate_impulse(feeds, min_usd=70, max_usd=0, divergence_usd=60, min_feeds=2, now_ts=slot + 180)
        check("binance+cryptocompare gate confirms UP move", r["ok"] and r["direction"] == "UP")
    finally:
        pf._get_json = orig


def main():
    test_history_cryptocompare_parsing_and_pagination()
    test_history_cryptocompare_into_backtest()
    test_fetch_history_unknown_provider()
    test_feed_registry_and_gate_capability()
    test_feed_parsers_stubbed()
    test_gate_uses_cryptocompare_when_selected()
    print("\nAll multi-provider data-layer tests passed.")


if __name__ == "__main__":
    main()
