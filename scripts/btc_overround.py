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
import os
import sys
from typing import Any, Optional


def load(path: str) -> list[dict[str, Any]]:
    out = []
    if not os.path.exists(path):
        print(f"log not found: {path}", file=sys.stderr)
        print("that recorder has not run yet (or has not produced samples).", file=sys.stderr)
        print("for ETH: start it once with  ETH=1 scripts/lab.sh  then let it collect.", file=sys.stderr)
        raise SystemExit(2)
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


def analyze(events: list[dict[str, Any]], dislocation_at: float = 1.0,
            min_size: float = 0.0) -> dict[str, Any]:
    sums: list[float] = []
    ts_all: list[float] = []
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
        if isinstance(e.get("ts"), (int, float)):
            ts_all.append(e["ts"])
        sized = True
        if min_size > 0:
            usz, dsz = e.get("up_sz"), e.get("dn_sz")
            sized = (isinstance(usz, (int, float)) and usz >= min_size
                     and isinstance(dsz, (int, float)) and dsz >= min_size)
        if s <= dislocation_at and sized:
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
    span_days = ((max(ts_all) - min(ts_all)) / 86400.0) if len(ts_all) >= 2 else None

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
        "min_size": min_size,
        "span_days": round(span_days, 2) if span_days else None,
    }


def arb_net_per_pair(s: float, fee_rate: float) -> float:
    """Net profit of buying BOTH sides (one share each) at combined cost `s`:
    payout is exactly $1 at settle; taker fee per leg = rate*p*(1-p) per share.
    Assumes pU ~ s/2 each for the fee term (exact split changes it little)."""
    p = s / 2.0
    fee = 2.0 * fee_rate * p * (1.0 - p)
    return (1.0 - s) - fee


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Overround / sum-of-asks monitor (E2)")
    ap.add_argument("--log", default="out/trajectory.jsonl")
    ap.add_argument("--at", type=float, default=1.0, help="Dislocation threshold: flag samples where up+dn <= this")
    ap.add_argument("--min-run", type=int, default=1, help="Only list dislocations lasting >= this many consecutive samples")
    ap.add_argument("--min-size", type=float, default=0.0,
                    help="Require BOTH asks to have at least this many shares (executable-arb filter)")
    ap.add_argument("--fee-rate", type=float, default=0.0, help="Taker fee rate for the net-profit column")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    res = analyze(load(args.log), dislocation_at=args.at, min_size=args.min_size)
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
    sized = f", both sizes >= {args.min_size:.0f}" if args.min_size > 0 else ""
    if runs:
        print(f"  DISLOCATIONS (up+dn <= {res['dislocation_at']:.2f}, >= {args.min_run} sample(s){sized}):")
        hdr = f"    {'round':<14}{'samples':<9}{'~secs':<8}{'min sum':<9}"
        if args.fee_rate > 0:
            hdr += "net/pair"
        print(hdr)
        for r in runs[:50]:
            dur = r.get("duration_s")
            dur_s = f"{dur:.0f}" if dur is not None else "?"
            row = f"    {str(r['round']):<14}{r['n']:<9}{dur_s:<8}{r['min_sum']:<9.3f}"
            if args.fee_rate > 0:
                row += f"{arb_net_per_pair(r['min_sum'], args.fee_rate):+.3f}"
            print(row)
        print(f"  -> {len(runs)} run(s)", end="")
        if res.get("span_days"):
            print(f"  (~{len(runs) / res['span_days']:.1f}/day over {res['span_days']:.1f} days)", end="")
        print()
        pos = [r for r in runs if arb_net_per_pair(r["min_sum"], args.fee_rate) > 0]
        if args.fee_rate > 0:
            print(f"  -> {len(pos)} clear the {args.fee_rate:.0%} fee. This is a RISKLESS payoff")
            print("     (both sides pay $1 total at settle) — the open question is only")
            print("     whether real orders fill before the quote vanishes.")
        else:
            print("     Rerun with --fee-rate and --min-size 100 --min-run 2 for the")
            print("     EXECUTABLE subset — riskless if fills match the recorded quotes.")
    else:
        print(f"  NO dislocations pass (threshold {res['dislocation_at']:.2f}{sized}, >= {args.min_run} samples).")
        print("  -> at these filters the book never offers a capturable discount; the")
        print("     median overround above is simply the taker tax on every round.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
