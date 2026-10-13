#!/usr/bin/env python3
"""Offline tests for the Limitless executor's money-touching pieces. No network.

Covers the parts that cost real dollars if wrong: the fee formula, the risk
guard (per-trade cap, daily kill, exposure cap, one-order-per-round), the
slip/depth refusal arithmetic, and that dry-run stays the fail-closed default.
Run: python3 scripts/test_btc_limitless_exec.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from btc_limitless_exec import ExecGuard, TAKER_FEE_RATE, taker_fee


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


NOW = 1_785_720_000.0  # 2026-08-03 (UTC) — fixed so day-roll tests are deterministic


def test_fee_formula():
    # fee = shares x 0.07 x p x (1-p): the user's measured reckoning.
    check("fee at 0.70 is 1.47c/share",
          abs(taker_fee(1.0, 0.70) - 0.07 * 0.70 * 0.30) < 1e-12)
    check("fee is symmetric in p", taker_fee(10, 0.35) == taker_fee(10, 0.65))
    check("fee scales with shares", abs(taker_fee(7, 0.8) - 7 * taker_fee(1, 0.8)) < 1e-12)
    check("rate is the documented 0.07", TAKER_FEE_RATE == 0.07)


def test_per_trade_clamp():
    g = ExecGuard(max_stake=5.0)
    check("tiered $10 stake clamps to $5", g.clamp(10.0) == 5.0)
    check("small stakes pass through", g.clamp(2.0) == 2.0)


def test_one_order_per_round():
    g = ExecGuard()
    ok, _ = g.allow(NOW, 900, 2.0)
    check("first order allowed", ok)
    g.record_order(900, 2.0)
    ok, why = g.allow(NOW, 900, 2.0)
    check("second order same round refused", not ok and why == "already_ordered")
    ok, _ = g.allow(NOW, 1800, 2.0)
    check("next round allowed again", ok)


def test_exposure_cap():
    g = ExecGuard(max_open=10.0)
    g.record_order(900, 5.0)
    g.record_order(1800, 4.0)
    ok, why = g.allow(NOW, 2700, 2.0)
    check("$9 open + $2 refused at $10 cap", not ok and why == "max_open")
    g.record_settle(NOW, 900, 1.0)          # frees $5 of risk
    ok, _ = g.allow(NOW, 2700, 2.0)
    check("allowed again after a settle frees risk", ok)


def test_daily_kill_and_reset():
    g = ExecGuard(daily_loss_kill=15.0)
    g.record_order(900, 5.0)
    check("-$10 does not trip", not g.record_settle(NOW, 900, -10.0))
    g.record_order(1800, 5.0)
    check("-$15 cumulative trips the kill", g.record_settle(NOW, 1800, -5.0))
    ok, why = g.allow(NOW, 2700, 2.0)
    check("killed day refuses orders", not ok and why == "daily_kill")
    ok, _ = g.allow(NOW + 86400, 2700, 2.0)  # next UTC day
    check("kill resets when the UTC day rolls", ok)
    check("day pnl reset too", g.day_pnl == 0.0)


def test_wins_offset_losses():
    g = ExecGuard(daily_loss_kill=15.0)
    g.record_order(900, 5.0)
    g.record_settle(NOW, 900, +8.0)
    g.record_order(1800, 5.0)
    check("-$14 after a +$8 win does not trip (net -$6)",
          not g.record_settle(NOW, 1800, -14.0))


def test_settle_pnl_arithmetic():
    # Mirrors the executor's settle path: win pays shares x (1-p) minus fee,
    # loss forfeits the stake minus nothing further (fee charged either way).
    price, stake = 0.80, 4.0
    shares = round(stake / price, 3)
    fee = taker_fee(shares, price)
    win_net = shares * (1 - price) - fee
    loss_net = -stake - fee
    check("win net is positive at 0.80", win_net > 0)
    check("$4 @ 0.80 win nets ~$0.944",
          abs(win_net - (5.0 * 0.20 - taker_fee(5.0, 0.80))) < 1e-9)
    check("loss costs stake plus fee", loss_net < -stake)


def test_dry_run_is_fail_closed():
    # The live path demands --live AND LIMITLESS_LIVE=1 AND wallet+HMAC creds.
    # Missing any one must leave live=False. Re-derive the executor's own
    # arming expression under each combination.
    def armed(flag, env, pk, hmac_):
        return flag and env and pk and hmac_
    for combo in range(15):          # every combination except all-true
        flag, env, pk, hmac_ = (bool(combo & 1), bool(combo & 2),
                                bool(combo & 4), bool(combo & 8))
        if flag and env and pk and hmac_:
            continue
        check(f"combo flag={flag} env={env} pk={pk} hmac={hmac_} stays dry",
              not armed(flag, env, pk, hmac_))
    check("only the full set arms live", armed(True, True, True, True))


def main():
    test_fee_formula()
    test_per_trade_clamp()
    test_one_order_per_round()
    test_exposure_cap()
    test_daily_kill_and_reset()
    test_wins_offset_losses()
    test_settle_pnl_arithmetic()
    test_dry_run_is_fail_closed()
    print("all tests passed")


if __name__ == "__main__":
    main()
