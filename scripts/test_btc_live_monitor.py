#!/usr/bin/env python3
"""
Offline tests for the live monitor dashboard. Run:
    python3 scripts/test_btc_live_monitor.py
"""

import json
import os
import tempfile

from btc_live_monitor import (
    GREEN, RED,
    Painter, new_state, fold_event, render, read_new_events, _activity_row,
)


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def settle(ts, side, actual, pnl, cum):
    return {"ts": ts, "type": "settle", "side": side, "actual": actual,
            "result": "win" if side == actual else "loss", "pnl": pnl, "cum_pnl": cum}


def test_fold_aggregation():
    st = new_state()
    fold_event(st, {"type": "heartbeat", "price": 60000.0, "seconds_left": 100})
    fold_event(st, {"type": "prediction", "direction": "UP", "confidence": 0.7, "btc_move_usd": 30})
    fold_event(st, settle(1, "UP", "UP", 0.15, 0.15))
    fold_event(st, settle(2, "DOWN", "UP", -0.85, -0.70))
    fold_event(st, settle(3, "DOWN", "DOWN", 0.15, -0.55))
    check("trades counted", st["trades"] == 3)
    check("wins counted", st["wins"] == 2)
    check("pnl summed", abs(st["pnl"] - (0.15 - 0.85 + 0.15)) < 1e-9)
    check("last heartbeat captured", st["last_hb"]["price"] == 60000.0)
    check("last prediction captured", st["last_pred"]["direction"] == "UP")
    check("peak pnl tracked", abs(st["peak_pnl"] - 0.15) < 1e-9)
    check("max drawdown tracked (negative)", st["max_drawdown"] < 0)


def test_render_colors_wins_and_losses():
    st = new_state()
    fold_event(st, {"type": "heartbeat", "price": 61000.0, "seconds_left": 90})
    fold_event(st, settle(1, "UP", "UP", 0.15, 0.15))     # win
    fold_event(st, settle(2, "UP", "DOWN", -0.85, -0.70))  # loss
    out = render(st, 0.85, Painter(color=True))
    check("render shows WIN", "WIN" in out)
    check("render shows LOSS", "LOSS" in out)
    check("render uses green somewhere (gains)", GREEN in out)
    check("render uses red somewhere (losses)", RED in out)
    check("render shows winrate label", "winrate" in out)


def test_dollar_account_and_backward_compat():
    # Old-style settle lines (no pnl_usd/balance) must still produce a $ account
    # via derivation from the unit pnl, using the monitor's stake/entry config.
    st = new_state(bankroll=100.0, stake_usd=10.0, entry_price=0.85)
    fold_event(st, settle(1, "UP", "UP", 0.15, 0.15))     # win: +$1.7647
    fold_event(st, settle(2, "UP", "DOWN", -0.85, -0.70))  # loss: -$10
    win_usd = 10.0 * (1 - 0.85) / 0.85
    check("derived $ balance = 100 + win - stake",
          abs(st["balance"] - (100.0 + win_usd - 10.0)) < 1e-6)
    out = render(st, 0.85, Painter(color=True))
    check("render shows account line", "account" in out)
    check("render shows a dollar figure", "$" in out)

    # New-style lines with explicit pnl_usd/balance are used as-is.
    st2 = new_state(bankroll=100.0, stake_usd=10.0, entry_price=0.85)
    ev = {"ts": 1, "type": "settle", "side": "UP", "actual": "UP", "result": "win",
          "pnl": 0.15, "cum_pnl": 0.15, "pnl_usd": 2.50, "balance": 102.50}
    fold_event(st2, ev)
    check("explicit pnl_usd is used verbatim", abs(st2["pnl_usd"] - 2.50) < 1e-9)
    check("explicit balance reflected", abs(st2["balance"] - 102.50) < 1e-9)


def test_render_no_color_is_plain():
    st = new_state()
    fold_event(st, settle(1, "UP", "UP", 0.15, 0.15))
    out = render(st, 0.85, Painter(color=False))
    check("no-color render has no ANSI escapes", "\033[" not in out)
    check("no-color render still has content", "account" in out and "trades" in out)


def test_breakeven_verdict_flips_color():
    # 100% winrate -> above breakeven (green verdict present)
    st = new_state()
    for i in range(3):
        fold_event(st, settle(i, "UP", "UP", 0.15, 0.15 * (i + 1)))
    above = render(st, 0.85, Painter(color=True))
    check("all wins -> ABOVE breakeven", "ABOVE breakeven" in above)
    # mostly losses -> below breakeven
    st2 = new_state()
    fold_event(st2, settle(1, "UP", "UP", 0.15, 0.15))
    for i in range(5):
        fold_event(st2, settle(i + 2, "UP", "DOWN", -0.85, 0.0))
    below = render(st2, 0.85, Painter(color=True))
    check("mostly losses -> below breakeven", "below breakeven" in below)


def test_incremental_read_and_truncation():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "live.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "settle", "side": "UP", "actual": "UP",
                                "result": "win", "pnl": 0.15, "cum_pnl": 0.15, "ts": 1}) + "\n")
        evs, off1 = read_new_events(path, 0)
        check("first read returns the line", len(evs) == 1)
        # No new data -> empty, offset unchanged.
        evs2, off2 = read_new_events(path, off1)
        check("incremental read returns nothing new", evs2 == [] and off2 == off1)
        # Append another line -> only the new one is returned.
        with open(path, "a") as f:
            f.write(json.dumps({"type": "heartbeat", "price": 1.0}) + "\n")
        evs3, off3 = read_new_events(path, off2)
        check("incremental read returns only new line", len(evs3) == 1 and evs3[0]["type"] == "heartbeat")
        # Truncate -> offset resets and re-reads from start.
        with open(path, "w") as f:
            f.write(json.dumps({"type": "heartbeat", "price": 2.0}) + "\n")
        evs4, off4 = read_new_events(path, off3)
        check("truncation handled (re-reads from start)", len(evs4) == 1 and off4 > 0)


def test_skip_rows_graded_by_shadow_settle():
    # A skipped round's later shadow_settle must annotate the SKIP row in the
    # feed with how the contract actually resolved (and if the call would win).
    st = new_state()
    fold_event(st, {"ts": 1, "type": "skip", "asset": "ETH", "round": 900,
                    "reason": "no_lead_setup", "side": "DOWN", "confidence": 0.25})
    check("skip row initially ungraded", "actual" not in st["activity"][-1])
    fold_event(st, {"ts": 2, "type": "shadow_settle", "asset": "ETH", "round": 900,
                    "side": "DOWN", "actual": "UP", "result": "loss"})
    check("shadow settle grades the skip row", st["activity"][-1].get("actual") == "UP")
    out = _activity_row(st["activity"][-1], Painter(color=False))
    check("skip row shows the resolution", "-> UP" in out and "✗" in out)
    win = new_state()
    fold_event(win, {"ts": 1, "type": "skip", "asset": "BTC", "round": 900,
                     "reason": "insolvent", "side": "UP", "confidence": 0.5})
    fold_event(win, {"ts": 2, "type": "shadow_settle", "asset": "BTC", "round": 900,
                     "side": "UP", "actual": "UP", "result": "win"})
    out_w = _activity_row(win["activity"][-1], Painter(color=False))
    check("correct shadow call marked with a check", "-> UP" in out_w and "✓" in out_w)
    # BTC and ETH share round buckets: another market's resolution must not
    # grade this market's skip.
    st2 = new_state()
    fold_event(st2, {"ts": 1, "type": "skip", "asset": "BTC", "round": 900,
                     "reason": "no_lead_setup", "side": "UP", "confidence": 0.5})
    fold_event(st2, {"ts": 2, "type": "shadow_settle", "asset": "ETH", "round": 900,
                     "side": "DOWN", "actual": "UP", "result": "loss"})
    check("other market's shadow does not grade it", "actual" not in st2["activity"][-1])


def test_15m_stream_is_a_separate_market():
    """A BTC 15m trader merged into the panel must not pollute the BTC 5m
    stream: its rows tag as BTC15, it gets its own per-market accounting, the
    header lists both windows, and the rules line states the hold-to-resolution
    + ask-cap rules from the trader's config event."""
    st = new_state()
    fold_event(st, {"type": "config", "asset": "BTC", "window": "5m", "bankroll": 100.0,
                    "entry_rule": "lead", "lead_min_conf": 0.40, "lead_max_price": 0.72,
                    "lead_hi": 270.0, "lead_lo": 180.0})
    fold_event(st, {"type": "config", "asset": "BTC", "window": "15m", "bankroll": 100.0,
                    "lead_hi": 810.0, "lead_lo": 540.0})
    fold_event(st, {"type": "heartbeat", "asset": "BTC", "window": "5m",
                    "price": 60000.0, "seconds_left": 100, "round": 300})
    fold_event(st, {"type": "heartbeat", "asset": "BTC", "window": "15m",
                    "price": 60000.0, "seconds_left": 700, "round": 900})
    fold_event(st, dict(settle(1, "UP", "UP", 0.15, 0.15), asset="BTC", window="5m",
                        pnl_usd=1.5, round=300))
    fold_event(st, dict(settle(2, "UP", "DOWN", -0.85, -0.70), asset="BTC", window="15m",
                        pnl_usd=-10.0, round=900))
    check("5m and 15m are separate market streams",
          set(st["assets"]) == {"BTC", "BTC15"})
    check("PnL lands on the right stream",
          st["assets"]["BTC"]["pnl_usd"] > 0 > st["assets"]["BTC15"]["pnl_usd"])
    out = render(st, 0.85, Painter(color=False))
    check("header lists both windows", "5m+15m" in out)
    check("rows tag the 15m market", "BTC15" in out)
    check("rules line shows hold to resolution", "hold to resolution" in out)
    check("rules line shows the ask cap", "ask <= 0.72" in out)
    check("rules line shows the 5m lead window, not the 15m x3 one",
          "180-270s left (x3 on 15m)" in out)
    # A 15m shadow settle must not grade a 5m skip of the same round number.
    st2 = new_state()
    fold_event(st2, {"type": "skip", "asset": "BTC", "window": "5m", "round": 900,
                     "reason": "no_lead_setup", "side": "UP", "confidence": 0.5})
    fold_event(st2, {"type": "shadow_settle", "asset": "BTC", "window": "15m",
                     "round": 900, "actual": "UP", "result": "win"})
    skip_row = st2["activity"][0]
    check("15m shadow does not grade the 5m skip", "actual" not in skip_row)


def test_missing_file_is_safe():
    evs, off = read_new_events("/nonexistent/path/live.jsonl", 0)
    check("missing file returns empty", evs == [] and off == 0)


def main():
    test_fold_aggregation()
    test_render_colors_wins_and_losses()
    test_dollar_account_and_backward_compat()
    test_render_no_color_is_plain()
    test_breakeven_verdict_flips_color()
    test_incremental_read_and_truncation()
    test_skip_rows_graded_by_shadow_settle()
    test_15m_stream_is_a_separate_market()
    test_missing_file_is_safe()
    print("\nAll live monitor tests passed.")


if __name__ == "__main__":
    main()
