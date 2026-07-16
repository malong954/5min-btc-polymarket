#!/usr/bin/env python3
"""Offline tests for the entry-timing analyzer. Run:
    python3 scripts/test_btc_entry_timing.py"""

from btc_entry_timing import (analyze, auto_min_move, calibration_analysis,
                              confidence_bands, crossing_analysis,
                              edge_gate_analysis, fade_analysis,
                              session_analysis)


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def build(rounds):
    """rounds: list of (samples, outcome) where samples is [(sec_left, move, up, dn), ...]."""
    evs = []
    for i, (samps, outcome) in enumerate(rounds):
        for sl, mv, up, dn in samps:
            evs.append({"type": "sample", "round": i, "sec_left": sl, "move": mv,
                        "up_ask": up, "dn_ask": dn, "spot": 60000})
        evs.append({"type": "result", "round": i, "outcome": outcome})
    return evs


def test_price_rises_toward_close():
    # Every round: BTC up (move>0 -> buy UP), UP ask climbs from 0.80 (early) to 0.98
    # (late), and it always resolves UP -> 100% win at every offset, cheaper early.
    rounds = []
    for _ in range(20):
        samps = [(250, 40, 0.80, 0.20), (190, 55, 0.88, 0.12),
                 (130, 70, 0.94, 0.06), (70, 80, 0.98, 0.02)]
        rounds.append((samps, "UP"))
    rows = analyze(build(rounds))
    by = {tuple(r["offset"]): r for r in rows}
    check("all offset bins populated", len(rows) >= 4)
    check("early entry cheaper than late",
          by[(240, 270)]["avg_price"] < by[(60, 90)]["avg_price"])
    check("early entry has higher edge when winrate equal",
          by[(240, 270)]["edge"] > by[(60, 90)]["edge"])
    check("winrate 100% (always resolves with the move)", all(r["winrate"] == 1.0 for r in rows))


def test_early_less_certain():
    # Half the rounds reverse: early the move points the wrong way sometimes, but by
    # late the move aligns with outcome. So late winrate should exceed early winrate.
    rounds = []
    for i in range(20):
        outcome = "UP" if i % 2 == 0 else "DOWN"
        # early move is noisy (random-ish), late move matches outcome
        early_mv = 30 if i % 3 == 0 else -30
        late_mv = 60 if outcome == "UP" else -60
        samps = [(250, early_mv, 0.82, 0.18), (70, late_mv, 0.97, 0.03)]
        rounds.append((samps, outcome))
    rows = analyze(build(rounds))
    by = {tuple(r["offset"]): r for r in rows}
    check("both bins present", (240, 270) in by and (60, 90) in by)
    check("late entry more accurate than early", by[(60, 90)]["winrate"] >= by[(240, 270)]["winrate"])


def test_efficient_market_negative_edge():
    # Price always equals the true win probability -> edge ~0 or negative everywhere.
    rounds = []
    for i in range(40):
        outcome = "UP" if i % 10 < 8 else "DOWN"   # 80% up
        samps = [(120, 50, 0.80, 0.20)]            # priced exactly at 0.80
        rounds.append((samps, outcome))
    rows = analyze(build(rounds))
    check("priced-fair market shows ~zero edge", all(r["edge"] <= 0.05 for r in rows))


def build_ind(rounds):
    """rounds: list of (samples, outcome), samples = [(sec_left, up, dn, ind_dir, ind_conf), ...]."""
    evs = []
    for i, (samps, outcome) in enumerate(rounds):
        for sl, up, dn, idir, iconf in samps:
            evs.append({"type": "sample", "round": i, "sec_left": sl, "move": 1.0,
                        "up_ask": up, "dn_ask": dn, "ind_dir": idir, "ind_conf": iconf})
        evs.append({"type": "result", "round": i, "outcome": outcome})
    return evs


def test_indicator_side_and_min_conf():
    # Indicator calls UP at 0.82 early. When it's confident (conf>=0.7) it is
    # right; when unsure (conf<0.7) it is often wrong. min-conf should raise edge.
    rounds = []
    for i in range(20):
        confident = i % 2 == 0
        conf = 0.85 if confident else 0.55
        idir = "UP"
        outcome = "UP" if confident else "DOWN"   # confident calls win, unsure lose
        rounds.append(([(70, 0.82, 0.18, idir, conf)], outcome))
    evs = build_ind(rounds)
    all_rows = analyze(evs, side_source="indicator")
    hi_rows = analyze(evs, side_source="indicator", min_conf=0.7)
    by_all = {tuple(r["offset"]): r for r in all_rows}
    by_hi = {tuple(r["offset"]): r for r in hi_rows}
    check("indicator side produces rows", (60, 90) in by_all)
    check("min-conf filters to the confident (winning) subset",
          by_hi[(60, 90)]["winrate"] > by_all[(60, 90)]["winrate"])
    check("min-conf edge beats unfiltered edge",
          by_hi[(60, 90)]["edge"] > by_all[(60, 90)]["edge"])


def test_confidence_bands_edge_rises():
    # High-confidence rounds priced 0.80 win 100%; low-confidence priced 0.80 win
    # 50%. Edge should be strongly positive in the top band, negative in the low.
    rounds = []
    for i in range(40):
        if i < 20:
            rounds.append(([(120, 0.80, 0.20, "UP", 0.90)], "UP"))          # high conf, always right
        else:
            outcome = "UP" if i % 2 == 0 else "DOWN"                          # low conf, coin flip
            rounds.append(([(120, 0.80, 0.20, "UP", 0.55)], outcome))
    rows = confidence_bands(build_ind(rounds))
    by = {tuple(r["band"]): r for r in rows}
    check("both confidence bands present", (0.5, 0.6) in by and (0.9, 1.01) in by)
    check("top band edge positive", by[(0.9, 1.01)]["edge"] > 0)
    check("low band edge below top band", by[(0.5, 0.6)]["edge"] < by[(0.9, 1.01)]["edge"])


def test_fade_divergence():
    # BTC leads UP (move>0) but bearish divergence (-1) says DOWN. The DOWN ask is
    # cheap (0.20). In 30 rounds the fade wins 12 (40% winrate at a 0.20 price =
    # +0.20 edge, +100% EV). Also: rounds where divergence AGREES with the move
    # must not be counted, and expensive trailing asks are excluded.
    evs = []
    r = 0
    for i in range(30):
        r += 1
        win = i % 5 < 2   # 12/30 = 40% fade winrate
        evs.append({"type": "sample", "round": r, "sec_left": 100, "move": 30,
                    "up_ask": 0.80, "dn_ask": 0.20, "ind_div": -1})
        evs.append({"type": "result", "round": r, "outcome": "DOWN" if win else "UP"})
    # agreeing divergence -> ignored
    r += 1
    evs.append({"type": "sample", "round": r, "sec_left": 100, "move": 30,
                "up_ask": 0.80, "dn_ask": 0.20, "ind_div": 1})
    evs.append({"type": "result", "round": r, "outcome": "UP"})
    # opposing but trailing ask too expensive (0.60 > 0.40 band) -> counted opposing, not traded
    r += 1
    evs.append({"type": "sample", "round": r, "sec_left": 100, "move": 30,
                "up_ask": 0.40, "dn_ask": 0.60, "ind_div": -1})
    evs.append({"type": "result", "round": r, "outcome": "UP"})
    res = fade_analysis(evs)
    check("fade counts opposing rounds", res["n_opposing"] == 31)
    check("fade trades only the cheap band", res["n_traded"] == 30)
    check("fade winrate 40%", abs(res["winrate"] - 0.40) < 1e-9)
    check("fade edge positive (0.40 win at 0.20 cost)", res["edge"] > 0.15)
    check("fade EV strongly positive", res["ev_pct"] > 0.5)


def test_fade_no_setups():
    # No divergence anywhere -> no rows, no crash.
    evs = [{"type": "sample", "round": 1, "sec_left": 100, "move": 30,
            "up_ask": 0.8, "dn_ask": 0.2, "ind_div": 0},
           {"type": "result", "round": 1, "outcome": "UP"}]
    res = fade_analysis(evs)
    check("no fade setups handled", res["n_traded"] == 0 and res.get("winrate") is None)


def test_crossing_first_moment():
    # Confidence climbs through each round: 0.55 @200s (ask 0.80), 0.75 @120s
    # (ask 0.90), 0.95 @60s (ask 0.98). Crossing X=0.7 must buy at the 120s
    # sample (0.90), X=0.9 at the 60s sample (0.98) — the FIRST qualifying
    # moment, not the best or last one.
    evs = []
    for i in range(10):
        for sl, c, ask in [(200, 0.55, 0.80), (120, 0.75, 0.90), (60, 0.95, 0.98)]:
            evs.append({"type": "sample", "round": i, "sec_left": sl, "move": 20,
                        "up_ask": ask, "dn_ask": round(1 - ask, 2),
                        "ind_dir": "UP", "ind_conf": c})
        evs.append({"type": "result", "round": i, "outcome": "UP"})
    rows = crossing_analysis(evs, thresholds=[0.5, 0.7, 0.9])
    by = {r["threshold"]: r for r in rows}
    check("crossing 0.5 fills at the earliest sample", abs(by[0.5]["avg_price"] - 0.80) < 1e-9)
    check("crossing 0.7 fills at the 120s sample", abs(by[0.7]["avg_price"] - 0.90) < 1e-9)
    check("crossing 0.9 fills at the 60s sample", abs(by[0.9]["avg_price"] - 0.98) < 1e-9)
    check("earlier crossing is cheaper", by[0.5]["avg_price"] < by[0.9]["avg_price"])
    check("crossing avg sec_left ordered", by[0.5]["avg_sec_left"] > by[0.9]["avg_sec_left"])


def test_edge_gate_price_is_hurdle():
    # Round shape: early sample conf 0.85 at ask 0.70 (conf clears ask), late
    # sample conf 0.95 at ask 0.98 (conf can NEVER clear). Margin 0 must buy the
    # EARLY cheap moment; margin 0.20 must find no qualifying moment at all.
    evs = []
    for i in range(10):
        evs.append({"type": "sample", "round": i, "sec_left": 120, "move": 20,
                    "up_ask": 0.70, "dn_ask": 0.30, "ind_dir": "UP", "ind_conf": 0.85})
        evs.append({"type": "sample", "round": i, "sec_left": 60, "move": 40,
                    "up_ask": 0.98, "dn_ask": 0.02, "ind_dir": "UP", "ind_conf": 0.95})
        evs.append({"type": "result", "round": i, "outcome": "UP"})
    rows = edge_gate_analysis(evs, margins=[0.0, 0.20])
    by = {r["margin"]: r for r in rows}
    check("edge-gate margin 0 buys the cheap qualifying moment",
          by[0.0]["n"] == 10 and abs(by[0.0]["avg_price"] - 0.70) < 1e-9)
    check("edge-gate buys early (120s left)", by[0.0]["avg_sec_left"] == 120)
    check("edge-gate huge margin finds nothing", by[0.20]["n"] == 0)


def test_official_result_overrides_spot():
    # Spot feed says UP, but Polymarket officially resolved DOWN -> the analyzer
    # must grade against the OFFICIAL outcome.
    evs = [
        {"type": "sample", "round": 0, "sec_left": 100, "move": 10, "up_ask": 0.7, "dn_ask": 0.3},
        {"type": "result", "round": 0, "open": 60000, "close": 60000.5, "outcome": "UP"},
        {"type": "result_pm", "round": 0, "outcome": "DOWN"},
    ]
    rows = analyze(evs)
    check("official resolution overrides spot label", rows[0]["winrate"] == 0.0)


def test_min_move_drops_flat_rounds():
    evs = [
        # flat round (|close-open| = 0.5): excluded at min_move=10
        {"type": "sample", "round": 0, "sec_left": 100, "move": 1, "up_ask": 0.6, "dn_ask": 0.4},
        {"type": "result", "round": 0, "open": 60000, "close": 60000.5, "outcome": "UP"},
        # decisive round: kept
        {"type": "sample", "round": 1, "sec_left": 100, "move": 50, "up_ask": 0.8, "dn_ask": 0.2},
        {"type": "result", "round": 1, "open": 60000, "close": 60050, "outcome": "UP"},
    ]
    all_rows = analyze(evs)
    solid = analyze(evs, min_move=10.0)
    check("min-move keeps only decisive rounds", all_rows[0]["n"] == 2 and solid[0]["n"] == 1)


def test_empty_and_missing():
    check("empty log -> no rows", analyze([]) == [])
    # samples without a result round are ignored.
    evs = [{"type": "sample", "round": 5, "sec_left": 100, "move": 10, "up_ask": 0.9, "dn_ask": 0.1}]
    check("samples without result ignored", analyze(evs) == [])


def test_auto_min_move_scales_with_price():
    # BTC-priced log -> ~$10; ETH-priced -> cents; XRP-priced -> sub-cent.
    def log_at(px):
        return [{"type": "sample", "round": 0, "sec_left": 200, "move": 1,
                 "up_ask": 0.5, "dn_ask": 0.5, "spot": px}]
    check("auto move ~ $10 at BTC price", abs(auto_min_move(log_at(62500)) - 10.0) < 0.01)
    check("auto move ~ $0.28 at ETH price", abs(auto_min_move(log_at(1780)) - 0.2848) < 0.001)
    check("auto move sub-cent at XRP price", 0 < auto_min_move(log_at(2.5)) < 0.001)
    check("no samples falls back to $10", auto_min_move([]) == 10.0)


def test_session_analysis_buckets_and_lead():
    # Two rounds in the Asia block (00-08 UTC, both resolve UP with a lead
    # setup that wins), one weekend round in the US block resolving DOWN with
    # a losing lead pick. 2026-07-14 is a Tuesday; 2026-07-12 a Sunday.
    asia1, asia2 = 1783990800, 1783994400            # Tue 2026-07-14 01:00/02:00 UTC
    us_wknd = 1783876500                              # Sun 2026-07-12 17:15 UTC
    evs = []
    for r, mv, up_ask in ((asia1, 20.0, 0.65), (asia2, 25.0, 0.66)):
        evs.append({"type": "sample", "round": r, "sec_left": 200, "move": mv,
                    "up_ask": up_ask, "dn_ask": 0.40, "spot": 62000, "ind_conf": 0.5})
        evs.append({"type": "result", "round": r, "open": 62000, "close": 62000 + mv, "outcome": "UP"})
        evs.append({"type": "result_pm", "round": r, "outcome": "UP"})
    evs.append({"type": "sample", "round": us_wknd, "sec_left": 200, "move": 30.0,
                "up_ask": 0.60, "dn_ask": 0.45, "spot": 62000, "ind_conf": 0.5})
    evs.append({"type": "result", "round": us_wknd, "open": 62000, "close": 61970, "outcome": "DOWN"})
    evs.append({"type": "result_pm", "round": us_wknd, "outcome": "DOWN"})
    rows = {r["bucket"]: r for r in session_analysis(evs, min_move_signal=10.0)}
    asia = rows["Asia   00-08 UTC"]
    check("asia bucket has both weekday rounds", asia["rounds"] == 2)
    check("asia drift 100% UP", asia["up_pct"] == 1.0)
    check("asia lead setup found and won", asia["lead_n"] == 2 and asia["lead_winrate"] == 1.0)
    us = rows["US     16-24 UTC"]
    check("US bucket has the weekend round", us["rounds"] == 1 and us["up_pct"] == 0.0)
    check("US lead pick lost (bought UP, resolved DOWN)",
          us["lead_n"] == 1 and us["lead_winrate"] == 0.0)
    wk = rows["weekday (Mon-Fri)"]
    we = rows["weekend (Sat-Sun)"]
    check("weekday/weekend split", wk["rounds"] == 2 and we["rounds"] == 1)
    # conviction floor above the recorded ind_conf removes the lead picks
    rows2 = {r["bucket"]: r for r in session_analysis(evs, min_move_signal=10.0, lead_min_conf=0.9)}
    check("conf floor filters lead picks", rows2["Asia   00-08 UTC"]["lead_n"] == 0)


def test_combo_min_price_filters_book_disagreement():
    from btc_entry_timing import combo_analysis
    # Two rounds. Round A: our move says UP (+40) but the book prices UP cheap
    # (ask 0.10) — the market disagrees, and it resolves DOWN (a poisoned/bad-
    # open loser). Round B: move says UP (+40), book agrees (UP ask 0.62), and
    # it resolves UP (a genuine winner). The plain combo grades A as a loss and
    # includes it; min_price=0.40 should DROP round A entirely, leaving B.
    def rnd(r, up_ask, outcome):
        return [
            {"type": "sample", "round": r, "sec_left": 200, "move": 40.0, "spot": 62000,
             "up_ask": up_ask, "dn_ask": round(1.0 - up_ask + 0.01, 2),
             "up_sz": 500, "dn_sz": 500, "ind_conf": 0.5, "ind_dir": "UP"},
            {"type": "result", "round": r, "open": 62000, "close": 62040, "outcome": outcome},
            {"type": "result_pm", "round": r, "outcome": outcome},
        ]
    evs = rnd(1000, 0.10, "DOWN") + rnd(1300, 0.62, "UP")
    base = {r["conf_floor"]: r for r in combo_analysis(evs, min_move_signal=10.0)}
    check("plain combo includes both rounds", base[0.0]["n"] == 2)
    check("plain combo winrate 50% (one poisoned loss)", abs(base[0.0]["winrate"] - 0.5) < 1e-9)
    guarded = {r["conf_floor"]: r for r in combo_analysis(evs, min_move_signal=10.0, min_price=0.40)}
    check("guard drops the cheap book-disagreement round", guarded[0.0]["n"] == 1)
    check("guard keeps the book-agreement winner", abs(guarded[0.0]["winrate"] - 1.0) < 1e-9)


def test_disagreement_anatomy_classifies_by_book():
    from btc_entry_timing import disagreement_analysis
    # Round A: spot says UP, official says DOWN, and the book priced DOWN at
    # 0.90 at the last sample -> reference_error (our feed was wrong).
    # Round B: spot says UP, official says DOWN, book priced UP at 0.95 at the
    # last sample -> photo_finish (genuine late flip).
    # Round C: clean agreement (control, feeds avg_conf_clean).
    evs = [
        {"type": "sample", "round": 100, "sec_left": 195, "move": 40.0, "spot": 62040,
         "up_ask": 0.11, "dn_ask": 0.90, "up_sz": 500, "dn_sz": 500, "ind_conf": 0.30},
        {"type": "sample", "round": 100, "sec_left": 30, "move": 40.0, "spot": 62040,
         "up_ask": 0.05, "dn_ask": 0.97, "up_sz": 500, "dn_sz": 500, "ind_conf": 0.30},
        {"type": "result", "round": 100, "open": 62000, "close": 62040, "outcome": "UP"},
        {"type": "result_pm", "round": 100, "outcome": "DOWN"},
        {"type": "sample", "round": 400, "sec_left": 195, "move": 2.0, "spot": 62002,
         "up_ask": 0.60, "dn_ask": 0.42, "up_sz": 500, "dn_sz": 500, "ind_conf": 0.10},
        {"type": "sample", "round": 400, "sec_left": 30, "move": 1.0, "spot": 62001,
         "up_ask": 0.95, "dn_ask": 0.07, "up_sz": 500, "dn_sz": 500, "ind_conf": 0.10},
        {"type": "result", "round": 400, "open": 62000, "close": 62001, "outcome": "UP"},
        {"type": "result_pm", "round": 400, "outcome": "DOWN"},
        {"type": "sample", "round": 700, "sec_left": 195, "move": 30.0, "spot": 62030,
         "up_ask": 0.65, "dn_ask": 0.37, "up_sz": 500, "dn_sz": 500, "ind_conf": 0.60},
        {"type": "result", "round": 700, "open": 62000, "close": 62030, "outcome": "UP"},
        {"type": "result_pm", "round": 700, "outcome": "UP"},
    ]
    res = disagreement_analysis(evs, min_move_signal=10.0)
    check("two disagreements found", res["n_disagreements"] == 2)
    check("reference error classified (book knew)", res["kinds"]["reference_error"] == 1)
    check("photo finish classified (late flip)", res["kinds"]["photo_finish"] == 1)
    check("clean-round conf used as control", res["avg_conf_clean"] == 0.60)
    check("disagreement conf averaged", abs(res["avg_conf_disagreements"] - 0.20) < 1e-9)
    check("lead-pick intersection counted", res["n_with_lead_pick"] == 1)


def test_calibration_maps_conf_to_winrate():
    # 10 rounds at conf 0.35 where the indicator side wins 8 of 10, and 4
    # rounds at conf 0.55 where it always loses. Only official rounds count.
    evs = []
    for i in range(10):
        r = 1000 + i * 300
        win = i < 8
        evs.append({"type": "sample", "round": r, "sec_left": 195, "move": 5,
                    "up_ask": 0.6, "dn_ask": 0.4, "spot": 62000,
                    "ind_conf": 0.35, "ind_dir": "UP"})
        evs.append({"type": "result", "round": r, "outcome": "UP" if win else "DOWN"})
        evs.append({"type": "result_pm", "round": r, "outcome": "UP" if win else "DOWN"})
    for i in range(4):
        r = 9000 + i * 300
        evs.append({"type": "sample", "round": r, "sec_left": 195, "move": 5,
                    "up_ask": 0.6, "dn_ask": 0.4, "spot": 62000,
                    "ind_conf": 0.55, "ind_dir": "UP"})
        evs.append({"type": "result", "round": r, "outcome": "DOWN"})
        evs.append({"type": "result_pm", "round": r, "outcome": "DOWN"})
    # one unofficial round that must be excluded
    evs.append({"type": "sample", "round": 99999, "sec_left": 195, "move": 5,
                "up_ask": 0.6, "dn_ask": 0.4, "spot": 62000,
                "ind_conf": 0.35, "ind_dir": "UP"})
    evs.append({"type": "result", "round": 99999, "outcome": "UP"})
    rows = {r["band"]: r for r in calibration_analysis(evs)}
    b = rows["0.3-0.4"]
    check("calibration band counts official rounds only", b["n"] == 10)
    check("calibration winrate measured", abs(b["winrate"] - 0.8) < 1e-9)
    check("underconfidence gap positive", b["gap"] > 0.4)
    b2 = rows["0.5-0.6"]
    check("losing band shows negative gap", b2["n"] == 4 and b2["gap"] < 0)


def main():
    test_price_rises_toward_close()
    test_early_less_certain()
    test_efficient_market_negative_edge()
    test_indicator_side_and_min_conf()
    test_confidence_bands_edge_rises()
    test_fade_divergence()
    test_fade_no_setups()
    test_crossing_first_moment()
    test_edge_gate_price_is_hurdle()
    test_official_result_overrides_spot()
    test_min_move_drops_flat_rounds()
    test_auto_min_move_scales_with_price()
    test_session_analysis_buckets_and_lead()
    test_combo_min_price_filters_book_disagreement()
    test_disagreement_anatomy_classifies_by_book()
    test_calibration_maps_conf_to_winrate()
    test_empty_and_missing()
    print("\nAll entry-timing tests passed.")


if __name__ == "__main__":
    main()
