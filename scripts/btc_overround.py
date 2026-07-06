#!/usr/bin/env python3
"""
Overround / sum-of-asks monitor  (Fable experiment E2).

For a two-outcome market the two best asks should sum to a bit MORE than $1 — the
excess is the "overround", the market maker's edge and (for a taker) your
structural tax. This reads the recorder's trajectory log (up_ask + dn_ask every
5s) and reports:

  - the overround distribution (median = your exact taker tax before fees),
  - any DISLOCATIONS where up_ask + dn_ask <= threshold (< ~1.0): a direction-free
    "buy both sides" opportunity that a slow taker CAN capture if it persists a
    few seconds — and how long each one lasted.

    python3 scripts/btc_overround.py --log out/trajectory.jsonl

PASS (an edge worth chasing): several dislocations per day lasting >= a couple
samples. FAIL (efficient book): overround is always positive and stable — that
median is simply the tax you pay on every taker round.
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


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def analyze(events: list[dict[str, Any]], dislocation_at: float = 1.0) -> dict[str, Any]:
    sums: list[float] = []
    # Track consecutive dislocation runs within a round (samples are ~5s apart).
    runs: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for e in events:
        if e.get("type") != "sample":
            # a round boundary (result) ends any open run
            if cur is not None:
                runs.append(cur)
                cur = None
            continue
        up, dn = e.get("up_ask"), e.get("dn_ask")
        if not isinstance(up, (int, float)) or not isinstance(dn, (int, float)):
            continue
        s = up + dn
        sums.append(s)
        if s <= dislocation_at:
            if cur is None:
                cur = {"round": e.get("round"), "n": 0, "min_sum": s,
                       "start_ts": e.get("ts"), "end_ts": e.get("ts")}
            cur["n"] += 1
            cur["min_sum"] = min(cur["min_sum"], s)
            cur["end_ts"] = e.get("ts")
        else:
            if cur is not None:
                runs.append(cur)
                cur = None
    if cur is not None:
        runs.append(cur)

    for r in runs:
        st, en = r.get("start_ts"), r.get("end_ts")
        r["duration_s"] = (en - st) if (isinstance(st, (int, float)) and isinstance(en, (int, float))) else None

    n = len(sums)
    return {
        "n_samples": n,
        "median_overround": round(_median(sums) - 1.0, 4) if n else None,
        "min_sum": round(min(sums), 4) if n else None,
        "max_sum": round(max(sums), 4) if n else None,
        "pct_dislocated": round(100.0 * sum(1 for s in sums if s <= dislocation_at) / n, 2) if n else 0.0,
        "dislocations": runs,
        "dislocation_at": dislocation_at,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Overround / sum-of-asks monitor (E2)")
    ap.add_argument("--log", default="out/trajectory.jsonl")
    ap.add_argument("--at", type=float, default=1.0, help="Dislocation threshold: flag samples where up+dn <= this")
    ap.add_argument("--min-run", type=int, default=1, help="Only list dislocations lasting >= this many consecutive samples")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    res = analyze(load(args.log), dislocation_at=args.at)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0

    print("=" * 66)
    print("OVERROUND / SUM-OF-ASKS MONITOR  (E2)")
    print("=" * 66)
    if not res["n_samples"]:
        print("no price samples yet — run scripts/record.sh first.")
        return 0
    mo = res["median_overround"]
    print(f"  samples: {res['n_samples']}")
    print(f"  median overround: {mo:+.3f}  (up+dn = {1 + mo:.3f})  <- your taker tax before fees")
    print(f"  sum range: {res['min_sum']:.3f} .. {res['max_sum']:.3f}")
    print(f"  samples with up+dn <= {res['dislocation_at']:.2f}: {res['pct_dislocated']:.2f}%")
    runs = [r for r in res["dislocations"] if r["n"] >= args.min_run]
    print("-" * 66)
    if runs:
        print(f"  DISLOCATIONS (up+dn <= {res['dislocation_at']:.2f}, >= {args.min_run} sample(s)):")
        print(f"    {'round':<14}{'samples':<9}{'~secs':<8}{'min sum'}")
        for r in runs[:50]:
            dur = r.get("duration_s")
            dur_s = f"{dur:.0f}" if dur is not None else "?"
            print(f"    {str(r['round']):<14}{r['n']:<9}{dur_s:<8}{r['min_sum']:.3f}")
        print(f"  -> {len(runs)} dislocation run(s). A slow taker can only capture ones")
        print("     that persist several seconds AND clear the fee. Check duration + depth.")
    else:
        print("  NO dislocations: up+dn stays above the threshold at all times.")
        print("  -> efficient book. The median overround above is simply the tax you")
        print("     pay as a taker on every round; it is not capturable.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
