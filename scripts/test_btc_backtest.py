#!/usr/bin/env python3
"""
Offline tests for the multi-timeframe backtester. Stdlib only, no network.
Run: python3 scripts/test_btc_backtest.py
"""

from btc_indicators import ema, macd, rsi
from btc_backtest import (
    Bar,
    MTFModel,
    DECISION_OFFSET,
    run_backtest,
    synth_bars,
)


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def test_indicators():
    rising = [float(i) for i in range(1, 60)]
    r = rsi(rising, 14)
    check("RSI of monotonically rising series > 90", r[-1] is not None and r[-1] > 90)

    falling = [float(i) for i in range(60, 1, -1)]
    r = rsi(falling, 14)
    check("RSI of monotonically falling series < 10", r[-1] is not None and r[-1] < 10)

    e = ema(rising, 10)
    check("EMA tracks below the latest value on an uptrend", e[-1] is not None and e[-1] < rising[-1])

    macd_line, _, _ = macd(rising, 12, 26, 9)
    check("MACD line positive on a clean uptrend", macd_line[-1] is not None and macd_line[-1] > 0)

    # A convex (accelerating) uptrend should show a positive histogram, since the
    # histogram measures momentum acceleration (a linear ramp gives ~0).
    accel = [float(i * i) for i in range(1, 60)]
    _, _, hist = macd(accel, 12, 26, 9)
    check("MACD histogram positive on an accelerating uptrend", hist[-1] is not None and hist[-1] > 0)


def test_no_lookahead():
    """Features at the decision instant must not change when data AFTER the
    decision instant changes. Build two datasets identical up to t_dec of a
    round, differing wildly after, and confirm the signal is identical."""
    base = synth_bars(2000, autocorr=0.0, seed=7)
    # Pick a round well past warm-up.
    rs = base[600].ts - (base[600].ts % 300)
    t_dec = rs + DECISION_OFFSET

    variant = [Bar(b.ts, b.o, b.h, b.l, b.c, b.v) for b in base]
    # Corrupt every bar strictly AFTER the decision instant of this round.
    for b in variant:
        if b.ts >= t_dec:  # bar opening at/after decision instant is in the future
            b.c += 5000.0
            b.h += 5000.0
            b.o += 5000.0
            b.l += 5000.0

    s_base = MTFModel(base).evaluate(rs)
    s_var = MTFModel(variant).evaluate(rs)
    check("decision-time signal exists", s_base is not None and s_var is not None)
    same_score = abs(s_base.score - s_var.score) < 1e-9
    same_dir = s_base.direction == s_var.direction
    check("no lookahead: score identical despite corrupted future bars", same_score)
    check("no lookahead: direction identical", same_dir)


def test_end_to_end_random_walk():
    bars = synth_bars(6000, autocorr=0.0, seed=11)
    res = run_backtest(bars, entry_threshold=0.15)
    check("random walk: trades were taken", res["trades"] > 20)
    check("random walk: accuracy is a valid probability", 0.0 <= res["directional_accuracy"] <= 1.0)
    check("random walk: feature correlation table populated", len(res["feature_correlation"]) >= 3)
    check("random walk: confidence buckets present", len(res["accuracy_by_confidence"]) == 3)


def test_edge_detected_on_momentum():
    """With injected momentum (autocorr), the ensemble should beat a coin flip
    by a clear margin, and its score should positively correlate with outcome."""
    bars = synth_bars(8000, autocorr=0.6, seed=5)
    res = run_backtest(bars, entry_threshold=0.15)
    acc = res["directional_accuracy"]
    corr = res["feature_correlation"].get("score", 0.0)
    print(f"       momentum-data accuracy={acc:.3f} score_corr={corr:+.3f} trades={res['trades']}")
    check("momentum data: accuracy clearly beats 50%", acc > 0.55)
    check("momentum data: ensemble score positively correlates with outcome", corr > 0.1)
    check("momentum data: high-confidence bucket >= low-confidence bucket",
          res["accuracy_by_confidence"][-1]["accuracy"] >= res["accuracy_by_confidence"][0]["accuracy"])


def test_resample_alignment():
    bars = synth_bars(300, seed=3)
    m = MTFModel(bars)
    check("5m bars aligned to 300s boundaries", all(b.ts % 300 == 0 for b in m.bars_5m))
    check("15m bars aligned to 900s boundaries", all(b.ts % 900 == 0 for b in m.bars_15m))
    check("15m bar count roughly 1/3 of 5m", abs(len(m.bars_15m) - len(m.bars_5m) / 3) <= 2)


def main():
    test_indicators()
    test_resample_alignment()
    test_no_lookahead()
    test_end_to_end_random_walk()
    test_edge_detected_on_momentum()
    print("\nAll backtest tests passed.")


if __name__ == "__main__":
    main()
