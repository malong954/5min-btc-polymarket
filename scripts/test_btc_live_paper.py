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


def test_lead_persistence_gate():
    # The full lead setup must HOLD lead_persist seconds before entering: a
    # single armed poll (a 2s threshold-graze) must not fire; the same setup
    # sustained past the persistence window must. Audited motivation: grazes
    # ran 60% winrate / -15.8% EV vs 75% / +9.0% on rule-matched rounds.
    bars = synth_bars(6000, autocorr=0.5, seed=52)
    warm = bars[0].ts + 40 * 60
    prices = {"UP": 0.65, "DOWN": 0.37, "UP_size": 500, "DOWN_size": 500}

    def mk(persist):
        return LivePaperEngine(entry_rule="lead", lead_min_move=0.0001,
                               lead_window=(240.0, 180.0), lead_persist=persist)

    target = None
    for k in range(60):
        rs = bucket_5m(warm) + 300 * (k + 1)
        t0 = rs + 70                      # 230s left — inside the lead window
        evs = mk(0.0).step(t0, visible_bars(bars, t0), entry_prices=prices)
        if any(e["type"] == "entry" for e in evs):
            target = t0
            break
    check("legacy persist=0 enters on the first armed poll", target is not None)
    t0 = target
    eng = mk(5.0)
    e1 = eng.step(t0, visible_bars(bars, t0), entry_prices=prices)
    check("persist: first armed poll does not enter",
          not any(e["type"] == "entry" for e in e1))
    e2 = eng.step(t0 + 2, visible_bars(bars, t0 + 2), entry_prices=prices)
    check("persist: a 2s graze still does not enter",
          not any(e["type"] == "entry" for e in e2))
    e3 = eng.step(t0 + 6, visible_bars(bars, t0 + 6), entry_prices=prices)
    check("persist: setup sustained past 5s enters",
          any(e["type"] == "entry" for e in e3))


def test_settle_precedence_official_chainlink_spot():
    # settle_outcome order: official first; after official_wait a Chainlink
    # provisional grade beats the spot fallback; official always wins over both.
    bars = synth_bars(6000, autocorr=0.5, seed=44)
    warm = bars[0].ts + 40 * 60
    rs = bucket_5m(warm) + 300           # a full round after warm-up
    after_wait = rs + 300 + 200.0        # past official_wait=150

    def run(official, provisional):
        eng = LivePaperEngine(entry_threshold=0.5)
        eng.positions[rs] = {"side": "UP", "entry_price": 0.65, "confidence": 0.6,
                             "opened_ts": rs, "stake_usd": 10.0, "price_source": "polymarket"}
        evs = eng.step(after_wait, visible_bars(bars, after_wait),
                       official=official, provisional=provisional)
        return next((e for e in evs if e["type"] == "settle"), None)

    ev = run(None, {rs: "UP"})
    check("chainlink provisional used after the wait",
          ev is not None and ev["settle_source"] == "chainlink" and ev["actual"] == "UP")
    ev2 = run({rs: "DOWN"}, {rs: "UP"})
    check("official beats provisional",
          ev2 is not None and ev2["settle_source"] == "official" and ev2["actual"] == "DOWN")
    ev3 = run(None, None)
    check("no provisional -> spot fallback still settles",
          ev3 is not None and ev3["settle_source"] == "spot_fallback")


def test_requote_pays_worse_never_better():
    # Honest fill: entries re-price to the WORSE of decision ask vs fresh ask.
    eng = LivePaperEngine(entry_threshold=0.5)
    eng.positions[900] = {"side": "UP", "entry_price": 0.66, "confidence": 0.6,
                          "opened_ts": 1, "stake_usd": 10.0, "price_source": "polymarket"}
    ev = eng.apply_requote(900, {"UP": 0.70, "DOWN": 0.32}, now=2.0)
    check("requote event emitted", ev is not None and ev["type"] == "requote")
    check("slip measured", abs(ev["slip"] - 0.04) < 1e-9)
    check("position re-priced to the worse ask", eng.positions[900]["entry_price"] == 0.70)
    check("second requote is a no-op", eng.apply_requote(900, {"UP": 0.90}, 3.0) is None)
    # A better fresh ask records the slip but never improves the fill.
    eng.positions[901] = {"side": "DOWN", "entry_price": 0.60, "confidence": 0.6,
                          "opened_ts": 1, "stake_usd": 10.0, "price_source": "polymarket"}
    ev2 = eng.apply_requote(901, {"DOWN": 0.55}, now=2.0)
    check("favorable move logged as negative slip", abs(ev2["slip"] + 0.05) < 1e-9)
    check("fill never improves", eng.positions[901]["entry_price"] == 0.60)
    # Fixed-price entries (no real book) are left alone.
    eng.positions[902] = {"side": "UP", "entry_price": 0.85, "confidence": 0.6,
                          "opened_ts": 1, "stake_usd": 10.0, "price_source": "fixed"}
    check("fixed-price entries not requoted", eng.apply_requote(902, {"UP": 0.99}, 2.0) is None)
    check("requote line renders",
          "requote" in format_event({"ts": 1, "type": "requote", "side": "UP",
                                     "decision_ask": 0.66, "fresh_ask": 0.70,
                                     "slip": 0.04, "filled_at": 0.70}))


def test_format_event_smoke():
    ev = {"ts": 1_700_000_000, "type": "settle", "round": 1_700_000_000, "side": "UP",
          "actual": "UP", "result": "win", "pnl": 0.15, "cum_pnl": 0.3, "trades": 2, "winrate": 1.0}
    s = format_event(ev)
    check("format_event renders settle line", "SETTLE" in s and "WIN" in s)


def test_15m_window_rounds_and_tagging():
    """--window 15m: rounds are 900s buckets, events are stamped window=15m, and
    settles grade the full 15m round (open of first 1m bar vs close of last)."""
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    eng = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, window="15m")
    events = drive(eng, bars)

    check("15m stream emits entries", any(e["type"] == "entry" for e in events))
    check("15m events stamped window=15m", all(e.get("window") == "15m" for e in events))
    check("15m rounds are 900s-aligned",
          all(e["round"] % 900 == 0 for e in events if "round" in e))
    settles = [e for e in events if e["type"] == "settle"]
    check("15m rounds settle", len(settles) > 0)
    # Grade each settle against the true 15m open->close from the raw bars.
    by_ts = {b.ts: b for b in bars}
    ok = True
    for s in settles:
        rs = s["round"]
        o, c = by_ts.get(rs), by_ts.get(rs + 840)
        if o is None or c is None:
            continue
        truth = "UP" if c.c > o.o else "DOWN" if c.c < o.o else "FLAT"
        if truth in ("UP", "DOWN") and s["actual"] != truth:
            ok = False
    check("15m settles match the true 15m open->close", ok)
    # 5m engine untouched: same drive still emits 300s-aligned window=5m events.
    eng5 = LivePaperEngine(entry_threshold=0.0, entry_price=0.85)
    ev5 = drive(eng5, bars)
    check("5m default unchanged (window=5m, 300s rounds)",
          all(e.get("window") == "5m" for e in ev5)
          and all(e["round"] % 300 == 0 for e in ev5 if "round" in e))


def test_regime_gate_blocks_flat_markets():
    """regime_min_move: an unreachable floor blocks every entry (skipped as
    flat_regime, still shadow-graded); a negligible floor changes nothing."""
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    eng = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, regime_min_move=1e9)
    events = drive(eng, bars)
    check("huge regime floor blocks every entry",
          not any(e["type"] == "entry" for e in events))
    sided_skips = [e for e in events if e["type"] == "skip" and e.get("side")]
    check("blocked rounds skip as flat_regime",
          sided_skips and all(e["reason"] == "flat_regime" for e in sided_skips))
    check("flat_regime skips report the measured trailing move",
          all(e.get("regime_move") is not None for e in sided_skips))
    check("flat_regime skips still shadow-grade",
          any(e["type"] == "shadow_settle" for e in events))
    eng2 = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, regime_min_move=1e-9)
    ev2 = drive(eng2, bars)
    check("negligible regime floor does not block entries",
          any(e["type"] == "entry" for e in ev2))


def test_quiet_rule_gates_on_calm_and_divergence():
    """entry_rule 'quiet': a generous vol cap trades, an impossible one skips
    every round as loud_market — and the regime gate must NOT block it (quiet
    deliberately trades the calm rounds the gate exists to skip)."""
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    calm = LivePaperEngine(entry_rule="quiet", quiet_vol_max=1e9, entry_price=0.85,
                           regime_min_move=1e9)  # gate would block everything...
    ev_calm = drive(calm, bars)
    check("quiet rule enters on calm rounds (regime gate exempt)",
          any(e["type"] == "entry" for e in ev_calm))
    check("quiet rule never skips flat_regime",
          not any(e["type"] == "skip" and e.get("reason") == "flat_regime" for e in ev_calm))
    loud = LivePaperEngine(entry_rule="quiet", quiet_vol_max=-1.0, entry_price=0.85)
    ev_loud = drive(loud, bars)
    check("impossible vol cap blocks every entry",
          not any(e["type"] == "entry" for e in ev_loud))
    sided = [e for e in ev_loud if e["type"] == "skip" and e.get("side")]
    check("blocked rounds skip as loud_market",
          sided and all(e["reason"] == "loud_market" for e in sided))
    check("loud skips still shadow-grade",
          any(e["type"] == "shadow_settle" for e in ev_loud))


def test_quiet_rule_is_one_shot():
    """The quiet decision is taken at the FIRST evaluable poll only. A round
    that is dirty at the snapshot but clean afterwards must NOT enter (no
    re-arming: live re-arming was measured to triple trades and pay ~5c more);
    a round clean at the snapshot but dirty afterwards must still enter.
    Divergence flips per-poll via a patched feature_snapshot keyed on the
    current round (the test clock stamps it before every step)."""
    import btc_live_paper as blp
    orig = blp.feature_snapshot
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    first_seen: set = set()
    cur_round = {"r": None}

    def make_stub(div_on_first: bool):
        def stub(sig):
            f = dict(orig(sig))
            r = cur_round["r"]
            first = r not in first_seen
            first_seen.add(r)
            f["divergence"] = "regular_bearish" if first == div_on_first else None
            return f
        return stub

    def drive_stamped(engine, all_bars, poll=60):
        start, end = all_bars[0].ts, all_bars[-1].ts + 60
        events, now = [], start + 40 * 60
        while now <= end:
            cur_round["r"] = bucket_5m(int(now))
            events.extend(engine.step(now, visible_bars(all_bars, now)))
            now += poll
        return events

    try:
        first_seen.clear()
        blp.feature_snapshot = make_stub(div_on_first=True)
        eng = LivePaperEngine(entry_rule="quiet", quiet_vol_max=1e9, entry_price=0.85)
        ev = drive_stamped(eng, bars)
        check("dirty snapshot -> clean later: never enters",
              not any(e["type"] == "entry" for e in ev))
        sided = [e for e in ev if e["type"] == "skip" and e.get("side")]
        check("those rounds skip as divergence_present",
              sided and all(e["reason"] == "divergence_present" for e in sided))

        first_seen.clear()
        blp.feature_snapshot = make_stub(div_on_first=False)
        eng2 = LivePaperEngine(entry_rule="quiet", quiet_vol_max=1e9, entry_price=0.85)
        ev2 = drive_stamped(eng2, bars)
        check("clean snapshot -> dirty later: still enters (one-shot decision)",
              any(e["type"] == "entry" for e in ev2))
    finally:
        blp.feature_snapshot = orig


def test_session_gate():
    """Only the listed UTC sessions may trade; others skip as off_session but
    are still predicted and shadow-graded. Measured motivation: cheap leading
    sides win 71% in asia vs 51% in us (informed flow)."""
    from btc_live_paper import _session_of
    import calendar
    check("00:00 UTC is asia", _session_of(calendar.timegm((2026, 7, 25, 3, 0, 0, 0, 0, 0))) == "asia")
    check("10:00 UTC is europe", _session_of(calendar.timegm((2026, 7, 25, 10, 0, 0, 0, 0, 0))) == "europe")
    check("20:00 UTC is us", _session_of(calendar.timegm((2026, 7, 25, 20, 0, 0, 0, 0, 0))) == "us")

    bars = synth_bars(6000, autocorr=0.5, seed=31)
    # The synthetic clock spans whichever sessions it spans; gate to the set the
    # run actually touches vs its complement and assert the two are exclusive.
    touched = {_session_of(b.ts) for b in bars}
    allow = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, sessions=touched)
    ev_allow = drive(allow, bars)
    check("gate open for the sessions in range -> trades",
          any(e["type"] == "entry" for e in ev_allow))
    blocked = LivePaperEngine(entry_threshold=0.0, entry_price=0.85,
                              sessions={"__none__"})
    ev_block = drive(blocked, bars)
    check("gate closed -> no entries at all",
          not any(e["type"] == "entry" for e in ev_block))
    sided = [e for e in ev_block if e["type"] == "skip" and e.get("side")]
    check("blocked rounds skip as off_session",
          sided and all(e["reason"] == "off_session" for e in sided))
    check("off_session rounds still shadow-grade",
          any(e["type"] == "shadow_settle" for e in ev_block))
    check("sessions=None (default) trades everything",
          any(e["type"] == "entry" for e in
              drive(LivePaperEngine(entry_threshold=0.0, entry_price=0.85), bars)))


def test_tiered_sizing_follows_the_account():
    """SIZING=tiered: 25% of balance at/above the starting bankroll, 10% at
    50-100%, 5% below 50% — aggressive above water, defensive in a hole."""
    from btc_sizing import stake_for
    kw = dict(base_stake=10, confidence=0.5, entry_price=0.7, start_bankroll=100.0)
    check("at the start: 25% tier", abs(stake_for("tiered", bankroll=100, balance=100.0, **kw) - 25.0) < 1e-9)
    check("above water: still 25%", abs(stake_for("tiered", bankroll=140, balance=140.0, **kw) - 35.0) < 1e-9)
    check("drawdown to 50-100%: 10% tier", abs(stake_for("tiered", bankroll=80, balance=80.0, **kw) - 8.0) < 1e-9)
    check("below half: 5% tier", abs(stake_for("tiered", bankroll=30, balance=30.0, **kw) - 1.5) < 1e-9)
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    eng = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, sizing="tiered", bankroll=100.0)
    ev = drive(eng, bars)
    entries = [e for e in ev if e["type"] == "entry"]
    check("tiered engine enters", bool(entries))
    check("first tiered stake is 25% of the account", abs(entries[0]["stake_usd"] - 25.0) < 1e-9)


def test_skip_band_refuses_the_dead_zone():
    """--skip-band 0.60:0.80: armed rounds whose ask sits inside the band skip
    as mid_band; asks outside the band trade normally."""
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    mid = LivePaperEngine(entry_threshold=0.0, skip_band=(0.60, 0.80),
                          require_market_price=True)
    ev_mid = drive_prices(mid, bars, lambda sl: {"UP": 0.70, "DOWN": 0.70})
    check("in-band asks never enter", not any(e["type"] == "entry" for e in ev_mid))
    sided = [e for e in ev_mid if e["type"] == "skip" and e.get("side")]
    check("in-band rounds skip as mid_band",
          sided and all(e["reason"] == "mid_band" for e in sided))
    out = LivePaperEngine(entry_threshold=0.0, skip_band=(0.60, 0.80),
                          require_market_price=True)
    ev_out = drive_prices(out, bars, lambda sl: {"UP": 0.55, "DOWN": 0.55})
    check("below-band asks trade normally", any(e["type"] == "entry" for e in ev_out))
    hi = LivePaperEngine(entry_threshold=0.0, skip_band=(0.60, 0.80),
                         require_market_price=True, max_entry_price=0.97)
    ev_hi = drive_prices(hi, bars, lambda sl: {"UP": 0.82, "DOWN": 0.82})
    check("above-band asks trade normally", any(e["type"] == "entry" for e in ev_hi))


def test_cooldown_after_loss():
    """--cooldown-loss 1: after each settled loss the next armed round is
    refused (reason cooldown), shadow-graded, and only ONE round is consumed."""
    bars = synth_bars(8000, autocorr=0.5, seed=33)
    eng = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, cooldown_loss=1)
    ev = drive(eng, bars)
    losses = [e for e in ev if e["type"] == "settle" and e["result"] == "loss"]
    cds = [e for e in ev if e["type"] == "skip" and e.get("reason") == "cooldown"]
    check("losses occurred (test is meaningful)", len(losses) > 3)
    check("cooldown skips occur after losses", bool(cds))
    check("at most one cooldown per loss", len(cds) <= len(losses))
    # every cooldown skip must come after at least one loss settle
    first_loss_ts = losses[0]["ts"]
    check("no cooldown before the first loss", all(c["ts"] >= first_loss_ts for c in cds))
    check("cooldown rounds still shadow-grade",
          any(e["type"] == "shadow_settle" and e.get("reason") == "cooldown" for e in ev))
    plain = LivePaperEngine(entry_threshold=0.0, entry_price=0.85)
    ev_plain = drive(plain, bars)
    check("cooldown off -> no cooldown skips",
          not any(e["type"] == "skip" and e.get("reason") == "cooldown" for e in ev_plain))


def test_ask_fall_veto():
    """--ask-fall-veto: an entry whose side-ask FELL by >= the veto over the
    trailing ~60s is refused (ask_falling); flat or rising asks trade."""
    bars = synth_bars(6000, autocorr=0.5, seed=31)
    t0 = bars[0].ts

    class Feed:
        """Price feed keyed on the engine's wall clock via closure state."""
        def __init__(self, slope_per_min):
            self.slope = slope_per_min
        def __call__(self, now):
            m = (now - t0) % 300 / 60.0
            px = 0.80 + self.slope * m
            px = min(max(px, 0.05), 0.95)
            return {"UP": px, "DOWN": px}

    def drive_wall(engine, feed, poll=15):
        start, end = bars[0].ts, bars[-1].ts + 60
        events, now = [], start + 40 * 60
        while now <= end:
            events.extend(engine.step(now, visible_bars(bars, now),
                                      entry_prices=feed(now)))
            now += poll
        return events

    fall = LivePaperEngine(entry_threshold=0.0, ask_fall_veto=0.02,
                           require_market_price=True)
    ev_fall = drive_wall(fall, Feed(-0.06))
    check("falling ask never enters", not any(e["type"] == "entry" for e in ev_fall))
    sided = [e for e in ev_fall if e["type"] == "skip" and e.get("side")]
    check("refusals carry reason ask_falling",
          sided and all(e["reason"] == "ask_falling" for e in sided))
    rise = LivePaperEngine(entry_threshold=0.0, ask_fall_veto=0.02,
                           require_market_price=True)
    ev_rise = drive_wall(rise, Feed(+0.06))
    check("rising ask trades normally", any(e["type"] == "entry" for e in ev_rise))
    off = LivePaperEngine(entry_threshold=0.0, require_market_price=True)
    ev_off = drive_wall(off, Feed(-0.06))
    check("veto off -> falling ask still trades (measurement default)",
          any(e["type"] == "entry" for e in ev_off))


def test_divergence_veto_and_boost():
    """div_mode veto refuses divergence rounds (skip reason divergence_veto);
    div_mode boost multiplies the stake on those same rounds. Divergence is
    injected by patching feature_snapshot — the model's own synthetic-series
    divergences are too rare to assert on."""
    import btc_live_paper as blp
    orig = blp.feature_snapshot
    blp.feature_snapshot = lambda sig: {**orig(sig), "divergence": "regular_bullish"}
    try:
        bars = synth_bars(6000, autocorr=0.5, seed=31)
        veto = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, div_mode="veto")
        ev_veto = drive(veto, bars)
        check("veto blocks every (all-divergence) entry",
              not any(e["type"] == "entry" for e in ev_veto))
        sided = [e for e in ev_veto if e["type"] == "skip" and e.get("side")]
        check("vetoed rounds skip as divergence_veto",
              sided and all(e["reason"] == "divergence_veto" for e in sided))
        boost = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, div_mode="boost",
                                div_boost_mult=2.0, stake_usd=10.0, bankroll=1000.0)
        ev_boost = drive(boost, bars)
        entries = [e for e in ev_boost if e["type"] == "entry"]
        check("boost still enters", bool(entries))
        check("every entry flags div_boost", all(e.get("div_boost") for e in entries))
        # x2 whenever the account can afford it; a drawn-down balance clamps to
        # avail instead (never risk money that is not there).
        check("boosted entries stake x2 (clamped to avail)",
              all(abs(e["stake_usd"] - min(20.0, e["avail"])) < 1e-9 for e in entries))
    finally:
        blp.feature_snapshot = orig
    plain = LivePaperEngine(entry_threshold=0.0, entry_price=0.85, div_mode="boost",
                            div_boost_mult=2.0, stake_usd=10.0, bankroll=1000.0)
    ev_plain = drive(plain, bars)
    undiv = [e for e in ev_plain if e["type"] == "entry" and not (e.get("features") or {}).get("divergence")]
    check("no-divergence rounds stake the base amount (clamped to avail)",
          undiv and all(abs(e["stake_usd"] - min(10.0, e["avail"])) < 1e-9
                        and not e.get("div_boost") for e in undiv))


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
    test_lead_persistence_gate()
    test_settle_precedence_official_chainlink_spot()
    test_requote_pays_worse_never_better()
    test_format_event_smoke()
    test_15m_window_rounds_and_tagging()
    test_regime_gate_blocks_flat_markets()
    test_quiet_rule_gates_on_calm_and_divergence()
    test_quiet_rule_is_one_shot()
    test_session_gate()
    test_tiered_sizing_follows_the_account()
    test_skip_band_refuses_the_dead_zone()
    test_cooldown_after_loss()
    test_ask_fall_veto()
    test_divergence_veto_and_boost()
    print("\nAll live paper-trading tests passed.")


if __name__ == "__main__":
    main()
