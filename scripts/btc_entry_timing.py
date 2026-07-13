#!/usr/bin/env python3
"""
Find the optimal entry time (and test confidence-based sizing) from a trajectory
log (scripts/btc_record.py).

Two questions this answers:

1. ENTRY TIME. For each offset (seconds before close) it simulates the rule "buy
   the leading side, at that side's ask right then" and reports win rate, average
   price paid, and the resulting edge. Earlier = cheaper but less certain; later
   = surer but pricier. The sweet spot is the offset with the highest edge.
       python3 scripts/btc_entry_timing.py --log out/trajectory.jsonl

   With --side indicator it buys the INDICATOR model's called direction (impulse
   + divergence) instead of the raw move sign, and --min-conf filters to samples
   where the indicator confidence clears a bar. That tests the real thesis:
   "enter earlier, on a strong indicator signal, before the book reprices."
       python3 scripts/btc_entry_timing.py --side indicator --min-conf 0.7

2. CONFIDENCE SIZING. --by-confidence buckets each round (at one decision offset)
   by the indicator's confidence and shows edge per bucket. Confidence-based lot
   sizing only pays if EDGE (not just win rate) RISES with confidence — otherwise
   sizing up on high confidence just bets more on the same (or worse) edge.
       python3 scripts/btc_entry_timing.py --by-confidence

edge = winrate - avg_entry_price. Because breakeven win-rate equals the price, a
POSITIVE edge means that entry is profitable; negative means it loses.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Optional


def load(path: str) -> list[dict[str, Any]]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


DEFAULT_BINS = [(30, 60), (60, 90), (90, 120), (120, 150),
                (150, 180), (180, 210), (210, 240), (240, 270)]

DEFAULT_CONF_BANDS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7),
                      (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def _split(events: list[dict[str, Any]]):
    """Group samples and results per round. result_pm (Polymarket's OFFICIAL
    resolution) overrides the spot-feed outcome when present — the spot label
    can disagree with the Chainlink settle on near-flat rounds, and using it to
    grade a spot-derived signal risks grading our own homework."""
    samples: dict[int, list[dict[str, Any]]] = {}
    results: dict[int, dict[str, Any]] = {}
    official: dict[int, str] = {}
    for e in events:
        t = e.get("type")
        if t == "sample":
            samples.setdefault(e["round"], []).append(e)
        elif t == "result":
            results[e["round"]] = e
        elif t == "result_pm" and e.get("outcome") in ("UP", "DOWN"):
            official[e["round"]] = e["outcome"]
    merged: dict[int, dict[str, Any]] = {}
    for r, e in results.items():
        d = dict(e)
        if r in official:
            d["outcome"] = official[r]
            d["official"] = True
        merged[r] = d
    for r, oc in official.items():
        if r not in merged:
            merged[r] = {"round": r, "outcome": oc, "official": True}
    return samples, merged


VEL_FIELDS = {"vel_5s": "vel_5s", "vel_15s": "vel_15s", "vel_30s": "vel_30s", "vel_60s": "vel_60s"}


def _side_and_price(s: dict[str, Any], side_source: str, min_conf: Optional[float],
                    min_size: float = 0.0):
    """Resolve (side, price) for one sample under the chosen rule, or (None, None)
    to skip it. side_source: move | indicator | vel_5s | vel_15s | vel_30s | vel_60s.
    min_size > 0 requires that many shares at the best ask (samples recorded
    before sizes were logged are excluded), filtering phantom dust quotes."""
    if side_source == "indicator":
        if min_conf is not None:
            c = s.get("ind_conf")
            if c is None or c < min_conf:
                return None, None
        side = s.get("ind_dir")
        if side not in ("UP", "DOWN"):
            return None, None
    elif side_source in VEL_FIELDS:
        v = s.get(VEL_FIELDS[side_source])
        if v is None or v == 0:
            return None, None
        side = "UP" if v > 0 else "DOWN"
    else:
        move = s.get("move", 0.0)
        if move == 0:
            return None, None
        side = "UP" if move > 0 else "DOWN"
    price = s.get("up_ask") if side == "UP" else s.get("dn_ask")
    if price is None or not (0.0 < price < 1.0):
        return None, None
    if min_size > 0.0:
        sz = s.get("up_sz") if side == "UP" else s.get("dn_sz")
        if not isinstance(sz, (int, float)) or sz < min_size:
            return None, None
    return side, float(price)


def taker_fee_frac(price: float, fee_rate: float) -> float:
    """Polymarket taker fee as a fraction of the dollars STAKED.

    Per the fee schedule fee = contracts * rate * p * (1-p); staking $1 buys
    1/price contracts, so fee/stake = rate * (1 - price). Largest near p=0.5,
    negligible near the extremes. Makers pay zero (post limit orders instead)."""
    return fee_rate * (1.0 - price)


def analyze(events: list[dict[str, Any]], bins: list[tuple[int, int]] = DEFAULT_BINS,
            side_source: str = "move", min_conf: Optional[float] = None,
            fee_rate: float = 0.0, min_size: float = 0.0,
            min_move: float = 0.0, official_only: bool = False) -> list[dict[str, Any]]:
    samples, results = _split(events)
    rows = []
    for lo, hi in bins:
        recs: list[tuple[bool, float]] = []
        for r, samps in samples.items():
            res = results.get(r)
            if not res:
                continue
            if official_only and not res.get("official"):
                # Grade ONLY on Polymarket's own resolutions. Spot labels share
                # measurement noise with the spot-derived signal (stale close,
                # feed drift), which inflates leader-follow-through winrates —
                # measured: 14.7% label flips incl. $50+ "moves".
                continue
            if min_move > 0.0:
                # Robustness filter: drop near-flat rounds, where the label is
                # most exposed to feed disagreement (see --label-risk).
                op, cl = res.get("open"), res.get("close")
                if op is None or cl is None or abs(float(cl) - float(op)) < min_move:
                    continue
            outcome = res["outcome"]
            inbin = [s for s in samps if lo <= s["sec_left"] < hi]
            if not inbin:
                continue
            mid = (lo + hi) / 2.0
            s = min(inbin, key=lambda x: abs(x["sec_left"] - mid))
            side, price = _side_and_price(s, side_source, min_conf, min_size)
            if side is None:
                continue
            recs.append((side == outcome, price))
        if recs:
            n = len(recs)
            wins = sum(1 for w, _ in recs if w)
            wr = wins / n
            avg_price = sum(p for _, p in recs) / n
            # EV per $1 staked = (winrate - price)/price; edge = winrate - price.
            ev_pct = (wr - avg_price) / avg_price if avg_price else 0.0
            fee = taker_fee_frac(avg_price, fee_rate)   # per $1 staked (0 if maker/no fee)
            net_ev = ev_pct - fee
            rows.append({"offset": [lo, hi], "n": n, "winrate": round(wr, 4),
                         "avg_price": round(avg_price, 4), "edge": round(wr - avg_price, 4),
                         "ev_pct": round(ev_pct, 4), "fee_pct": round(fee, 4),
                         "net_ev_pct": round(net_ev, 4)})
    return rows


def confidence_bands(events: list[dict[str, Any]], ref_sec: float = 120.0,
                     bands: list[tuple[float, float]] = DEFAULT_CONF_BANDS) -> list[dict[str, Any]]:
    """One decision per round, taken at the sample nearest `ref_sec` seconds-left,
    bucketed by the indicator's confidence. Answers: does EDGE rise with
    confidence (the premise of confidence-based sizing)?"""
    samples, results = _split(events)
    picked: list[tuple[float, bool, float]] = []  # (conf, won, price)
    for r, samps in samples.items():
        res = results.get(r)
        if not res:
            continue
        withconf = [s for s in samps if s.get("ind_conf") is not None and s.get("ind_dir") in ("UP", "DOWN")]
        if not withconf:
            continue
        s = min(withconf, key=lambda x: abs((x.get("sec_left") or 0) - ref_sec))
        side, price = _side_and_price(s, "indicator", None)
        if side is None:
            continue
        picked.append((float(s["ind_conf"]), side == res["outcome"], price))

    rows = []
    for lo, hi in bands:
        band = [(w, p) for c, w, p in picked if lo <= c < hi]
        if not band:
            continue
        n = len(band)
        wr = sum(1 for w, _ in band if w) / n
        avg_price = sum(p for _, p in band) / n
        ev_pct = (wr - avg_price) / avg_price if avg_price else 0.0
        rows.append({"band": [lo, hi], "n": n, "winrate": round(wr, 4),
                     "avg_price": round(avg_price, 4), "edge": round(wr - avg_price, 4),
                     "ev_pct": round(ev_pct, 4)})
    return rows


CROSS_THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]


def crossing_analysis(events: list[dict[str, Any]],
                      thresholds: list[float] = CROSS_THRESHOLDS,
                      fee_rate: float = 0.0) -> list[dict[str, Any]]:
    """FIRST-CROSSING analysis: for each confidence threshold X, find the first
    sample in each round where the indicator confidence reaches X, buy the
    indicator's side at THAT moment's ask, and score it. This is the 'get to
    high confidence faster' hypothesis made measurable — it shows how early each
    conviction level arrives, what the contract costs right then, and whether
    buying at the crossing beats that price."""
    samples: dict[int, list[dict[str, Any]]] = {}
    results: dict[int, dict[str, Any]] = {}
    for e in events:
        t = e.get("type")
        if t == "sample":
            samples.setdefault(e["round"], []).append(e)
        elif t == "result":
            results[e["round"]] = e

    rows = []
    for x in thresholds:
        recs: list[tuple[bool, float, float]] = []   # (won, price, sec_left at crossing)
        for r, samps in samples.items():
            res = results.get(r)
            if not res:
                continue
            # chronological = decreasing sec_left
            for s in sorted(samps, key=lambda q: -(q.get("sec_left") or 0)):
                c = s.get("ind_conf")
                side = s.get("ind_dir")
                if c is None or side not in ("UP", "DOWN") or c < x:
                    continue
                price = s.get("up_ask") if side == "UP" else s.get("dn_ask")
                if isinstance(price, (int, float)) and 0.0 < price < 1.0:
                    recs.append((side == res["outcome"], float(price),
                                 float(s.get("sec_left") or 0)))
                break   # first crossing only
        if recs:
            n = len(recs)
            wr = sum(1 for w, _, _ in recs if w) / n
            avg_price = sum(p for _, p, _ in recs) / n
            avg_sl = sum(sl for _, _, sl in recs) / n
            ev = (wr - avg_price) / avg_price if avg_price else 0.0
            fee = taker_fee_frac(avg_price, fee_rate)
            rows.append({"threshold": x, "n": n, "winrate": round(wr, 4),
                         "avg_price": round(avg_price, 4),
                         "avg_sec_left": round(avg_sl, 1),
                         "edge": round(wr - avg_price, 4),
                         "ev_pct": round(ev, 4), "fee_pct": round(fee, 4),
                         "net_ev_pct": round(ev - fee, 4)})
    return rows


def _print_crossing(rows: list[dict[str, Any]]) -> None:
    print("=" * 74)
    print("CONFIDENCE-CROSSING ANALYSIS  (buy at the FIRST moment conf >= X)")
    print("=" * 74)
    if not rows:
        print("no crossings yet — needs recorder samples with ind_conf; let it run.")
        return
    print(f"  {'conf >=':<9}{'n':<5}{'avg sec left':<14}{'winrate':<10}{'price then':<12}"
          f"{'edge':<9}{'NET EV'}")
    for r in rows:
        print(f"  {r['threshold']:<9.2f}{r['n']:<5}{r['avg_sec_left']:<14.0f}{r['winrate']:<10.1%}"
              f"{r['avg_price']:<12.3f}{r['edge']:<+9.3f}{r['net_ev_pct']:+.1%}")
    best = max(rows, key=lambda r: r["net_ev_pct"])
    print("-" * 74)
    if best["net_ev_pct"] > 0:
        print(f"SWEET SPOT so far: enter when conf first reaches {best['threshold']:.2f} "
              f"(~{best['avg_sec_left']:.0f}s left) -> win {best['winrate']:.0%} "
              f"@ {best['avg_price']:.2f}, NET {best['net_ev_pct']:+.1%}/trade")
        print("Provisional until n is a few hundred; if it holds, set THRESHOLD there.")
    else:
        print("Every crossing level is NEGATIVE -> by the time our confidence arrives,")
        print("the ask has already priced the move (the book is winning the race).")
    print("=" * 74)


def label_risk(events: list[dict[str, Any]],
               bands: tuple = (1.0, 2.0, 5.0, 10.0)) -> dict[str, Any]:
    """How exposed are our win/loss labels to FEED DISAGREEMENT? We settle with
    Binance spot; Polymarket resolves on Chainlink Data Streams. The two can
    only disagree when the round closes nearly flat — |close - open| within a
    few dollars. This reports what fraction of rounds are that marginal."""
    moves = []
    for e in events:
        if e.get("type") == "result" and e.get("open") is not None and e.get("close") is not None:
            moves.append(abs(float(e["close"]) - float(e["open"])))
    out: dict[str, Any] = {"n": len(moves)}
    if moves:
        out["bands"] = [{"within_usd": b,
                         "n": sum(1 for m in moves if m <= b),
                         "pct": round(sum(1 for m in moves if m <= b) / len(moves), 4)}
                        for b in bands]
        out["median_abs_move"] = round(sorted(moves)[len(moves) // 2], 2)
    return out


def _print_label_risk(res: dict[str, Any]) -> None:
    print("=" * 68)
    print("LABEL-RISK  (rounds flat enough for feed disagreement to flip them)")
    print("=" * 68)
    if not res.get("n"):
        print("no settled rounds in the log yet.")
        print("=" * 68)
        return
    print(f"  rounds: {res['n']}   median |close-open|: ${res['median_abs_move']}")
    for b in res["bands"]:
        print(f"  within ${b['within_usd']:<5.0f} {b['n']:<5} ({b['pct']:.1%} of rounds)")
    tight = res["bands"][0]["pct"]
    print("-" * 68)
    if tight >= 0.05:
        print("  A meaningful share of rounds are near-flat -> Binance-vs-Chainlink")
        print("  label noise is material; wiring the Chainlink feed is worth it.")
    else:
        print("  Very few rounds are near-flat -> label noise is minor; the Chainlink")
        print("  feed would sharpen the ruler but won't change any conclusion.")
    print("=" * 68)


EDGE_MARGINS = [0.0, 0.02, 0.05, 0.10]


def edge_gate_analysis(events: list[dict[str, Any]],
                       margins: list[float] = EDGE_MARGINS,
                       fee_rate: float = 0.0) -> list[dict[str, Any]]:
    """EDGE-GATE rule: enter at the FIRST moment confidence >= ask + margin —
    the market price itself is the hurdle. Expensive near-decided rounds demand
    near-certainty; cheap early asks need only modest conviction. This is
    'edge = our probability minus their price' as an entry rule; the table
    shows which margin (if any) is profitable."""
    samples: dict[int, list[dict[str, Any]]] = {}
    results: dict[int, dict[str, Any]] = {}
    for e in events:
        t = e.get("type")
        if t == "sample":
            samples.setdefault(e["round"], []).append(e)
        elif t == "result":
            results[e["round"]] = e

    rows = []
    for m in margins:
        recs: list[tuple[bool, float, float]] = []   # (won, price, sec_left)
        for r, samps in samples.items():
            res = results.get(r)
            if not res:
                continue
            for s in sorted(samps, key=lambda q: -(q.get("sec_left") or 0)):
                c = s.get("ind_conf")
                side = s.get("ind_dir")
                if c is None or side not in ("UP", "DOWN"):
                    continue
                price = s.get("up_ask") if side == "UP" else s.get("dn_ask")
                if not isinstance(price, (int, float)) or not (0.0 < price < 1.0):
                    continue
                if c >= price + m:
                    recs.append((side == res["outcome"], float(price),
                                 float(s.get("sec_left") or 0)))
                    break   # first qualifying moment only
        if recs:
            n = len(recs)
            wr = sum(1 for w, _, _ in recs if w) / n
            avg_price = sum(p for _, p, _ in recs) / n
            avg_sl = sum(sl for _, _, sl in recs) / n
            ev = (wr - avg_price) / avg_price if avg_price else 0.0
            fee = taker_fee_frac(avg_price, fee_rate)
            rows.append({"margin": m, "n": n, "winrate": round(wr, 4),
                         "avg_price": round(avg_price, 4),
                         "avg_sec_left": round(avg_sl, 1),
                         "edge": round(wr - avg_price, 4),
                         "ev_pct": round(ev, 4), "fee_pct": round(fee, 4),
                         "net_ev_pct": round(ev - fee, 4)})
        else:
            rows.append({"margin": m, "n": 0})
    return rows


def _print_edge_gate(rows: list[dict[str, Any]]) -> None:
    print("=" * 74)
    print("EDGE-GATE ANALYSIS  (enter when confidence >= ask + margin)")
    print("=" * 74)
    traded = [r for r in rows if r.get("n")]
    if not traded:
        print("no qualifying moments yet — either not enough recorder data, or our")
        print("confidence never exceeds the ask (the book stays ahead of the model).")
        print("=" * 74)
        return
    print(f"  {'margin':<9}{'n':<5}{'avg sec left':<14}{'winrate':<10}{'price then':<12}"
          f"{'edge':<9}{'NET EV'}")
    for r in rows:
        if not r.get("n"):
            print(f"  {r['margin']:<9.2f}0    (no moment where conf cleared ask+margin)")
            continue
        print(f"  {r['margin']:<9.2f}{r['n']:<5}{r['avg_sec_left']:<14.0f}{r['winrate']:<10.1%}"
              f"{r['avg_price']:<12.3f}{r['edge']:<+9.3f}{r['net_ev_pct']:+.1%}")
    best = max(traded, key=lambda r: r["net_ev_pct"])
    print("-" * 74)
    if best["net_ev_pct"] > 0:
        print(f"BEST margin so far: {best['margin']:.2f}  ->  win {best['winrate']:.0%} "
              f"@ {best['avg_price']:.2f} (~{best['avg_sec_left']:.0f}s left), "
              f"NET {best['net_ev_pct']:+.1%}/trade")
        print(f"Run the trader with it:  RULE=edge EDGE_MARGIN={best['margin']:.2f} scripts/lab.sh")
    else:
        print("Every margin is NEGATIVE -> when our confidence clears the ask, we're")
        print("still wrong too often; the model's conviction isn't beating the book.")
    print("=" * 74)


COMBO_FLOORS = (0.0, 0.3, 0.5, 0.7, 0.8, 0.9)


def combo_analysis(events: list[dict[str, Any]],
                   conf_floors: tuple = COMBO_FLOORS,
                   win_hi: float = 240.0, win_lo: float = 150.0,
                   max_price: float = 0.72, min_move_signal: float = 10.0,
                   min_size: float = 0.0, fee_rate: float = 0.0,
                   official_only: bool = True) -> list[dict[str, Any]]:
    """LEAD + CONFIDENCE combo: the lead setup (leading side, window, cheap ask,
    decisive move) with an added conviction floor. Sweeps the floor from 0 (the
    plain lead rule) upward, graded on OFFICIAL resolutions only — spot labels
    are known to inflate leader-follow-through. First qualifying moment per
    round; one row per floor."""
    samples, results = _split(events)
    rows = []
    for floor in conf_floors:
        recs: list[tuple[bool, float]] = []
        for r, samps in samples.items():
            res = results.get(r)
            if not res:
                continue
            if official_only and not res.get("official"):
                continue
            for s in sorted(samps, key=lambda q: -(q.get("sec_left") or 0)):
                sl = s.get("sec_left") or 0
                if not (win_lo <= sl <= win_hi):
                    continue
                mv = s.get("move", 0.0) or 0.0
                if abs(mv) < min_move_signal:
                    continue
                side = "UP" if mv > 0 else "DOWN"
                price = s.get("up_ask") if side == "UP" else s.get("dn_ask")
                if not isinstance(price, (int, float)) or not (0.0 < price <= max_price):
                    continue
                if min_size > 0:
                    sz = s.get("up_sz") if side == "UP" else s.get("dn_sz")
                    if not isinstance(sz, (int, float)) or sz < min_size:
                        continue
                if floor > 0:
                    c = s.get("ind_conf")
                    if c is None or c < floor:
                        continue
                recs.append((side == res["outcome"], float(price)))
                break
        if recs:
            n = len(recs)
            wr = sum(1 for w, _ in recs if w) / n
            avg_price = sum(p for _, p in recs) / n
            ev = (wr - avg_price) / avg_price if avg_price else 0.0
            fee = taker_fee_frac(avg_price, fee_rate)
            rows.append({"conf_floor": floor, "n": n, "winrate": round(wr, 4),
                         "avg_price": round(avg_price, 4),
                         "edge": round(wr - avg_price, 4),
                         "ev_pct": round(ev, 4), "net_ev_pct": round(ev - fee, 4)})
        else:
            rows.append({"conf_floor": floor, "n": 0})
    return rows


def _print_combo(rows: list[dict[str, Any]]) -> None:
    print("=" * 74)
    print("LEAD + CONFIDENCE COMBO  (lead setup AND conf >= floor; OFFICIAL labels)")
    print("=" * 74)
    traded = [r for r in rows if r.get("n")]
    if not traded:
        print("  no qualifying official-graded rounds yet — let it run.")
        print("=" * 74)
        return
    print(f"  {'conf >=':<9}{'n':<5}{'winrate':<10}{'avg price':<11}{'edge':<9}{'NET EV'}")
    for r in rows:
        if not r.get("n"):
            print(f"  {r['conf_floor']:<9.2f}0    (no qualifying rounds)")
            continue
        print(f"  {r['conf_floor']:<9.2f}{r['n']:<5}{r['winrate']:<10.1%}{r['avg_price']:<11.3f}"
              f"{r['edge']:<+9.3f}{r['net_ev_pct']:+.1%}")
    print("-" * 74)
    print("  Row 0.00 = the plain lead rule (baseline). A floor only matters if it")
    print("  BEATS the baseline with n >= 100 and holds across segments — sweeping")
    print("  floors over one sample WILL find a lucky row; do not trust small n.")
    print("=" * 74)


def fade_analysis(events: list[dict[str, Any]], ref_lo: float = 60.0, ref_hi: float = 150.0,
                  max_price: float = 0.40, min_price: float = 0.03,
                  fee_rate: float = 0.0) -> dict[str, Any]:
    """E3 — trailing-side DIVERGENCE FADE.

    When the RSI divergence (our one OOS-validated leading signal) OPPOSES the
    side BTC is currently leading, buy the CHEAP trailing side the divergence
    points to. The payoff asymmetry flips in our favor: entry 0.05-0.40, so
    losses are small and wins pay 1.5-30x. Decision taken at the sample nearest
    the middle of [ref_lo, ref_hi] seconds-left. Pass: winrate - avg_price > +3%
    net of fee with a few hundred rounds.
    """
    samples, results = _split(events)
    recs: list[tuple[bool, float]] = []       # fade trades: (won, price)
    n_opposing = 0
    ref_mid = (ref_lo + ref_hi) / 2.0
    for r, samps in samples.items():
        res = results.get(r)
        if not res:
            continue
        inwin = [s for s in samps if ref_lo <= (s.get("sec_left") or 0) <= ref_hi
                 and s.get("ind_div") not in (None, 0)]
        if not inwin:
            continue
        s = min(inwin, key=lambda x: abs((x.get("sec_left") or 0) - ref_mid))
        div = s.get("ind_div")                 # +1 bullish -> UP, -1 bearish -> DOWN
        move = s.get("move", 0.0)
        fade_side = "UP" if div > 0 else "DOWN"
        lead_side = "UP" if move > 0 else "DOWN" if move < 0 else None
        if lead_side is None or fade_side == lead_side:
            continue                           # divergence must OPPOSE the leading side
        n_opposing += 1
        price = s.get("up_ask") if fade_side == "UP" else s.get("dn_ask")
        if not isinstance(price, (int, float)) or not (min_price <= price <= max_price):
            continue                           # only the CHEAP trailing side
        recs.append((fade_side == res["outcome"], float(price)))

    n = len(recs)
    out: dict[str, Any] = {"n_opposing": n_opposing, "n_traded": n,
                           "window": [ref_lo, ref_hi], "price_band": [min_price, max_price]}
    if n:
        wr = sum(1 for w, _ in recs if w) / n
        avg_price = sum(p for _, p in recs) / n
        fee = taker_fee_frac(avg_price, fee_rate)
        ev = (wr - avg_price) / avg_price if avg_price else 0.0
        out.update({"winrate": round(wr, 4), "avg_price": round(avg_price, 4),
                    "edge": round(wr - avg_price, 4), "ev_pct": round(ev, 4),
                    "fee_pct": round(fee, 4), "net_ev_pct": round(ev - fee, 4)})
    return out


def _print_fade(res: dict[str, Any]) -> None:
    print("=" * 68)
    print("TRAILING-SIDE DIVERGENCE FADE  (E3)")
    print("=" * 68)
    print(f"  window {res['window'][0]:.0f}-{res['window'][1]:.0f}s left, "
          f"price band {res['price_band'][0]:.2f}-{res['price_band'][1]:.2f}")
    print(f"  rounds where divergence OPPOSED the leading side: {res['n_opposing']}")
    print(f"  of those, trailing ask inside the cheap band:     {res['n_traded']}")
    if res.get("winrate") is None:
        print("  not enough fade setups yet — divergence-vs-move is rare; keep recording.")
    else:
        print(f"  winrate {res['winrate']:.1%}   avg price {res['avg_price']:.3f}   "
              f"edge {res['edge']:+.3f}")
        print(f"  EV/trade {res['ev_pct']:+.1%}   fee {res['fee_pct']:.2%}   "
              f"NET {res['net_ev_pct']:+.1%}")
        print("-" * 68)
        if res["net_ev_pct"] > 0.03:
            print("  PASS so far: edge clears the +3% bar — keep gathering to n>=300.")
        elif res["net_ev_pct"] > 0:
            print("  Marginally positive — below the +3% bar; needs more data.")
        else:
            print("  NEGATIVE — the fade loses too; divergence doesn't beat the cheap ask.")
    print("=" * 68)


def label_check(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Direct measurement of label noise: for rounds that have BOTH our
    spot-feed result and Polymarket's official resolution (result_pm), how
    often do they disagree — and how concentrated is disagreement in
    near-flat rounds?"""
    spot: dict[int, dict[str, Any]] = {}
    pm: dict[int, str] = {}
    for e in events:
        t = e.get("type")
        if t == "result":
            spot[e["round"]] = e
        elif t == "result_pm" and e.get("outcome") in ("UP", "DOWN"):
            pm[e["round"]] = e["outcome"]
    both = [r for r in spot if r in pm]
    dis = []
    for r in both:
        if spot[r].get("outcome") != pm[r]:
            op, cl = spot[r].get("open"), spot[r].get("close")
            mv = abs(float(cl) - float(op)) if op is not None and cl is not None else None
            dis.append({"round": r, "spot": spot[r].get("outcome"), "official": pm[r],
                        "abs_move": round(mv, 2) if mv is not None else None})
    return {"n_official": len(pm), "n_both": len(both), "n_disagree": len(dis),
            "pct_disagree": round(len(dis) / len(both), 4) if both else None,
            "disagreements": dis[:20]}


def _print_label_check(res: dict[str, Any]) -> None:
    print("=" * 68)
    print("LABEL AGREEMENT  (our spot settle vs Polymarket's OFFICIAL settle)")
    print("=" * 68)
    if not res["n_both"]:
        print("  no rounds have both labels yet — result_pm collection just started;")
        print("  let the recorder run.")
        print("=" * 68)
        return
    print(f"  official resolutions recorded: {res['n_official']}   comparable rounds: {res['n_both']}")
    pct = res["pct_disagree"]
    print(f"  disagreements: {res['n_disagree']} ({pct:.1%})")
    for d in res["disagreements"]:
        print(f"    round {d['round']}: spot={d['spot']} official={d['official']} |move|=${d['abs_move']}")
    print("-" * 68)
    if pct == 0:
        print("  Labels agree perfectly so far — spot-labeled history is trustworthy.")
    elif pct <= 0.03:
        print("  Small disagreement rate, concentrated in flat rounds — spot-labeled")
        print("  results are usable; official labels remove the residual noise.")
    else:
        print("  MATERIAL disagreement — treat spot-labeled edges as suspect and rerun")
        print("  the tables once enough official-labeled rounds accumulate.")
    print("=" * 68)


def _print_offsets(rows, side_source, min_conf, fee_rate):
    tag = f"side={side_source}" + (f" min-conf={min_conf}" if min_conf is not None else "")
    fee_on = fee_rate > 0
    if fee_on:
        tag += f" taker-fee-rate={fee_rate}"
    print("=" * 74)
    print(f"ENTRY-TIME ANALYSIS  ({tag})")
    print("=" * 74)
    if not rows:
        print("no complete rounds match — let scripts/btc_record.py run longer"
              + (" (or lower --min-conf)." if min_conf is not None else "."))
        return
    if fee_on:
        print(f"  {'sec left':<12}{'n':<6}{'winrate':<10}{'avg price':<11}"
              f"{'grossEV':<10}{'fee':<9}{'NET EV/trade'}")
        for r in rows:
            print(f"  {str(r['offset']):<12}{r['n']:<6}{r['winrate']:<10.1%}{r['avg_price']:<11.3f}"
                  f"{r['ev_pct']:<+10.1%}{r['fee_pct']:<9.1%}{r['net_ev_pct']:+.1%}")
        key, label = "net_ev_pct", "NET EV (after taker fee)"
    else:
        print(f"  {'sec left':<12}{'n':<6}{'winrate':<10}{'avg price':<11}{'edge':<9}{'EV/trade'}")
        for r in rows:
            print(f"  {str(r['offset']):<12}{r['n']:<6}{r['winrate']:<10.1%}{r['avg_price']:<11.3f}"
                  f"{r['edge']:<+9.3f}{r['ev_pct']:+.1%}")
        key, label = "ev_pct", "EV"
    best = max(rows, key=lambda r: r[key])
    print("-" * 74)
    if best[key] > 0:
        print(f"BEST entry: {best['offset'][0]}-{best['offset'][1]}s left  ->  "
              f"winrate {best['winrate']:.0%} @ price {best['avg_price']:.2f}  "
              f"{label} {best[key]:+.1%}/trade")
        print("Earlier = cheaper but less certain; this offset balances the two best.")
    else:
        neg = "NET (after fee) " if fee_on else ""
        print(f"EVERY entry time has NEGATIVE {neg}EV -> the market is efficiently priced;")
        print("no entry timing makes this taker rule profitable on this data.")
        if fee_on:
            print("(The taker fee only widens the loss — the maker side is the structural fix.)")
    print("=" * 74)


def _print_bands(rows, ref_sec):
    print("=" * 68)
    print(f"CONFIDENCE-BAND ANALYSIS  (one decision/round at ~{ref_sec:.0f}s left)")
    print("=" * 68)
    if not rows:
        print("no rounds with an indicator confidence yet — the recorder logs")
        print("ind_conf; let it run, or check the provider fetched klines.")
        return
    print(f"  {'confidence':<14}{'n':<6}{'winrate':<10}{'avg price':<11}{'edge':<9}{'EV/trade'}")
    for r in rows:
        lo, hi = r["band"]
        label = f"{lo:.2f}-{min(hi, 1.0):.2f}"
        print(f"  {label:<14}{r['n']:<6}{r['winrate']:<10.1%}{r['avg_price']:<11.3f}"
              f"{r['edge']:<+9.3f}{r['ev_pct']:+.1%}")
    print("-" * 68)
    # Does edge rise with confidence? Compare the highest-confidence band to the lowest.
    lowest, highest = rows[0], rows[-1]
    if highest["edge"] > lowest["edge"] and highest["edge"] > 0:
        print(f"Edge RISES with confidence ({lowest['edge']:+.3f} -> {highest['edge']:+.3f}) and the")
        print("top band is POSITIVE -> confidence-based sizing is justified: bet more there.")
    elif highest["edge"] > lowest["edge"]:
        print(f"Edge rises with confidence ({lowest['edge']:+.3f} -> {highest['edge']:+.3f}) but the top")
        print("band is still NEGATIVE -> higher confidence loses less, not profits; don't size up.")
    else:
        print(f"Edge does NOT rise with confidence ({lowest['edge']:+.3f} -> {highest['edge']:+.3f})")
        print("-> confidence is not tracking edge; confidence-based sizing would not help.")
    print("=" * 68)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Optimal entry-time + confidence-band analysis from a trajectory log")
    ap.add_argument("--log", default="out/trajectory.jsonl")
    ap.add_argument("--side", choices=["move", "indicator", "vel_5s", "vel_15s", "vel_30s", "vel_60s"],
                    default="move",
                    help="Which direction to buy: raw round move (default), the indicator model's call, "
                         "or the sign of a sub-minute velocity (vel_5s..vel_60s) to compare fast timeframes")
    ap.add_argument("--min-conf", type=float, default=None,
                    help="With --side indicator: only take samples with indicator confidence >= this")
    ap.add_argument("--min-size", type=float, default=0.0,
                    help="Require this many shares at the best ask (filters phantom dust quotes; "
                         "needs samples recorded with up_sz/dn_sz)")
    ap.add_argument("--min-move", type=float, default=0.0,
                    help="Drop rounds settling within this many dollars of open (label-noise robustness check)")
    ap.add_argument("--official-only", action="store_true",
                    help="Grade ONLY rounds with an official Polymarket resolution (result_pm) — the truth table")
    ap.add_argument("--combo", action="store_true",
                    help="Lead setup + confidence-floor sweep, official labels only")
    ap.add_argument("--split-halves", action="store_true",
                    help="With --combo: run separately on the FIRST and SECOND half of official-graded "
                         "rounds (by time) — a same-log stand-in for the cross-segment check")
    ap.add_argument("--by-confidence", action="store_true",
                    help="Instead of the entry-time table, show edge bucketed by indicator confidence")
    ap.add_argument("--fade", action="store_true",
                    help="E3: trailing-side divergence fade — buy the CHEAP side when divergence opposes the move")
    ap.add_argument("--crossing", action="store_true",
                    help="First-crossing table: buy at the first moment confidence reaches each threshold")
    ap.add_argument("--edge-gate", action="store_true",
                    help="Edge-gate table: buy at the first moment confidence >= ask + margin (price as hurdle)")
    ap.add_argument("--label-risk", action="store_true",
                    help="Fraction of near-flat rounds where Binance-vs-Chainlink settle labels could differ")
    ap.add_argument("--label-check", action="store_true",
                    help="Measured disagreement between our spot settle and the official Polymarket settle")
    ap.add_argument("--conf-offset", type=float, default=120.0,
                    help="Seconds-left decision point for --by-confidence (default 120)")
    ap.add_argument("--fee-rate", type=float, default=0.0,
                    help="Polymarket TAKER fee rate (fee=contracts*rate*p*(1-p)). 0=off. "
                         "Read the REAL rate from the CLOB market data, don't hardcode. Makers pay 0.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    events = load(args.log)
    if args.combo:
        if args.split_halves:
            # Independent halves of the official-graded rounds, by time. A real
            # edge should be positive in BOTH; a lucky row usually isn't.
            official_rounds = sorted({e["round"] for e in events
                                      if e.get("type") == "result_pm"
                                      and e.get("outcome") in ("UP", "DOWN")})
            if len(official_rounds) < 20:
                print("not enough official-graded rounds to split; let it run.")
                return 0
            cutoff = official_rounds[len(official_rounds) // 2]
            first = [e for e in events if e.get("round") is not None and e["round"] < cutoff]
            second = [e for e in events if e.get("round") is not None and e["round"] >= cutoff]
            for label, chunk in (("FIRST HALF (older)", first), ("SECOND HALF (newer)", second)):
                print(f"\n########## {label} ##########")
                _print_combo(combo_analysis(chunk, min_size=args.min_size, fee_rate=args.fee_rate))
            return 0
        rows = combo_analysis(events, min_size=args.min_size, fee_rate=args.fee_rate)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        _print_combo(rows)
        return 0
    if args.label_check:
        res = label_check(events)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        _print_label_check(res)
        return 0
    if args.label_risk:
        res = label_risk(events)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        _print_label_risk(res)
        return 0
    if args.edge_gate:
        rows = edge_gate_analysis(events, fee_rate=args.fee_rate)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        _print_edge_gate(rows)
        return 0
    if args.crossing:
        rows = crossing_analysis(events, fee_rate=args.fee_rate)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        _print_crossing(rows)
        return 0
    if args.fade:
        res = fade_analysis(events, fee_rate=args.fee_rate)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        _print_fade(res)
        return 0
    if args.by_confidence:
        rows = confidence_bands(events, ref_sec=args.conf_offset)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        _print_bands(rows, args.conf_offset)
        return 0

    rows = analyze(events, side_source=args.side, min_conf=args.min_conf,
                   fee_rate=args.fee_rate, min_size=args.min_size, min_move=args.min_move,
                   official_only=args.official_only)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    _print_offsets(rows, args.side, args.min_conf, args.fee_rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
