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
        "close_reasons": Counter(), "sides": Counter(), "entry_px": [],
        "echo_n": 0, "echo_fillable": 0, "echo_adverse": [],
    })

    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        mode = str(obj.get("mode") or "?")
        if args.mode and mode != args.mode:
            continue
        strategy = str(obj.get("strategy") or "favorite")
        tf_key = str(obj.get("timeframe") or "?")
        if strategy != "favorite":
            tf_key += f" {strategy}"
        key = (str(obj.get("asset") or "?"), tf_key, mode)
        g = groups[key]
        g["slots"] += 1
        g["results"][str(obj.get("result"))] += 1
        opened = obj.get("opened") or {}
        closed = obj.get("closed") or {}
        if opened:
            g["entries"] += 1
            g["sides"][str(opened.get("side"))] += 1
            if isinstance(opened.get("entry_price"), (int, float)):
                g["entry_px"].append(float(opened["entry_price"]))
            echo = opened.get("fill_echo") or {}
            if "still_fillable" in echo:
                g["echo_n"] += 1
                if echo["still_fillable"]:
                    g["echo_fillable"] += 1
                if isinstance(echo.get("adverse_move"), (int, float)):
                    g["echo_adverse"].append(float(echo["adverse_move"]))
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
        px = sorted(g["entry_px"])
        med_px = px[len(px) // 2] if px else None
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
            # for hold-to-resolution longs the breakeven win rate IS the
            # entry price; for early-exit strategies it's an approximation
            "median_entry_price": round(med_px, 3) if med_px is not None else None,
            "breakeven_win_rate_approx": round(med_px, 3) if med_px is not None else None,
            "edge_vs_breakeven": (round(g["wins"] / decided - med_px, 3)
                                  if decided and med_px is not None else None),
            "pnl_sum_usdc": round(g["pnl_sum"], 6),
            "pnl_avg_usdc": round(g["pnl_sum"] / g["pnl_known"], 6) if g["pnl_known"] else None,
            # paper-fill fidelity: share of entries whose price was still on
            # the book ~1.5s later (a live order's flight time), and the
            # average adverse move when it wasn't
            "fill_persist_rate": (round(g["echo_fillable"] / g["echo_n"], 3)
                                  if g["echo_n"] else None),
            "fill_echo_checked": g["echo_n"],
            "fill_avg_adverse_move": (round(sum(g["echo_adverse"]) / len(g["echo_adverse"]), 4)
                                      if g["echo_adverse"] else None),
            "results": dict(g["results"]),
            "close_reasons": dict(g["close_reasons"]),
            "sides": dict(g["sides"]),
        })

    print(json.dumps(out, ensure_ascii=False, indent=2))

    if not args.json_only and out["groups"]:
        print()
        hdr = (f"{'asset':<6} {'series':<14} {'mode':<6} {'slots':>5} {'entries':>7} "
               f"{'winrate':>7} {'b/e~':>6} {'edge':>7} {'fill✓':>6} "
               f"{'pnl_sum':>10} {'pnl_avg':>9}")
        print(hdr)
        print("-" * len(hdr))
        for g in out["groups"]:
            wr = f"{g['win_rate']:.0%}" if g["win_rate"] is not None else "-"
            be = (f"{g['breakeven_win_rate_approx']:.0%}"
                  if g["breakeven_win_rate_approx"] is not None else "-")
            ed = (f"{g['edge_vs_breakeven']:+.0%}"
                  if g["edge_vs_breakeven"] is not None else "-")
            fp = (f"{g['fill_persist_rate']:.0%}"
                  if g["fill_persist_rate"] is not None else "-")
            pa = f"{g['pnl_avg_usdc']:.4f}" if g["pnl_avg_usdc"] is not None else "-"
            print(f"{g['asset']:<6} {g['timeframe']:<14} {g['mode']:<6} "
                  f"{g['slots']:>5} {g['entries']:>7} {wr:>7} {be:>6} {ed:>7} {fp:>6} "
                  f"{g['pnl_sum_usdc']:>10.4f} {pa:>9}")
        print("\nb/e~ = breakeven win rate (≈ median entry price; exact for "
              "hold-to-resolution). edge = winrate − b/e. A 15% win rate at "
              "$0.10 entries is a GOOD result; judge underdog/arb by edge and "
              "pnl, never by raw winrate.\n"
              "fill✓ = share of paper entries whose price was still on the "
              "book ~1.5s later (a live order's flight time). High fill✓ = "
              "paper results should carry to live; low = latency tax, expect "
              "live to underperform paper on that arm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
