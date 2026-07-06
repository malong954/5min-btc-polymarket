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
    samples: dict[int, list[dict[str, Any]]] = {}
    results: dict[int, dict[str, Any]] = {}
    for e in events:
        t = e.get("type")
        if t == "sample":
            samples.setdefault(e["round"], []).append(e)
        elif t == "result":
            results[e["round"]] = e
    return samples, results


VEL_FIELDS = {"vel_5s": "vel_5s", "vel_15s": "vel_15s", "vel_30s": "vel_30s", "vel_60s": "vel_60s"}


def _side_and_price(s: dict[str, Any], side_source: str, min_conf: Optional[float]):
    """Resolve (side, price) for one sample under the chosen rule, or (None, None)
    to skip it. side_source: move | indicator | vel_5s | vel_15s | vel_30s | vel_60s."""
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
    return side, float(price)


def taker_fee_frac(price: float, fee_rate: float) -> float:
    """Polymarket taker fee as a fraction of the dollars STAKED.

    Per the fee schedule fee = contracts * rate * p * (1-p); staking $1 buys
    1/price contracts, so fee/stake = rate * (1 - price). Largest near p=0.5,
    negligible near the extremes. Makers pay zero (post limit orders instead)."""
    return fee_rate * (1.0 - price)


def analyze(events: list[dict[str, Any]], bins: list[tuple[int, int]] = DEFAULT_BINS,
            side_source: str = "move", min_conf: Optional[float] = None,
            fee_rate: float = 0.0) -> list[dict[str, Any]]:
    samples, results = _split(events)
    rows = []
    for lo, hi in bins:
        recs: list[tuple[bool, float]] = []
        for r, samps in samples.items():
            res = results.get(r)
            if not res:
                continue
            outcome = res["outcome"]
            inbin = [s for s in samps if lo <= s["sec_left"] < hi]
            if not inbin:
                continue
            mid = (lo + hi) / 2.0
            s = min(inbin, key=lambda x: abs(x["sec_left"] - mid))
            side, price = _side_and_price(s, side_source, min_conf)
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
    ap.add_argument("--by-confidence", action="store_true",
                    help="Instead of the entry-time table, show edge bucketed by indicator confidence")
    ap.add_argument("--conf-offset", type=float, default=120.0,
                    help="Seconds-left decision point for --by-confidence (default 120)")
    ap.add_argument("--fee-rate", type=float, default=0.0,
                    help="Polymarket TAKER fee rate (fee=contracts*rate*p*(1-p)). 0=off. "
                         "Read the REAL rate from the CLOB market data, don't hardcode. Makers pay 0.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    events = load(args.log)
    if args.by_confidence:
        rows = confidence_bands(events, ref_sec=args.conf_offset)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        _print_bands(rows, args.conf_offset)
        return 0

    rows = analyze(events, side_source=args.side, min_conf=args.min_conf, fee_rate=args.fee_rate)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    _print_offsets(rows, args.side, args.min_conf, args.fee_rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
