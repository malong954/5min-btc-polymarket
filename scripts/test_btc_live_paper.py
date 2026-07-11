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
    # Per-trade dollar economics, using each trade's ACTUAL stake (flat mode caps
    # the stake at the available balance, so it isn't always exactly $10).
    running = 100.0
    ok = True
    for s in settles:
        stake = s["stake_usd"]
        expect = round(stake * (1 - 0.85) / 0.85, 2) if s["result"] == "win" else -stake
        if abs(s["pnl_usd"] - expect) > 0.01:
            ok = False
        running += s["pnl_usd"]
        if abs(round(running, 2) - s["balance"]) > 0.02:
            ok = False
    check("per-trade $ pnl matches economics", ok)
    win_usd = 10.0 * (1 - 0.85) / 0.85
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


def test_live_spot_heartbeat_and_round_move():
    """A spot tick passed to step() should drive the heartbeat price live and
    compute the intra-round move (spot - round open), independent of the last
    1m candle close."""
    bars = synth_bars(600, autocorr=0.0, seed=61)
    eng = LivePaperEngine(entry_threshold=0.99)  # no entries; just heartbeats
    # 150s into a round so the round's opening 1m bar has already closed (a live
    # feed would include the forming bar even earlier).
    now = bars[300].ts + 150
    cur = bucket_5m(int(now))
    # Feed a spot far from the last candle close to prove it's used.
    spot = bars[-1].c + 123.0
    events = eng.step(now, [b for b in bars if b.ts + 60 <= now], spot=spot)
    hb = [e for e in events if e["type"] == "heartbeat"][-1]
    check("heartbeat price uses the live spot tick", abs(hb["price"] - spot) < 0.01)
    check("heartbeat reports an intra-round move", hb["round_move"] is not None)


def test_confidence_tier_sizes_up():
    """With --big-mult > 1, trades at conf >= big_conf get a larger stake; lower-
    confidence trades keep the base stake."""
    bars = synth_bars(6000, autocorr=0.6, seed=91)
    eng = LivePaperEngine(entry_threshold=0.2, sizing="flat", stake_usd=10,
                          big_conf=0.80, big_mult=2.0)
    events = drive(eng, bars)
    entries = [e for e in events if e["type"] == "entry"]
    check("some entries were made", len(entries) > 3)
    highs = [e for e in entries if e["confidence"] >= 0.80]
    lows = [e for e in entries if e["confidence"] < 0.80]
    if highs:
        check("high-confidence entries are flagged big_bet", all(e["big_bet"] for e in highs))
        # stake = min(base*2, AVAILABLE balance) — settles can wait for the
        # official resolution, so sizing uses balance net of open stakes.
        check("high-confidence stake is 2x base (capped at available)",
              all(abs(e["stake_usd"] - min(20.0, e.get("avail", e["balance"]))) < 0.05 for e in highs))
    if lows:
        check("low-confidence stake is base (capped at available)",
              all(abs(e["stake_usd"] - min(10.0, e.get("avail", e["balance"]))) < 0.05 for e in lows))
        check("low-confidence entries not flagged big", all(not e["big_bet"] for e in lows))


def test_new_indicators_feed_the_model():
    """Bollinger %B and ROC sub-signals should appear in prediction features."""
    from btc_backtest import MTFModel, score_rounds
    bars = synth_bars(4000, autocorr=0.5, seed=92)
    rows = score_rounds(MTFModel(bars))
    check("model produced rows", len(rows) > 10)
    feats = rows[len(rows) // 2]["features"]
    check("Bollinger %B sub-signal present", "sub_bb_1m" in feats)
    check("ROC sub-signal present", "sub_roc_1m" in feats)


def drive_prices(engine, all_bars, price_fn, poll=30):
    """Like drive(), but supplies entry_prices per poll via price_fn(sec_left)."""
    start = all_bars[0].ts
    end = all_bars[-1].ts + 60
    events = []
    now = start + 40 * 60
    while now <= end:
        cur = bucket_5m(int(now))
        sec_left = (cur + 300) - now
        events.extend(engine.step(now, visible_bars(all_bars, now),
                                  entry_prices=price_fn(sec_left)))
        now += poll
    return events


def test_no_market_price_retries_then_fills():
    # Prices only become available late in the window (sec_left < 80). The armed
    # signal must WAIT and fill on a later poll instead of skipping the round.
    bars = synth_bars(700, autocorr=0.5, seed=31)
    eng = LivePaperEngine(entry_threshold=0.05, require_market_price=True)
    events = drive_prices(eng, bars,
                          lambda sl: {"UP": 0.9, "DOWN": 0.9} if sl < 80 else None)
    entries = [e for e in events for _ in [0] if e["type"] == "entry"]
    check("retry: entries happen despite late prices", len(entries) > 0)
    check("retry: fills record their retry count",
          all(e.get("price_retries", 0) >= 1 for e in entries))
    check("retry: no premature no_market_price skips for filled rounds",
          not any(e["type"] == "skip" and e.get("reason") == "no_market_price"
                  and e["round"] in {en["round"] for en in entries} for e in events))


def test_no_market_price_expires_to_skip():
    # Prices NEVER appear: every armed round must end as a final no_market_price
    # skip (with retries recorded) and be shadow-settled — never entered.
    bars = synth_bars(700, autocorr=0.5, seed=31)
    eng = LivePaperEngine(entry_threshold=0.05, require_market_price=True)
    events = drive_prices(eng, bars, lambda sl: None)
    check("expiry: no entries without a price",
          not any(e["type"] == "entry" for e in events))
    skips = [e for e in events if e["type"] == "skip" and e.get("reason") == "no_market_price"]
    check("expiry: unpriceable rounds end as no_market_price skips", len(skips) > 0)
    check("expiry: skips record retry attempts", all(s.get("price_retries", 0) >= 1 for s in skips))
    check("expiry: skipped rounds shadow-settle",
          any(e["type"] == "shadow_settle" for e in events))


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
    test_live_spot_heartbeat_and_round_move()
    test_confidence_tier_sizes_up()
    test_new_indicators_feed_the_model()
    test_no_market_price_retries_then_fills()
    test_no_market_price_expires_to_skip()
    test_format_event_smoke()
    print("\nAll live paper-trading tests passed.")


if __name__ == "__main__":
    main()
