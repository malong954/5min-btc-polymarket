#!/usr/bin/env python3
"""
Offline tests for the multi-timeframe backtester. Stdlib only, no network.
Run: python3 scripts/test_btc_backtest.py
"""

import csv
import os
import tempfile

from btc_indicators import ema, macd, rsi
from btc_backtest import (
    Bar,
    MTFModel,
    DECISION_OFFSET,
    DEFAULT_WEIGHTS,
    run_ablation,
    run_backtest,
    run_walk_forward,
    run_weight_optimization,
    weight_opt_over_rows,
    rescore_rows,
    score_rounds,
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


def test_trade_log():
    bars = synth_bars(4000, autocorr=0.4, seed=9)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "trades.csv")
        res = run_backtest(bars, entry_threshold=0.5, trade_log=path)
        check("trade log recorded in result", res.get("trade_log", {}).get("path") == path)
        with open(path) as f:
            rows = list(csv.DictReader(f))
        check("trade log row per evaluated round", len(rows) == res["rounds_evaluated"])
        taken = [r for r in rows if r["taken"] == "1"]
        check("trade log taken count matches metrics", len(taken) == res["trades"])
        check("taken rows all have win/loss result", all(r["result"] in ("win", "loss") for r in taken))
        check("skipped rows have empty result", all(r["result"] == "" for r in rows if r["taken"] == "0"))
        wins = sum(1 for r in taken if r["result"] == "win")
        expected = round(res["directional_accuracy"] * res["trades"])
        check("trade log wins match reported accuracy", abs(wins - expected) <= 1)
        check("trade log has feature columns", "btc_move_usd" in rows[0] and "rsi_1m" in rows[0])


def test_walk_forward_structure():
    bars = synth_bars(12000, autocorr=0.5, seed=13)
    res = run_walk_forward(bars, train=400, test=200, min_trades=10)
    check("walk-forward produced folds", len(res["folds"]) >= 2)
    check("walk-forward has OOS trades", res["oos_trades"] > 0)
    check("walk-forward OOS accuracy valid", 0.0 <= res["oos_accuracy"] <= 1.0)
    grid_lo, grid_hi = 0.10, 0.95
    check("chosen thresholds within grid",
          all(grid_lo - 1e-9 <= f["chosen_threshold"] <= grid_hi + 1e-9 for f in res["folds"]))
    # Each fold's threshold was tuned on train only, then applied to the *next*
    # block, so this is a true out-of-sample number.
    check("walk-forward OOS edge is finite", res["oos_edge_vs_breakeven"] == res["oos_edge_vs_breakeven"])
    print(f"       walk-forward OOS: acc={res['oos_accuracy']:.3f} "
          f"trades={res['oos_trades']} thr_range={res['chosen_threshold_range']}")


def test_walk_forward_is_out_of_sample():
    """Sanity: on a pure random walk the OOS edge should not be a large positive
    number (no free lunch once the threshold is tuned out-of-sample)."""
    bars = synth_bars(12000, autocorr=0.0, seed=21)
    res = run_walk_forward(bars, train=400, test=200, min_trades=10)
    check("random-walk OOS edge is not implausibly large", res["oos_edge_vs_breakeven"] < 0.15)


def test_ablation():
    bars = synth_bars(12000, autocorr=0.5, seed=17)
    res = run_ablation(bars, train=400, test=200, min_trades=10)
    check("ablation covers every weighted feature",
          {a["removed"] for a in res["ablations"]} == set(DEFAULT_WEIGHTS))
    check("ablation has a baseline", res["baseline"]["oos_trades"] > 0)
    check("ablation ranked by EV contribution (descending)",
          all(res["ablations"][i]["ev_contribution"] >= res["ablations"][i + 1]["ev_contribution"]
              for i in range(len(res["ablations"]) - 1)))
    # On momentum data the current-round impulse is the dominant signal, so
    # removing it should be among the most damaging (top-half contributor).
    order = [a["removed"] for a in res["ablations"]]
    check("impulse_1m ranks in the more-valuable half",
          order.index("impulse_1m") < len(order) / 2)
    print(f"       ablation ranking (most->least valuable): {order}")


def test_rescore_identity():
    """Rescoring with the default weights must reproduce the original score and
    confidence exactly (proves the cheap re-weight path matches the model)."""
    bars = synth_bars(3000, autocorr=0.3, seed=8)
    rows = score_rounds(MTFModel(bars))
    re = rescore_rows(rows, dict(DEFAULT_WEIGHTS))
    check("rescore reproduces model scores under default weights",
          all(abs(a["score"] - b["score"]) < 1e-12 and abs(a["confidence"] - b["confidence"]) < 1e-12
              for a, b in zip(rows, re)))
    # Zeroing all weights => zero score => no direction.
    z = rescore_rows(rows, {k: 0.0 for k in DEFAULT_WEIGHTS})
    check("zero weights => zero score and no prediction",
          all(r["score"] == 0.0 and r["pred"] is None for r in z))


def test_weight_optimization_out_of_sample():
    bars = synth_bars(12000, autocorr=0.5, seed=19)
    res = run_weight_optimization(bars, train=400, test=200, min_trades=10)
    check("weight-opt produced folds", len(res["folds"]) >= 2)
    check("weight-opt baseline has OOS trades", res["baseline_oos"]["oos_trades"] > 0)
    check("weight-opt optimized has OOS trades", res["optimized_oos"]["oos_trades"] > 0)
    check("weight-opt reports a verdict", "overfit" in res["verdict"] or "helps" in res["verdict"])
    # Each optimized weight is either a grid candidate or the retained default
    # (a coordinate is only moved off its default when a candidate beats it).
    allowed = {0.0, 0.25, 0.5, 1.0, 1.5} | {round(v, 2) for v in DEFAULT_WEIGHTS.values()}
    check("optimized weights are grid candidates or retained defaults",
          all(v in allowed for f in res["folds"] for v in f["opt_weights"].values()))
    print(f"       weight-opt: baseline EV={res['baseline_oos']['oos_ev_per_trade']:+.4f} "
          f"optimized EV={res['optimized_oos']['oos_ev_per_trade']:+.4f} "
          f"-> {res['verdict']}")


def test_weight_opt_no_edge_on_shuffled_labels():
    """Leakage guard: if outcome labels are decoupled from the decision-time
    features, the optimizer must NOT be able to produce a large positive
    out-of-sample edge. If it does, the walk-forward machinery is leaking.
    Rotating the labels destroys both real and mechanical edge."""
    rows = score_rounds(MTFModel(synth_bars(12000, autocorr=0.5, seed=19)))
    n = len(rows)
    k = n // 3
    shuffled = [dict(r) for r in rows]
    for i in range(n):
        shuffled[i]["actual"] = rows[(i + k) % n]["actual"]
    real = weight_opt_over_rows(rows, train=400, test=200, min_trades=10)
    null = weight_opt_over_rows(shuffled, train=400, test=200, min_trades=10)
    real_edge = real["optimized_oos"]["oos_edge_vs_breakeven"]
    null_edge = null["optimized_oos"]["oos_edge_vs_breakeven"]
    print(f"       real optimized edge={real_edge:+.3f}  shuffled optimized edge={null_edge:+.3f}")
    check("real labels: optimizer finds positive OOS edge", real_edge > 0.02)
    check("shuffled labels: optimizer cannot manufacture OOS edge", null_edge < 0.02)
    check("shuffled edge is far below real edge (no leakage)", null_edge < real_edge - 0.10)


def main():
    test_indicators()
    test_resample_alignment()
    test_no_lookahead()
    test_end_to_end_random_walk()
    test_edge_detected_on_momentum()
    test_trade_log()
    test_walk_forward_structure()
    test_walk_forward_is_out_of_sample()
    test_ablation()
    test_rescore_identity()
    test_weight_optimization_out_of_sample()
    test_weight_opt_no_edge_on_shuffled_labels()
    print("\nAll backtest tests passed.")


if __name__ == "__main__":
    main()
