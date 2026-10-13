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


COMBO_FLOORS = (0.0, 0.3, 0.4, 0.5, 0.7, 0.8, 0.9)

# The BTC 'decisive move' rule ($10 at ~$62k spot) expressed scale-free, so the
# same setup means the same thing on ETH/SOL/XRP: 0.016% of the asset's price.
MOVE_FRAC = 1.6e-4

# The lead-rule definition the combo/session/exit/disagreement/executor
# analyses grade. Defaults match the ORIGINAL rule; overridable via
# --lead-hi/--lead-lo/--lead-max-price so the tables grade the rule the
# trader is ACTUALLY running (per-asset windows/caps in lab.sh) — otherwise
# executor-vs-table compares fills against a stale definition and BOTH is
# empty by construction.
LEAD_WIN_HI = 240.0
LEAD_WIN_LO = 150.0
LEAD_MAX_PRICE = 0.72


def auto_min_move(events: list[dict[str, Any]]) -> float:
    """Auto-scale the lead rule's dollar move threshold from the data itself:
    MOVE_FRAC of the median recorded spot. Falls back to BTC's $10."""
    spots = sorted(e["spot"] for e in events
                   if e.get("type") == "sample" and isinstance(e.get("spot"), (int, float)))
    if not spots:
        return 10.0
    return round(spots[len(spots) // 2] * MOVE_FRAC, 6)


def _first_lead_pick(samps: list[dict[str, Any]], win_hi: Optional[float] = None,
                     win_lo: Optional[float] = None,
                     max_price: Optional[float] = None, min_move_signal: float = 10.0,
                     min_size: float = 0.0, floor: float = 0.0,
                     min_price: float = 0.0) -> Optional[tuple]:
    """First sample in the round satisfying the lead setup (leading side inside
    the window, decisive move, executable ask in [min_price, max_price], optional
    conviction floor). Returns (side, price) or None — shared by the combo and
    session analyses so they grade the identical rule.

    min_price is the 'book-agreement' guard: if the side WE think is leading is
    priced below min_price, the MARKET strongly disagrees (it is pricing the
    other side as the winner) — which is exactly the case where our round-open
    reference is wrong or we are being adversely selected. Requiring the leading
    side to cost at least min_price trusts the book over our move sign."""
    if win_hi is None:
        win_hi = LEAD_WIN_HI
    if win_lo is None:
        win_lo = LEAD_WIN_LO
    if max_price is None:
        max_price = LEAD_MAX_PRICE
    for s in sorted(samps, key=lambda q: -(q.get("sec_left") or 0)):
        sl = s.get("sec_left") or 0
        if not (win_lo <= sl <= win_hi):
            continue
        mv = s.get("move", 0.0) or 0.0
        if abs(mv) < min_move_signal:
            continue
        side = "UP" if mv > 0 else "DOWN"
        price = s.get("up_ask") if side == "UP" else s.get("dn_ask")
        if not isinstance(price, (int, float)) or not (min_price < price <= max_price):
            continue
        if min_size > 0:
            sz = s.get("up_sz") if side == "UP" else s.get("dn_sz")
            if not isinstance(sz, (int, float)) or sz < min_size:
                continue
        if floor > 0:
            c = s.get("ind_conf")
            if c is None or c < floor:
                continue
        return side, float(price), float(sl)
    return None


def combo_analysis(events: list[dict[str, Any]],
                   conf_floors: tuple = COMBO_FLOORS,
                   win_hi: Optional[float] = None, win_lo: Optional[float] = None,
                   max_price: Optional[float] = None, min_move_signal: float = 10.0,
                   min_size: float = 0.0, fee_rate: float = 0.0,
                   official_only: bool = True, min_price: float = 0.0) -> list[dict[str, Any]]:
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
            pick = _first_lead_pick(samps, win_hi=win_hi, win_lo=win_lo, max_price=max_price,
                                    min_move_signal=min_move_signal, min_size=min_size,
                                    floor=floor, min_price=min_price)
            if pick is not None:
                recs.append((pick[0] == res["outcome"], pick[1]))
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
    print(f"LEAD + CONFIDENCE COMBO  (lead setup {LEAD_WIN_LO:g}-{LEAD_WIN_HI:g}s ask <= "
          f"{LEAD_MAX_PRICE:g} AND conf >= floor; OFFICIAL labels)")
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


def exit_policy_analysis(events: list[dict[str, Any]], min_move_signal: float = 10.0,
                         min_size: float = 0.0, floor: float = 0.0,
                         lock_bid: float = 0.90, stop_bid: float = 0.30,
                         official_only: bool = True) -> dict[str, Any]:
    """Early-exit study on recorded BIDS: for every historical lead-rule entry,
    compare four exit policies per $1 share (all graded on official outcomes):

      hold  — settle at $1 / $0 (the current rule)
      sell  — always sell at the bid of the LAST recorded sample (~30s left)
      lock  — sell at the last bid only if it >= lock_bid (cash the near-certain
              win, your 'guarantee the win' proposal); otherwise settle
      stop  — sell at the FIRST moment the bid <= stop_bid (cut the loser);
              otherwise settle
      both  — stop-loss first, else lock at the end, else settle
      flat  — sell at the last bid ONLY when the round is finishing near-flat
              (|move| at the last sample below the decisive threshold): the
              exact rounds where photo-finish/reference-error label risk lives
              and holding is a coin flip anyway; decisive rounds are held

    If late prices are calibrated, no early exit can BEAT hold on EV (you pay
    the spread) — the question this answers is what the insurance costs, and
    whether photo-finish/reference-error rounds make it cheap or even free."""
    samples, results = _split(events)
    recs: list[dict[str, Any]] = []
    for r, samps in samples.items():
        res = results.get(r)
        if not res or (official_only and not res.get("official")):
            continue
        if res.get("outcome") not in ("UP", "DOWN"):
            continue
        pick = _first_lead_pick(samps, min_move_signal=min_move_signal,
                                min_size=min_size, floor=floor)
        if pick is None:
            continue
        side, entry, entry_sl = pick
        win = side == res["outcome"]
        hold = 1.0 if win else 0.0
        bidkey = "up_bid" if side == "UP" else "dn_bid"
        later = sorted((s for s in samps if (s.get("sec_left") or 0) < entry_sl),
                       key=lambda s: -(s.get("sec_left") or 0))
        final = later[-1] if later else None
        fbid = final.get(bidkey) if final else None
        fbid = float(fbid) if isinstance(fbid, (int, float)) else None
        sell = fbid if fbid is not None else hold      # unsellable -> settle
        lock = fbid if (fbid is not None and fbid >= lock_bid) else hold
        stop = hold
        stopped = False
        for s in later:
            b = s.get(bidkey)
            if isinstance(b, (int, float)) and b <= stop_bid:
                stop = float(b)
                stopped = True
                break
        both = stop if stopped else lock
        fmv = final.get("move") if final else None
        finishing_flat = isinstance(fmv, (int, float)) and abs(fmv) < min_move_signal
        flat = sell if (finishing_flat and fbid is not None) else hold
        recs.append({"entry": entry, "win": win, "hold": hold, "sell": sell,
                     "lock": lock, "stop": stop, "both": both, "flat": flat,
                     "was_flat": finishing_flat,
                     "final_sl": final.get("sec_left") if final else None})
    n = len(recs)
    out: dict[str, Any] = {"n": n, "floor": floor, "lock_bid": lock_bid, "stop_bid": stop_bid}
    if not n:
        return out
    avg_entry = sum(x["entry"] for x in recs) / n
    out["avg_entry"] = round(avg_entry, 4)
    fsl = [x["final_sl"] for x in recs if x["final_sl"] is not None]
    out["avg_exit_sec_left"] = round(sum(fsl) / len(fsl), 1) if fsl else None
    out["n_finishing_flat"] = sum(1 for x in recs if x["was_flat"])
    for pol in ("hold", "sell", "lock", "stop", "both", "flat"):
        proceeds = sum(x[pol] for x in recs) / n
        out[pol] = {
            "avg_proceeds": round(proceeds, 4),
            "ev_pct": round((proceeds - avg_entry) / avg_entry, 4),
            # a 'bust' loses most of the stake: proceeds under half the entry
            "busts": sum(1 for x in recs if x[pol] < 0.5 * x["entry"]),
        }
    return out


def _print_exit(res: dict[str, Any]) -> None:
    print("=" * 74)
    print(f"EXIT-POLICY STUDY  (lead entries, conf >= {res.get('floor', 0):g}; "
          f"sell at recorded BIDS; official labels)")
    print("=" * 74)
    if not res.get("n"):
        print("  no qualifying entries with bid data yet — needs samples recorded")
        print("  after bid capture was enabled.")
        print("=" * 74)
        return
    exit_sl = res.get("avg_exit_sec_left")
    print(f"  entries: {res['n']}   avg entry: {res['avg_entry']:.3f}   "
          f"last sellable sample: ~{exit_sl:.0f}s left" if exit_sl else f"  entries: {res['n']}")
    print(f"  {'policy':<31}{'avg proceeds':<14}{'EV/trade':<11}{'busts'}")
    labels = {
        "hold": "hold to settlement",
        "sell": "sell at ~30s always",
        "lock": f"lock: sell if bid >= {res['lock_bid']:.2f}",
        "stop": f"stop: sell if bid <= {res['stop_bid']:.2f}",
        "both": "stop-loss + lock",
        "flat": "flat-guard: sell flat finishes",
    }
    for pol in ("hold", "sell", "lock", "stop", "both", "flat"):
        p = res[pol]
        print(f"  {labels[pol]:<31}{p['avg_proceeds']:<14.3f}{p['ev_pct']:<+11.1%}{p['busts']}")
    print("-" * 74)
    nf = res.get("n_finishing_flat", 0)
    print(f"  flat-guard sells only rounds finishing near-flat ({nf} of {res['n']} here) —")
    print("  the label-risk rounds where holding is a coin flip anyway.")
    print("  busts = trades losing over half the stake. If an exit policy has FAR")
    print("  fewer busts for <= ~1-2% EV, that is cheap insurance (and a maker exit")
    print("  would pay no fee); if EV drops more, the market charges too much for it.")
    print("=" * 74)


def disagreement_analysis(events: list[dict[str, Any]], at_sec: float = 195.0,
                          min_move_signal: float = 10.0,
                          min_size: float = 0.0) -> dict[str, Any]:
    """Anatomy of label DISAGREEMENTS (our spot settle vs the official
    resolution). Each one is classified by what the ORDER BOOK said at the
    round's last recorded sample:

      reference_error — the book's favorite matched the OFFICIAL outcome:
                        the market knew all along; OUR open/close reference
                        was wrong (fixable: better feed / faster close).
      photo_finish    — the book's favorite matched OUR spot label: the round
                        genuinely flipped at the very end (irreducible noise).

    Also compares indicator confidence on disagreement rounds vs clean rounds
    (are disagreements concentrated where the bot is unsure anyway?) and counts
    how many disagreements intersected an actual lead-setup pick (real trades
    at risk, not just labels)."""
    spot_res: dict[int, dict[str, Any]] = {}
    official: dict[int, str] = {}
    samples: dict[int, list[dict[str, Any]]] = {}
    for e in events:
        t = e.get("type")
        if t == "sample":
            samples.setdefault(e["round"], []).append(e)
        elif t == "result":
            spot_res[e["round"]] = e
        elif t == "result_pm" and e.get("outcome") in ("UP", "DOWN"):
            official[e["round"]] = e["outcome"]

    def conf_at(samps: list[dict[str, Any]]) -> Optional[float]:
        best = None
        for s in samps:
            if s.get("ind_conf") is None:
                continue
            if best is None or abs((s.get("sec_left") or 0) - at_sec) < abs((best.get("sec_left") or 0) - at_sec):
                best = s
        return float(best["ind_conf"]) if best else None

    rows = []
    clean_confs: list[float] = []
    for r, sres in spot_res.items():
        if r not in official or sres.get("outcome") not in ("UP", "DOWN"):
            continue
        samps = samples.get(r, [])
        c = conf_at(samps)
        if sres["outcome"] == official[r]:
            if c is not None:
                clean_confs.append(c)
            continue
        # a disagreement — classify it by the book's last word
        row: dict[str, Any] = {"round": r, "spot": sres["outcome"], "official": official[r],
                               "conf": c}
        if isinstance(sres.get("open"), (int, float)) and isinstance(sres.get("close"), (int, float)):
            row["abs_move"] = round(abs(sres["close"] - sres["open"]), 6)
        last = min(samps, key=lambda s: s.get("sec_left") or 1e9) if samps else None
        if last and isinstance(last.get("up_ask"), (int, float)) and isinstance(last.get("dn_ask"), (int, float)) \
                and last["up_ask"] != last["dn_ask"]:
            fav = "UP" if last["up_ask"] > last["dn_ask"] else "DOWN"
            row["book_favorite"] = fav
            row["kind"] = "reference_error" if fav == official[r] else "photo_finish"
        else:
            row["kind"] = "no_book_data"
        pick = _first_lead_pick(samps, min_move_signal=min_move_signal, min_size=min_size)
        if pick is not None:
            row["lead_pick"] = pick[0]
            row["lead_pick_won_officially"] = pick[0] == official[r]
        rows.append(row)

    dis_confs = [r["conf"] for r in rows if r.get("conf") is not None]
    n_compare = len(clean_confs) + len(dis_confs)
    return {
        "n_compared": n_compare,
        "n_disagreements": len(rows),
        "rate": round(len(rows) / n_compare, 4) if n_compare else None,
        "kinds": {k: sum(1 for r in rows if r["kind"] == k)
                  for k in ("reference_error", "photo_finish", "no_book_data")},
        "avg_conf_disagreements": round(sum(dis_confs) / len(dis_confs), 3) if dis_confs else None,
        "avg_conf_clean": round(sum(clean_confs) / len(clean_confs), 3) if clean_confs else None,
        "n_with_lead_pick": sum(1 for r in rows if "lead_pick" in r),
        "lead_picks_officially_right": sum(1 for r in rows if r.get("lead_pick_won_officially")),
        "rows": sorted(rows, key=lambda r: -(r.get("abs_move") or 0)),
    }


def _print_disagreements(res: dict[str, Any]) -> None:
    print("=" * 74)
    print("DISAGREEMENT ANATOMY  (our spot label vs official; classified by the book)")
    print("=" * 74)
    if not res["n_disagreements"]:
        print("  no disagreements among compared rounds — labels are clean so far.")
        print("=" * 74)
        return
    k = res["kinds"]
    print(f"  compared rounds: {res['n_compared']}   disagreements: {res['n_disagreements']} "
          f"({res['rate']:.1%})")
    print(f"  reference errors (book knew; OUR feed was wrong):   {k['reference_error']}")
    print(f"  photo finishes  (book agreed with us; late flip):   {k['photo_finish']}")
    if k["no_book_data"]:
        print(f"  unclassifiable (no book data near the close):       {k['no_book_data']}")
    ac, ad = res.get("avg_conf_clean"), res.get("avg_conf_disagreements")
    if ac is not None and ad is not None:
        rel = "LOWER than" if ad < ac else "similar to" if abs(ad - ac) < 0.05 else "HIGHER than"
        print(f"  avg confidence: {ad:.2f} on disagreement rounds vs {ac:.2f} on clean rounds "
              f"({rel} clean)")
    print(f"  disagreements that intersected a LEAD-SETUP pick: {res['n_with_lead_pick']}"
          + (f" (officially right: {res['lead_picks_officially_right']})" if res["n_with_lead_pick"] else ""))
    print("-" * 74)
    print(f"  {'round':<13}{'spot':<6}{'offcl':<7}{'|move|':<10}{'conf':<7}{'book fav':<10}{'kind'}")
    for r in res["rows"][:15]:
        mv = f"{r.get('abs_move'):.4g}" if r.get("abs_move") is not None else "-"
        cf = f"{r['conf']:.2f}" if r.get("conf") is not None else "-"
        print(f"  {r['round']:<13}{r['spot']:<6}{r['official']:<7}{mv:<10}{cf:<7}"
              f"{r.get('book_favorite', '-'):<10}{r['kind']}")
    print("-" * 74)
    print("  reference errors are FIXABLE: a settle reference closer to the venue's")
    print("  oracle (median of feeds, faster close sampling, or Chainlink itself)")
    print("  removes them. Photo finishes are not — no feed resolves a tie early.")
    print("=" * 74)


CALIB_BANDS = [(0.0, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01)]


def calibration_analysis(events: list[dict[str, Any]], at_sec: float = 195.0,
                         official_only: bool = True) -> list[dict[str, Any]]:
    """Confidence CALIBRATION (official labels): per confidence band, how often
    did the indicator's side actually win? gap = winrate - avg_conf. A big
    positive gap means the score under-states its own accuracy (it RANKS well
    but is not a probability); the fix is a calibration lookup fitted on one
    segment and verified on another — never a live re-tune on the same data.
    One reading per round, at the sample nearest `at_sec` seconds left (mid
    lead window)."""
    samples, results = _split(events)
    recs: list[tuple[float, bool]] = []
    for r, samps in samples.items():
        res = results.get(r)
        if not res or (official_only and not res.get("official")):
            continue
        if res.get("outcome") not in ("UP", "DOWN"):
            continue
        best = None
        for s in samps:
            if s.get("ind_conf") is None or s.get("ind_dir") not in ("UP", "DOWN"):
                continue
            if best is None or abs((s.get("sec_left") or 0) - at_sec) < abs((best.get("sec_left") or 0) - at_sec):
                best = s
        if best is None:
            continue
        recs.append((float(best["ind_conf"]), best["ind_dir"] == res["outcome"]))
    rows = []
    for lo, hi in CALIB_BANDS:
        xs = [(c, w) for c, w in recs if lo <= c < hi]
        row: dict[str, Any] = {"band": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(xs)}
        if xs:
            n = len(xs)
            wr = sum(1 for _, w in xs if w) / n
            ac = sum(c for c, _ in xs) / n
            row.update({"avg_conf": round(ac, 3), "winrate": round(wr, 4),
                        "gap": round(wr - ac, 4)})
        rows.append(row)
    return rows


def _print_calibration(rows: list[dict[str, Any]]) -> None:
    print("=" * 70)
    print("CONFIDENCE CALIBRATION  (indicator side vs OFFICIAL outcome, ~195s left)")
    print("=" * 70)
    if not any(r["n"] for r in rows):
        print("  no official-graded rounds with indicator readings yet.")
        print("=" * 70)
        return
    print(f"  {'conf band':<12}{'n':<6}{'avg conf':<10}{'winrate':<10}{'gap'}")
    for r in rows:
        if not r["n"]:
            print(f"  {r['band']:<12}0")
            continue
        print(f"  {r['band']:<12}{r['n']:<6}{r['avg_conf']:<10.3f}{r['winrate']:<10.1%}{r['gap']:+.3f}")
    print("-" * 70)
    print("  Read: winrate should RISE with the band (score ranks well). 'gap' is")
    print("  winrate minus avg conf: large positive = the score under-states its")
    print("  accuracy — a probability it is not. Any re-mapping must be fitted on")
    print("  an archived segment and VERIFIED on a fresh one, never tuned live.")
    print("=" * 70)


# 8h UTC blocks approximating the three trading sessions. Each round lands in
# exactly one, plus a weekday/weekend bucket.
SESSIONS = (("Asia   00-08 UTC", 0, 8),
            ("Europe 08-16 UTC", 8, 16),
            ("US     16-24 UTC", 16, 24))


def session_analysis(events: list[dict[str, Any]], min_size: float = 0.0,
                     fee_rate: float = 0.0, min_move_signal: float = 10.0,
                     lead_min_conf: float = 0.0, official_only: bool = True) -> list[dict[str, Any]]:
    """Time-of-day / weekday regimes (official labels only). Two questions per
    bucket: (a) DRIFT — do rounds resolve UP more or less than 50%, i.e. does
    the session itself lean a direction; (b) does the LEAD SETUP perform
    differently by session (where does the edge live, if anywhere)."""
    import datetime

    samples, results = _split(events)

    def buckets_of(rs: int) -> list[str]:
        h = (rs // 3600) % 24
        sess = next(name for name, lo, hi in SESSIONS if lo <= h < hi)
        wd = datetime.datetime.fromtimestamp(rs, datetime.timezone.utc).weekday()
        return [sess, "weekday (Mon-Fri)" if wd < 5 else "weekend (Sat-Sun)"]

    order = [s[0] for s in SESSIONS] + ["weekday (Mon-Fri)", "weekend (Sat-Sun)"]
    drift = {k: {"n": 0, "up": 0, "mv_sum": 0.0, "mv_n": 0} for k in order}
    lead = {k: [] for k in order}
    for r, res in results.items():
        if official_only and not res.get("official"):
            continue
        if res.get("outcome") not in ("UP", "DOWN"):
            continue
        keys = buckets_of(int(r))
        absmove = None
        if isinstance(res.get("open"), (int, float)) and isinstance(res.get("close"), (int, float)):
            absmove = abs(res["close"] - res["open"])
        pick = _first_lead_pick(samples.get(r, []), min_move_signal=min_move_signal,
                                min_size=min_size, floor=lead_min_conf)
        for k in keys:
            d = drift[k]
            d["n"] += 1
            d["up"] += 1 if res["outcome"] == "UP" else 0
            if absmove is not None:
                d["mv_sum"] += absmove
                d["mv_n"] += 1
            if pick is not None:
                lead[k].append((pick[0] == res["outcome"], pick[1]))

    rows = []
    for k in order:
        d = drift[k]
        row: dict[str, Any] = {"bucket": k, "rounds": d["n"],
                               "up_pct": round(d["up"] / d["n"], 4) if d["n"] else None,
                               "avg_abs_move": round(d["mv_sum"] / d["mv_n"], 6) if d["mv_n"] else None}
        recs = lead[k]
        if recs:
            n = len(recs)
            wr = sum(1 for w, _ in recs if w) / n
            ap = sum(p for _, p in recs) / n
            ev = (wr - ap) / ap if ap else 0.0
            row.update({"lead_n": n, "lead_winrate": round(wr, 4),
                        "lead_avg_price": round(ap, 4),
                        "lead_net_ev": round(ev - taker_fee_frac(ap, fee_rate), 4)})
        else:
            row["lead_n"] = 0
        rows.append(row)
    return rows


def _print_session(rows: list[dict[str, Any]], min_move_signal: float,
                   lead_min_conf: float) -> None:
    print("=" * 74)
    print("SESSION / TIME-OF-DAY ANALYSIS  (official labels; ET = UTC-4 in summer)")
    print("=" * 74)
    if not any(r["rounds"] for r in rows):
        print("  no official-graded rounds yet — let it run.")
        print("=" * 74)
        return
    print("  DRIFT — how rounds resolve by session (bias regardless of any signal)")
    print(f"  {'bucket':<20}{'rounds':<8}{'UP%':<8}{'avg |move|'}")
    for r in rows:
        if not r["rounds"]:
            print(f"  {r['bucket']:<20}0")
            continue
        mv = f"{r['avg_abs_move']:.4g}" if r.get("avg_abs_move") is not None else "-"
        print(f"  {r['bucket']:<20}{r['rounds']:<8}{r['up_pct']:<8.1%}{mv}")
    print("-" * 74)
    print(f"  LEAD SETUP by session ({LEAD_WIN_LO:g}-{LEAD_WIN_HI:g}s left, ask <= {LEAD_MAX_PRICE:g}, "
          f"|move| >= {min_move_signal:g}, conf >= {lead_min_conf:g})")
    print(f"  {'bucket':<20}{'n':<6}{'winrate':<10}{'avg price':<11}{'NET EV'}")
    for r in rows:
        if not r.get("lead_n"):
            print(f"  {r['bucket']:<20}0     (no qualifying rounds)")
            continue
        print(f"  {r['bucket']:<20}{r['lead_n']:<6}{r['lead_winrate']:<10.1%}"
              f"{r['lead_avg_price']:<11.3f}{r['lead_net_ev']:+.1%}")
    print("-" * 74)
    print("  Read: UP% far from 50% at n>=100+ = a real session lean worth testing;")
    print("  at small n every bucket looks biased. A lead row only matters where")
    print("  NET clears +5% at n>=100 in that bucket AND agrees across markets.")
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
    ap.add_argument("--combo-min-move", type=float, default=None,
                    help="With --combo/--by-session: the 'decisive move' threshold in dollars. "
                         "Default: auto-scaled to 0.016%% of the log's median spot (= BTC's $10 "
                         "rule), so the same setup works on ETH/SOL/XRP without hand-tuning.")
    ap.add_argument("--lead-hi", type=float, default=None,
                    help="Lead-rule window opens at this many seconds left (default 240) — set to "
                         "the TRADER'S actual window so combo/session/exit/executor tables grade "
                         "the rule being run")
    ap.add_argument("--lead-lo", type=float, default=None,
                    help="Lead-rule window closes at this many seconds left (default 150)")
    ap.add_argument("--lead-max-price", type=float, default=None,
                    help="Lead-rule ask cap (default 0.72) — set to the trader's actual cap")
    ap.add_argument("--combo-min-price", type=float, default=0.0,
                    help="With --combo: require the LEADING side's ask >= this (book-agreement "
                         "guard). If our 'leading' side is priced below this the market disagrees "
                         "with our move — the poisoned/bad-open case. 0 = off (current live rule).")
    ap.add_argument("--by-session", action="store_true",
                    help="Time-of-day/weekday regimes: per-session UP-drift and lead-setup "
                         "performance, official labels only")
    ap.add_argument("--calibration", action="store_true",
                    help="Confidence calibration: per conf band, how often the indicator side "
                         "actually won (official labels) — is the score a probability or a rank?")
    ap.add_argument("--disagreements", action="store_true",
                    help="Anatomy of spot-vs-official label disagreements, classified by the "
                         "order book: fixable reference errors vs irreducible photo finishes")
    ap.add_argument("--exit", action="store_true", dest="exit_study",
                    help="Exit-policy study on recorded bids: hold vs sell-at-30s vs lock-the-win "
                         "vs stop-loss, for lead entries at conf >= --min-conf")
    ap.add_argument("--lock-bid", type=float, default=0.90,
                    help="With --exit: sell early when our side's bid reaches this (default 0.90)")
    ap.add_argument("--stop-bid", type=float, default=0.30,
                    help="With --exit: cut the position when our side's bid falls to this (default 0.30)")
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

    # Rebind the lead-rule definition BEFORE any analysis runs — every consumer
    # of _first_lead_pick (combo, session, exit, disagreements, and the executor
    # check via import) resolves these globals when its args are left default.
    global LEAD_WIN_HI, LEAD_WIN_LO, LEAD_MAX_PRICE
    if args.lead_hi is not None:
        LEAD_WIN_HI = float(args.lead_hi)
    if args.lead_lo is not None:
        LEAD_WIN_LO = float(args.lead_lo)
    if args.lead_max_price is not None:
        LEAD_MAX_PRICE = float(args.lead_max_price)

    events = load(args.log)
    if args.combo_min_move is None:
        args.combo_min_move = auto_min_move(events)
        if args.combo or args.by_session:
            print(f"  (lead 'decisive move' threshold auto-scaled: {args.combo_min_move:g} "
                  f"= {MOVE_FRAC:.3%} of median spot)")
    if args.by_session:
        rows = session_analysis(events, min_size=args.min_size, fee_rate=args.fee_rate,
                                min_move_signal=args.combo_min_move,
                                lead_min_conf=args.min_conf or 0.0)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        _print_session(rows, args.combo_min_move, args.min_conf or 0.0)
        return 0
    if args.calibration:
        rows = calibration_analysis(events)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        _print_calibration(rows)
        return 0
    if args.disagreements:
        res = disagreement_analysis(events, min_move_signal=args.combo_min_move,
                                    min_size=args.min_size)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        _print_disagreements(res)
        return 0
    if args.exit_study:
        res = exit_policy_analysis(events, min_move_signal=args.combo_min_move,
                                   min_size=args.min_size, floor=args.min_conf or 0.0,
                                   lock_bid=args.lock_bid, stop_bid=args.stop_bid)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        _print_exit(res)
        return 0
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
                _print_combo(combo_analysis(chunk, min_size=args.min_size, fee_rate=args.fee_rate,
                                            min_move_signal=args.combo_min_move,
                                            min_price=args.combo_min_price))
            return 0
        if args.combo_min_price > 0:
            print(f"  (book-agreement guard: leading side ask must be >= {args.combo_min_price:g})")
        rows = combo_analysis(events, min_size=args.min_size, fee_rate=args.fee_rate,
                              min_move_signal=args.combo_min_move,
                              min_price=args.combo_min_price)
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
