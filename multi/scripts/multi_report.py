#!/usr/bin/env python3
"""
Aggregate report across all multi-bot slot sessions, grouped per
(asset, timeframe, mode) so timeframes can be compared head-to-head —
the whole point of running them in parallel.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def default_reports_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "runtime" / "reports")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-dir", default=default_reports_dir())
    ap.add_argument("--limit", type=int, default=2000,
                    help="max most-recent report files to scan")
    ap.add_argument("--mode", choices=["paper", "live"], default=None,
                    help="only include this mode")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    rdir = Path(args.reports_dir)
    files = sorted(rdir.glob("*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[: args.limit] if rdir.exists() else []

    groups: dict[tuple, dict] = defaultdict(lambda: {
        "slots": 0, "entries": 0, "wins": 0, "losses": 0, "flat": 0,
        "pnl_sum": 0.0, "pnl_known": 0, "results": Counter(),
        "close_reasons": Counter(), "sides": Counter(),
    })

    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        mode = str(obj.get("mode") or "?")
        if args.mode and mode != args.mode:
            continue
        key = (str(obj.get("asset") or "?"), str(obj.get("timeframe") or "?"), mode)
        g = groups[key]
        g["slots"] += 1
        g["results"][str(obj.get("result"))] += 1
        opened = obj.get("opened") or {}
        closed = obj.get("closed") or {}
        if opened:
            g["entries"] += 1
            g["sides"][str(opened.get("side"))] += 1
        if closed:
            g["close_reasons"][str(closed.get("close_reason"))] += 1
        pnl = obj.get("realized_cashflow_pnl_usdc")
        if isinstance(pnl, (int, float)):
            g["pnl_sum"] += float(pnl)
            g["pnl_known"] += 1
            if pnl > 0:
                g["wins"] += 1
            elif pnl < 0:
                g["losses"] += 1
            else:
                g["flat"] += 1

    out = {"reports_scanned": len(files), "groups": []}
    for (asset, tf, mode), g in sorted(groups.items()):
        entries = g["entries"]
        decided = g["wins"] + g["losses"]
        out["groups"].append({
            "asset": asset,
            "timeframe": tf,
            "mode": mode,
            "slots": g["slots"],
            "entries": entries,
            "entry_rate": round(entries / g["slots"], 3) if g["slots"] else None,
            "wins": g["wins"],
            "losses": g["losses"],
            "flat": g["flat"],
            "win_rate": round(g["wins"] / decided, 3) if decided else None,
            "pnl_sum_usdc": round(g["pnl_sum"], 6),
            "pnl_avg_usdc": round(g["pnl_sum"] / g["pnl_known"], 6) if g["pnl_known"] else None,
            "results": dict(g["results"]),
            "close_reasons": dict(g["close_reasons"]),
            "sides": dict(g["sides"]),
        })

    print(json.dumps(out, ensure_ascii=False, indent=2))

    if not args.json_only and out["groups"]:
        print()
        hdr = f"{'asset':<6} {'tf':<4} {'mode':<6} {'slots':>5} {'entries':>7} {'winrate':>7} {'pnl_sum':>10} {'pnl_avg':>9}"
        print(hdr)
        print("-" * len(hdr))
        for g in out["groups"]:
            wr = f"{g['win_rate']:.0%}" if g["win_rate"] is not None else "-"
            pa = f"{g['pnl_avg_usdc']:.4f}" if g["pnl_avg_usdc"] is not None else "-"
            print(f"{g['asset']:<6} {g['timeframe']:<4} {g['mode']:<6} "
                  f"{g['slots']:>5} {g['entries']:>7} {wr:>7} "
                  f"{g['pnl_sum_usdc']:>10.4f} {pa:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
