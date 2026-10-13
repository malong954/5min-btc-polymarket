#!/usr/bin/env python3
"""
Offline tests for the Polymarket price fetcher + its wiring into the paper
trader. No network: gamma + CLOB responses are stubbed. Run:
    python3 scripts/test_btc_polymarket.py
"""

from btc_polymarket import (
    current_slug, parse_json_field, clob_best_ask, current_prices, bucket_5m,
)
from btc_backtest import synth_bars, Bar
from btc_live_paper import LivePaperEngine, bucket_5m as lp_bucket


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def test_slug_and_parse():
    slug = current_slug(1_700_000_123)
    check("slug uses the 5m bucket", slug == f"btc-updown-5m-{bucket_5m(1_700_000_123)}")
    ts = 1_700_000_123
    slug15 = current_slug(ts, "btc", "15m")
    check("15m slug uses the 900s bucket",
          slug15 == f"btc-updown-15m-{ts - (ts % 900)}")
    check("parse_json_field decodes a JSON string list", parse_json_field('["a","b"]') == ["a", "b"])
    check("parse_json_field passes through a real list", parse_json_field(["x"]) == ["x"])
    check("parse_json_field returns None on garbage", parse_json_field("{not json") is None)


def make_getter(up_ask, dn_ask, up_token="TUP", dn_token="TDN", outcomes=None):
    slug = current_slug(1_700_000_000)
    event = {"markets": [{
        "slug": slug,
        "outcomes": outcomes if outcomes is not None else '["Down","Up"]',
        "clobTokenIds": f'["{dn_token}","{up_token}"]',  # index0=Down, index1=Up
        "endDate": "2026-07-05T00:05:00Z",
    }]}

    def getter(url, params=None, timeout=8.0):
        if "gamma" in url:
            return [event]
        if "book" in url:
            tok = params["token_id"]
            ask = up_ask if tok == up_token else dn_ask
            # asks intentionally unsorted to prove we take the minimum.
            return {"asks": [{"price": f"{ask + 0.02:.2f}", "size": "5"},
                             {"price": f"{ask:.2f}", "size": "10"}],
                    "bids": [{"price": f"{ask - 0.03:.2f}", "size": "7"}]}
        raise AssertionError(url)

    return getter


def test_best_ask_takes_minimum():
    getter = make_getter(0.86, 0.14)
    check("best ask is the lowest ask price", clob_best_ask("TUP", getter) == 0.86)


def test_current_prices_maps_up_down():
    getter = make_getter(0.86, 0.14)
    p = current_prices(1_700_000_000, getter)
    check("current_prices resolved a market", p is not None)
    check("UP ask mapped from the 'Up' outcome token", abs(p["UP"] - 0.86) < 1e-9)
    check("DOWN ask mapped from the 'Down' outcome token", abs(p["DOWN"] - 0.14) < 1e-9)
    check("tokens are surfaced", p["up_token"] == "TUP" and p["down_token"] == "TDN")


def test_current_prices_none_when_no_event():
    def empty_getter(url, params=None, timeout=8.0):
        return [] if "gamma" in url else {"asks": []}
    check("no event -> None", current_prices(1_700_000_000, empty_getter) is None)


def _visible(bars, now):
    return [b for b in bars if b.ts + 60 <= now]


def test_paper_uses_real_entry_price_per_trade():
    """With a real ask fed in, the settled trade's economics must use THAT price,
    not the fixed 0.85 default."""
    bars = synth_bars(1200, autocorr=0.6, seed=71)
    eng = LivePaperEngine(entry_threshold=0.2, entry_price=0.85, require_market_price=True)
    # Drive until a trade settles, feeding a fixed real ask of 0.92 for both sides.
    start = bars[0].ts + 40 * 60
    end = bars[-1].ts + 60
    settle = None
    now = start
    while now <= end and settle is None:
        evs = eng.step(now, _visible(bars, now)[-360:], spot=None,
                       entry_prices={"UP": 0.92, "DOWN": 0.92})
        for e in evs:
            if e["type"] == "settle":
                settle = e
                break
        now += 60
    check("a trade settled under real pricing", settle is not None)
    check("settle records the real entry price (0.92, not 0.85)", abs(settle["entry_price"] - 0.92) < 1e-9)
    # Win pnl at 0.92 = (1-0.92) = 0.08 unit; loss = -0.92. Verify the unit pnl matches 0.92.
    if settle["result"] == "win":
        check("win unit pnl uses real price", abs(settle["pnl"] - (1 - 0.92)) < 1e-6)
    else:
        check("loss unit pnl uses real price", abs(settle["pnl"] - (-0.92)) < 1e-6)


def test_paper_skips_when_market_price_missing():
    bars = synth_bars(1200, autocorr=0.6, seed=72)
    eng = LivePaperEngine(entry_threshold=0.2, require_market_price=True)
    # Feed no usable price -> any would-be entry must skip with 'no_market_price'.
    start = bars[0].ts + 40 * 60
    end = bars[-1].ts + 60
    saw_skip = False
    saw_entry = False
    now = start
    while now <= end:
        for e in eng.step(now, _visible(bars, now)[-360:], entry_prices={"UP": None, "DOWN": None}):
            if e["type"] == "skip" and e.get("reason") == "no_market_price":
                saw_skip = True
            if e["type"] == "entry":
                saw_entry = True
        now += 60
    check("unpriceable rounds are skipped, not entered", saw_skip and not saw_entry)


def main():
    test_slug_and_parse()
    test_best_ask_takes_minimum()
    test_current_prices_maps_up_down()
    test_current_prices_none_when_no_event()
    test_paper_uses_real_entry_price_per_trade()
    test_paper_skips_when_market_price_missing()
    print("\nAll Polymarket price tests passed.")


if __name__ == "__main__":
    main()
