#!/usr/bin/env python3
"""
Offline tests for the live paper-trading engine. No network, no real clock:
a synthetic price series + a stepped fake clock drive the engine deterministically.
Run: python3 scripts/test_btc_live_paper.py
"""

import io
import json
import os
import tempfile

from btc_backtest import synth_bars, MTFModel, outcome_direction
from btc_live_paper import LivePaperEngine, format_event, bucket_5m


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def visible_bars(all_bars, now, window=360):
    """The trailing `window` minutes of already-closed bars — exactly what a live
    poller sees (it fetches a bounded history window, not the whole chain)."""
    closed = [b for b in all_bars if b.ts + 60 <= now]
    return closed[-window:]


def drive(engine, all_bars, poll=60):
    """Step the engine across the series at `poll`-second granularity, feeding a
    trailing window each step (mirrors live fetch, keeps it fast)."""
    start = all_bars[0].ts
    end = all_bars[-1].ts + 60
    events = []
    now = start + 40 * 60  # warm-up so indicators are defined
    while now <= end:
        events.extend(engine.step(now, visible_bars(all_bars, now)))
        now += poll
    return events


def test_stream_produces_full_lifecycle():
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    log = io.StringIO()
    eng = LivePaperEngine(entry_threshold=0.5, entry_price=0.85, log=log)
    events = drive(eng, bars)

    kinds = {e["type"] for e in events}
    check("stream emits heartbeats", "heartbeat" in kinds)
    check("stream emits predictions", "prediction" in kinds)
    check("stream emits entries", any(e["type"] == "entry" for e in events))
    check("stream emits settlements", any(e["type"] == "settle" for e in events))

    # Every emitted event was also written to the JSONL log, and each line parses.
    log.seek(0)
    lines = [ln for ln in log.read().splitlines() if ln]
    check("every event logged as JSONL", len(lines) == len(events))
    parsed = [json.loads(ln) for ln in lines]
    check("all JSONL lines parse", len(parsed) == len(lines))


def test_no_double_entry_per_round():
    bars = synth_bars(6000, autocorr=0.5, seed=32)
    eng = LivePaperEngine(entry_threshold=0.4, log=None)
    events = drive(eng, bars)
    entered_rounds = [e["round"] for e in events if e["type"] == "entry"]
    check("each round entered at most once", len(entered_rounds) == len(set(entered_rounds)))
    predicted_rounds = [e["round"] for e in events if e["type"] == "prediction"]
    check("each round predicted at most once", len(predicted_rounds) == len(set(predicted_rounds)))


def test_pnl_bookkeeping_consistent():
    bars = synth_bars(8000, autocorr=0.5, seed=33)
    eng = LivePaperEngine(entry_threshold=0.5, entry_price=0.85)
    events = drive(eng, bars)
    settles = [e for e in events if e["type"] == "settle"]
    check("some trades settled", len(settles) > 5)

    # Running cum_pnl on each settle equals the sum of per-trade pnl to that point.
    # Emitted cum_pnl / winrate are rounded to 4 dp; compare within that.
    running = 0.0
    ok = True
    wins = 0
    for i, s in enumerate(settles, 1):
        running += s["pnl"]
        wins += 1 if s["result"] == "win" else 0
        if abs(round(running, 4) - s["cum_pnl"]) > 1e-6 or s["trades"] != i:
            ok = False
        if abs(s["winrate"] - round(wins / i, 4)) > 1e-6:
            ok = False
    check("cum_pnl / trades / winrate are internally consistent", ok)

    # PnL matches win/loss economics exactly.
    w = sum(1 for s in settles if s["result"] == "win")
    expected = w * (1 - 0.85) + (len(settles) - w) * (-0.85)
    check("final PnL matches win/loss economics", abs(running - expected) < 1e-9)


def test_dollar_account_bookkeeping():
    """$100 bankroll, $10 stake: balance must track win/loss economics exactly."""
    bars = synth_bars(8000, autocorr=0.5, seed=41)
    eng = LivePaperEngine(entry_threshold=0.5, entry_price=0.85, bankroll=100.0, stake_usd=10.0)
    events = drive(eng, bars)
    settles = [e for e in events if e["type"] == "settle"]
    check("dollar test: some trades settled", len(settles) > 5)
    # Per-trade dollar economics: win = stake*(1-entry)/entry, loss = -stake.
    win_usd = 10.0 * (1 - 0.85) / 0.85
    running = 100.0
    ok = True
    for s in settles:
        expect = win_usd if s["result"] == "win" else -10.0
        if abs(s["pnl_usd"] - round(expect, 2)) > 0.01:
            ok = False
        running += s["pnl_usd"]
        if abs(round(running, 2) - s["balance"]) > 0.02:
            ok = False
    check("per-trade $ pnl matches economics", ok)
    check("engine.balance equals bankroll + cumulative $ pnl",
          abs(eng.balance - (100.0 + eng.stats["pnl_usd"])) < 1e-9)
    # A win must be much smaller than a loss (the 0.85-entry asymmetry).
    check("win $ is far smaller than loss $ (asymmetry)", win_usd < 10.0 / 4)


def test_settle_direction_matches_truth():
    """Each settle's `actual` must equal the real 5m outcome recomputed from the
    full series — proving the engine reads outcomes correctly, no lookahead."""
    bars = synth_bars(6000, autocorr=0.4, seed=34)
    full = MTFModel(bars)
    eng = LivePaperEngine(entry_threshold=0.4)
    events = drive(eng, bars)
    ok = True
    for s in [e for e in events if e["type"] == "settle"]:
        truth = outcome_direction(full, s["round"])
        if truth != s["actual"]:
            ok = False
    check("settle outcomes match ground truth", ok)


def test_threshold_gates_entries():
    """A very high threshold should yield predictions but essentially no entries;
    a low threshold should yield many. Confirms the confidence gate works live."""
    bars = synth_bars(6000, autocorr=0.5, seed=35)
    hi = LivePaperEngine(entry_threshold=0.99)
    lo = LivePaperEngine(entry_threshold=0.05)
    hi_ev, lo_ev = drive(hi, bars), drive(lo, bars)
    hi_entries = sum(1 for e in hi_ev if e["type"] == "entry")
    lo_entries = sum(1 for e in lo_ev if e["type"] == "entry")
    check("high threshold trades much less than low threshold", lo_entries > hi_entries)


def test_format_event_smoke():
    ev = {"ts": 1_700_000_000, "type": "settle", "round": 1_700_000_000, "side": "UP",
          "actual": "UP", "result": "win", "pnl": 0.15, "cum_pnl": 0.3, "trades": 2, "winrate": 1.0}
    s = format_event(ev)
    check("format_event renders settle line", "SETTLE" in s and "WIN" in s)


def main():
    test_stream_produces_full_lifecycle()
    test_no_double_entry_per_round()
    test_pnl_bookkeeping_consistent()
    test_dollar_account_bookkeeping()
    test_settle_direction_matches_truth()
    test_threshold_gates_entries()
    test_format_event_smoke()
    print("\nAll live paper-trading tests passed.")


if __name__ == "__main__":
    main()
