#!/usr/bin/env python3
"""
Offline tests for position sizing. Run: python3 scripts/test_btc_sizing.py
"""

from btc_sizing import kelly_fraction, Calibrator, stake_for, simulate_account
from btc_backtest import synth_bars, oos_trades_for_sizing


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def test_kelly_signs():
    # The load-bearing numbers the workflow independently verifies.
    f_low = kelly_fraction(0.818, 0.85)
    f_hi = kelly_fraction(0.88, 0.85)
    print(f"       kelly(0.818,0.85)={f_low:.4f}  kelly(0.88,0.85)={f_hi:.4f}")
    check("kelly negative at 81.8% (below breakeven)", f_low < 0)
    check("kelly positive at 88% (above breakeven)", f_hi > 0)
    check("kelly ~0.20 at 88%", abs(f_hi - 0.20) < 0.02)
    check("breakeven: kelly == 0 exactly at p == entry", abs(kelly_fraction(0.85, 0.85)) < 1e-9)


def test_flat_stake_cannot_change_ev_sign():
    """EV = s*(p-c)/c. For p<c EV<0 for every positive s; scaling s never flips it."""
    p, c = 0.818, 0.85
    def ev(s):
        win = s * (1 - c) / c
        return p * win - (1 - p) * s
    check("EV negative at small flat stake", ev(1) < 0)
    check("EV negative at large flat stake", ev(1000) < 0)
    check("EV scales linearly with stake (sign fixed)", abs(ev(100) - 100 * ev(1)) < 1e-6)


def test_kelly_stakes_zero_below_breakeven():
    s = stake_for("kelly", bankroll=100, base_stake=10, confidence=0.9,
                  entry_price=0.85, p_est=0.80)
    check("kelly stakes $0 when p_est <= entry", s == 0.0)
    s2 = stake_for("kelly", bankroll=100, base_stake=10, confidence=0.9,
                   entry_price=0.85, p_est=0.92, kelly_mult=0.5, kelly_cap=0.25)
    check("kelly stakes >0 when p_est > entry", s2 > 0)
    check("kelly respects cap", s2 <= 100 * 0.25 + 1e-9)


def test_kelly_safety_margin():
    # p_est just above breakeven (0.86 vs c=0.85) is inside the 0.03 margin -> $0.
    s_barely = stake_for("kelly", bankroll=100, base_stake=10, confidence=0.9,
                         entry_price=0.85, p_est=0.86, margin=0.03)
    check("kelly refuses a barely-above-breakeven p (noise cushion)", s_barely == 0.0)
    # Clear of the margin (0.90 >= 0.85+0.03) -> bets.
    s_clear = stake_for("kelly", bankroll=100, base_stake=10, confidence=0.9,
                        entry_price=0.85, p_est=0.90, margin=0.03)
    check("kelly bets when p clears breakeven + margin", s_clear > 0)
    # margin=0 recovers the plain p>c behavior.
    s_nomargin = stake_for("kelly", bankroll=100, base_stake=10, confidence=0.9,
                           entry_price=0.85, p_est=0.86, margin=0.0)
    check("margin=0 allows a bet just above breakeven", s_nomargin > 0)


def test_confidence_and_flat_modes():
    check("flat mode returns base stake", stake_for("flat", bankroll=100, base_stake=10,
          confidence=0.3, entry_price=0.85) == 10)
    half = stake_for("confidence", bankroll=100, base_stake=10, confidence=0.5, entry_price=0.85)
    check("confidence mode scales by confidence", abs(half - 5.0) < 1e-9)


def test_percent_mode_scales_with_balance():
    # 15% of balance: grows as the balance grows.
    s_100 = stake_for("percent", bankroll=100, base_stake=10, confidence=0.5, entry_price=0.85, pct=0.15)
    s_200 = stake_for("percent", bankroll=200, base_stake=10, confidence=0.5, entry_price=0.85, pct=0.15)
    check("percent mode stakes 15% of balance at $100", abs(s_100 - 15.0) < 1e-9)
    check("percent mode auto-grows to 15% of $200", abs(s_200 - 30.0) < 1e-9)
    check("percent stake grows with balance", s_200 > s_100)


def test_calibrator_monotone_recovery():
    # Build rows where higher confidence really does win more often.
    rows = []
    for i in range(200):
        conf = i / 200.0
        win = (i % 100) < (conf * 100)  # higher conf -> more wins
        rows.append({"confidence": conf, "pred": "UP", "actual": "UP" if win else "DOWN"})
    cal = Calibrator.fit(rows, n_buckets=5)
    lo = cal.predict(0.1)
    hi = cal.predict(0.9)
    check("calibrator: high-confidence bucket wins more than low", hi > lo)


def test_simulate_losing_vs_winning_edge():
    # Losing edge (p<c): every mode loses; kelly loses least (stakes ~0).
    losing = [{"confidence": 0.6, "win": (i % 100) < 81, "p_est": 0.81} for i in range(400)]
    res = {m: simulate_account(losing, m, bankroll=100, base_stake=10, entry_price=0.85)
           for m in ["flat", "confidence", "kelly"]}
    check("losing edge: flat loses money", res["flat"]["pnl"] < 0)
    check("losing edge: kelly stakes nothing (p<=c) -> ~flat balance", res["kelly"]["n_staked"] == 0)
    check("losing edge: kelly preserves capital best", res["kelly"]["final_balance"] >= res["flat"]["final_balance"])

    # Winning edge (p>c): kelly compounds and beats flat.
    winning = [{"confidence": 0.9, "win": (i % 100) < 90, "p_est": 0.90} for i in range(400)]
    resw = {m: simulate_account(winning, m, bankroll=100, base_stake=10, entry_price=0.85)
            for m in ["flat", "kelly"]}
    check("winning edge: flat makes money", resw["flat"]["pnl"] > 0)
    check("winning edge: kelly makes money", resw["kelly"]["pnl"] > 0)


def test_oos_sizing_pipeline_runs():
    bars = synth_bars(9000, autocorr=0.5, seed=51)
    trades = oos_trades_for_sizing(bars, entry_price=0.85, train=400, test=200, min_trades=10)
    check("oos sizing pipeline produced trades", len(trades) > 20)
    check("each trade has confidence/win/p_est",
          all("confidence" in t and "win" in t and "p_est" in t for t in trades))
    res = simulate_account(trades, "kelly", bankroll=100, base_stake=10, entry_price=0.85)
    check("kelly simulation returns a balance", res["final_balance"] > 0 or res["ruined"])


def main():
    test_kelly_signs()
    test_flat_stake_cannot_change_ev_sign()
    test_kelly_stakes_zero_below_breakeven()
    test_kelly_safety_margin()
    test_confidence_and_flat_modes()
    test_percent_mode_scales_with_balance()
    test_calibrator_monotone_recovery()
    test_simulate_losing_vs_winning_edge()
    test_oos_sizing_pipeline_runs()
    print("\nAll sizing tests passed.")


if __name__ == "__main__":
    main()
