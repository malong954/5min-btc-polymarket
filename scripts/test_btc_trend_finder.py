#!/usr/bin/env python3
"""Offline tests for btc_trend_finder.py. Stdlib only, no network."""

from btc_trend_finder import (
    Config,
    Dataset,
    Signal,
    arm_book_momentum,
    build_context,
    make_trade,
    partition_days,
    run_finder,
)


def check(description, condition):
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")
    if not condition:
        raise SystemExit(1)


def sample(round_start, sec_left, move=20.0, up=0.60, down=0.42, ts=None):
    ts = ts if ts is not None else round_start + 300 - sec_left
    return {
        "type": "sample", "round": round_start, "sec_left": sec_left, "ts": ts,
        "move": move, "spot": 100_000.0,
        "up_ask": up, "dn_ask": down, "up_sz": 100.0, "dn_sz": 100.0,
        "up_bid": up - 0.02, "dn_bid": down - 0.02,
        "up_bid_sz": 100.0, "dn_bid_sz": 100.0,
        "ind_dir": "UP" if move > 0 else "DOWN", "ind_conf": 0.70,
        "vel_5s": move / 4, "vel_15s": move / 3, "vel_30s": move / 2, "vel_60s": move,
    }


def test_delayed_fill_and_no_future_signal():
    round_start = 1_800_000_000
    samples = [
        sample(round_start, 250, up=0.52, down=0.50),
        sample(round_start, 210, up=0.60, down=0.42),
        sample(round_start, 204, up=0.70, down=0.32),
        sample(round_start, 198, move=-50, up=0.20, down=0.82),
    ]
    ctx = build_context(round_start, samples, Config(slippage=0.0))
    check("decision freezes at first sample crossing target", ctx.decision["sec_left"] == 210)
    check("fill uses a later quote after configured latency", ctx.fill["sec_left"] == 204)
    check("future sample excluded from signal history", all(s["sec_left"] > 210 for s in ctx.history))


def test_book_momentum_uses_prior_only():
    round_start = 1_800_000_000
    samples = [
        sample(round_start, 250, up=0.45, down=0.57),
        sample(round_start, 210, up=0.62, down=0.40),
        sample(round_start, 204, up=0.65, down=0.37),
    ]
    cfg = Config(slippage=0.0)
    ctx = build_context(round_start, samples, cfg)
    signal = arm_book_momentum(ctx, cfg)
    check("book momentum sees rising UP probability", signal is not None and signal.side == "UP")
    changed_future = list(samples)
    changed_future[2] = sample(round_start, 204, up=0.05, down=0.97)
    signal2 = arm_book_momentum(build_context(round_start, changed_future, cfg), cfg)
    check("book momentum ignores the future fill snapshot", signal2 == signal)


def test_fee_slippage_and_depth_math():
    round_start = 1_800_000_000
    cfg = Config(latency_sec=0, slippage=0.01, fee_rate=0.07,
                 stake_usd=100.0, min_size=5.0)
    s = sample(round_start, 210, up=0.60, down=0.42)
    s["up_sz"] = 10.0
    ctx = build_context(round_start, [s], cfg)
    trade = make_trade("test", Signal("UP", 1.0), ctx, "UP", "train", cfg)
    expected_fee = 10.0 * 0.07 * 0.61 * 0.39
    expected_pnl = 10.0 - 10.0 * 0.61 - expected_fee
    check("one tick slippage is added to ask", abs(trade["price"] - 0.61) < 1e-12)
    check("shares are capped at recorded best-ask size", trade["shares"] == 10.0)
    check("crypto taker fee uses C*rate*p*(1-p)", abs(trade["fee"] - expected_fee) < 1e-12)
    check("net PnL deducts cost and fee", abs(trade["pnl"] - expected_pnl) < 1e-12)


def test_whole_day_chronological_splits():
    rounds = [1_800_000_000 + day * 86400 for day in range(10)]
    by_day, parts = partition_days(rounds, 0.6, 0.2)
    check("split keeps 6/2/2 whole UTC days",
          [len(parts[k]) for k in ("train", "validation", "test")] == [6, 2, 2])
    check("partitions are chronological", max(parts["train"]) < min(parts["validation"])
          < min(parts["test"]))
    check("every round maps by UTC day", all((r // 86400) in by_day for r in rounds))


def test_end_to_end_official_holdout():
    data = Dataset()
    base = 1_800_000_000 - (1_800_000_000 % 86400)
    # Nine days, eight rounds/day. Spot/velocity/book all point UP. Official
    # labels win 7/8, enough to beat a ~0.61 delayed fill after fee and slippage.
    for day in range(9):
        for i in range(8):
            round_start = base + day * 86400 + i * 300
            data.samples[round_start] = [
                sample(round_start, 250, up=0.54, down=0.48),
                sample(round_start, 210, up=0.58, down=0.44),
                sample(round_start, 204, up=0.60, down=0.42),
            ]
            data.outcomes[round_start] = "DOWN" if i == 7 else "UP"
    cfg = Config(slippage=0.0, min_partition_trades=8, min_move_frac=0.0001)
    report, trades = run_finder(data, cfg)
    spot = report["summaries"]["spot_lead"]
    check("finder emits one spot trade per official round", spot["all"]["trades"] == 72)
    check("officially graded edge is positive in all splits", spot["stable_positive"])
    check("selection is made from train and validation", report["selected_on_train_validation"] is not None)
    check("trade records retain partition and delayed fill", all(t["partition"] in
          ("train", "validation", "test") and t["fill_delay_sec"] >= 6 for t in trades))


def main():
    test_delayed_fill_and_no_future_signal()
    test_book_momentum_uses_prior_only()
    test_fee_slippage_and_depth_math()
    test_whole_day_chronological_splits()
    test_end_to_end_official_holdout()
    print("\nAll trend-finder tests passed.")


if __name__ == "__main__":
    main()
