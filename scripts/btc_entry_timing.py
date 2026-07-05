#!/usr/bin/env python3
"""
Find the optimal entry time from a trajectory log (scripts/btc_record.py).

For each entry offset (seconds before close), it simulates the momentum rule —
"buy the side BTC is already leading, at that side's ask right then" — and reports
the win rate, the average price you'd pay, and the resulting edge. Earlier entries
are cheaper but less certain; later entries are surer but pricier. The sweet spot
is the offset with the highest edge.

    python3 scripts/btc_entry_timing.py --log out/trajectory.jsonl

edge = winrate - avg_entry_price. Because breakeven win-rate equals the price,
a POSITIVE edge means that entry time is profitable; negative means it loses.
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


def analyze(events: list[dict[str, Any]], bins: list[tuple[int, int]] = DEFAULT_BINS) -> list[dict[str, Any]]:
    samples: dict[int, list[dict[str, Any]]] = {}
    results: dict[int, dict[str, Any]] = {}
    for e in events:
        t = e.get("type")
        if t == "sample":
            samples.setdefault(e["round"], []).append(e)
        elif t == "result":
            results[e["round"]] = e

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
            move = s.get("move", 0.0)
            if move == 0:
                continue
            side = "UP" if move > 0 else "DOWN"
            price = s.get("up_ask") if side == "UP" else s.get("dn_ask")
            if price is None or not (0.0 < price < 1.0):
                continue
            recs.append((side == outcome, float(price)))
        if recs:
            n = len(recs)
            wins = sum(1 for w, _ in recs if w)
            wr = wins / n
            avg_price = sum(p for _, p in recs) / n
            # EV per $1 staked = (winrate - price)/price; edge = winrate - price.
            ev_pct = (wr - avg_price) / avg_price if avg_price else 0.0
            rows.append({"offset": [lo, hi], "n": n, "winrate": round(wr, 4),
                         "avg_price": round(avg_price, 4), "edge": round(wr - avg_price, 4),
                         "ev_pct": round(ev_pct, 4)})
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Optimal entry-time analysis from a trajectory log")
    ap.add_argument("--log", default="out/trajectory.jsonl")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows = analyze(load(args.log))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print("=" * 66)
    print("ENTRY-TIME ANALYSIS  (buy the leading side at each offset)")
    print("=" * 66)
    if not rows:
        print("no complete rounds yet — let scripts/btc_record.py run longer.")
        return 0
    print(f"  {'sec left':<12}{'n':<6}{'winrate':<10}{'avg price':<11}{'edge':<9}{'EV/trade'}")
    for r in rows:
        print(f"  {str(r['offset']):<12}{r['n']:<6}{r['winrate']:<10.1%}{r['avg_price']:<11.3f}"
              f"{r['edge']:<+9.3f}{r['ev_pct']:+.1%}")
    best = max(rows, key=lambda r: r["edge"])
    print("-" * 66)
    if best["edge"] > 0:
        print(f"BEST entry: {best['offset'][0]}-{best['offset'][1]}s left  ->  "
              f"winrate {best['winrate']:.0%} @ price {best['avg_price']:.2f}  "
              f"edge {best['edge']:+.3f}  (EV {best['ev_pct']:+.1%}/trade)")
        print("Earlier = cheaper but less certain; this offset balances the two best.")
    else:
        print("EVERY entry time has NEGATIVE edge -> the market is efficiently priced;")
        print("no entry timing makes the momentum rule profitable on this data.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
