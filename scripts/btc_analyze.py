#!/usr/bin/env python3
"""
Analyze a live paper-trading JSONL log into clean stats.

Purpose: give a human — or Claude Code running under your subscription — a
grounded, deterministic summary to reason over, instead of eyeballing raw JSON.
It joins prediction/entry/settle events by round, then breaks win rate and PnL
down by confidence, real entry price, hour, side, and BTC move, and flags a few
deterministic observations.

    python3 scripts/btc_analyze.py --log out/live.jsonl
    python3 scripts/btc_analyze.py --log out/live.jsonl --json

IMPORTANT: patterns in a small log are usually noise. Treat every observation as
a HYPOTHESIS to validate with the backtest (walk-forward / sizing-report) on the
accumulated real data before changing anything live. See --help notes.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Optional


def load_events(path: str) -> list[dict[str, Any]]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def join_by_round(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge prediction + entry + settle events that share a round into one
    record. Only rounds that actually settled are returned (they have an outcome)."""
    preds: dict[int, dict[str, Any]] = {}
    entries: dict[int, dict[str, Any]] = {}
    settles: dict[int, dict[str, Any]] = {}
    for ev in events:
        r = ev.get("round")
        if r is None:
            continue
        t = ev.get("type")
        if t == "prediction":
            preds[r] = ev
        elif t == "entry":
            entries[r] = ev
        elif t == "settle":
            settles[r] = ev

    recs = []
    for r, s in settles.items():
        p = preds.get(r, {})
        e = entries.get(r, {})
        recs.append({
            "round": r,
            "ts": s.get("ts"),
            "side": s.get("side"),
            "actual": s.get("actual"),
            "result": s.get("result"),
            "win": s.get("result") == "win",
            "entry_price": s.get("entry_price", e.get("entry_price")),
            "price_source": e.get("price_source"),
            "confidence": e.get("confidence", p.get("confidence")),
            "btc_move_usd": p.get("btc_move_usd"),
            "rsi_1m": p.get("rsi_1m"),
            "stake_usd": s.get("stake_usd", e.get("stake_usd")),
            "pnl_usd": s.get("pnl_usd"),
            "balance": s.get("balance"),
        })
    recs.sort(key=lambda x: (x["ts"] or 0))
    return recs


def _wr(rows: list[dict[str, Any]]) -> float:
    return sum(1 for r in rows if r["win"]) / len(rows) if rows else 0.0


def _bucketize(recs: list[dict[str, Any]], key: str, edges: list[float]) -> list[dict[str, Any]]:
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        grp = [r for r in recs if r.get(key) is not None and lo <= r[key] < hi]
        if grp:
            out.append({"range": [round(lo, 3), round(hi, 3)], "n": len(grp),
                        "winrate": round(_wr(grp), 4)})
    return out


def analyze(recs: list[dict[str, Any]], breakeven_default: float = 0.85) -> dict[str, Any]:
    n = len(recs)
    wins = sum(1 for r in recs if r["win"])
    wr = wins / n if n else 0.0

    priced = [r for r in recs if r.get("entry_price") is not None]
    real = [r for r in recs if r.get("price_source") == "polymarket"]
    avg_entry = sum(r["entry_price"] for r in priced) / len(priced) if priced else None
    breakeven = avg_entry if avg_entry is not None else breakeven_default

    pnl = sum(r["pnl_usd"] for r in recs if r.get("pnl_usd") is not None)
    balances = [r["balance"] for r in recs if r.get("balance") is not None]

    # By side.
    by_side = {}
    for s in ("UP", "DOWN"):
        grp = [r for r in recs if r["side"] == s]
        if grp:
            by_side[s] = {"n": len(grp), "winrate": round(_wr(grp), 4)}

    # By hour (local).
    by_hour = {}
    for r in recs:
        if r["ts"] is None:
            continue
        h = time.localtime(r["ts"]).tm_hour
        by_hour.setdefault(h, []).append(r)
    by_hour_out = {str(h): {"n": len(v), "winrate": round(_wr(v), 4)}
                   for h, v in sorted(by_hour.items())}

    # Deterministic observations (hypotheses only).
    obs = []
    if n < 100:
        obs.append(f"SAMPLE: only {n} settled trades — statistically weak; a win-rate "
                   f"estimate here can be off by ~10 points. Gather 200-1000+ before trusting any pattern.")
    if avg_entry is not None:
        verdict = "ABOVE" if wr > breakeven else "BELOW"
        obs.append(f"EDGE: win rate {wr:.1%} vs real breakeven {breakeven:.1%} -> {verdict} breakeven.")
    if len(real) < n:
        obs.append(f"PRICING: {len(real)}/{n} trades used REAL Polymarket prices; the rest are "
                   f"assumed/fixed and should be excluded or re-run before drawing conclusions.")
    conf_buckets = _bucketize(recs, "confidence", [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01])
    if len(conf_buckets) >= 2:
        hi = conf_buckets[-1]
        lo = conf_buckets[0]
        if hi["winrate"] - lo["winrate"] > 0.10:
            obs.append(f"CONFIDENCE: high bucket {hi['range']} wr={hi['winrate']:.0%} beats low "
                       f"{lo['range']} wr={lo['winrate']:.0%} -> raising the threshold MIGHT help "
                       f"(validate with backtest --sizing-report on real data).")
    if len(by_side) == 2:
        up, dn = by_side["UP"]["winrate"], by_side["DOWN"]["winrate"]
        if abs(up - dn) > 0.12:
            obs.append(f"SIDE BIAS: UP wr={up:.0%} vs DOWN wr={dn:.0%} — could be real or noise; check on more data.")

    return {
        "settled_trades": n,
        "winrate": round(wr, 4),
        "real_priced": len(real),
        "avg_real_entry_price": round(avg_entry, 4) if avg_entry is not None else None,
        "breakeven": round(breakeven, 4),
        "edge_vs_breakeven": round(wr - breakeven, 4),
        "pnl_usd": round(pnl, 2),
        "final_balance": round(balances[-1], 2) if balances else None,
        "by_confidence": conf_buckets,
        "by_entry_price": _bucketize(recs, "entry_price", [0.5, 0.8, 0.85, 0.9, 0.95, 1.01]),
        "by_side": by_side,
        "by_hour_local": by_hour_out,
        "by_btc_move": _bucketize(
            [{**r, "abs_move": abs(r["btc_move_usd"])} for r in recs if r.get("btc_move_usd") is not None],
            "abs_move", [0, 20, 40, 70, 100, 1e9]),
        "observations": obs,
    }


def _print(a: dict[str, Any]) -> None:
    print("=" * 64)
    print("BTC 5m Paper-Trading Analysis")
    print("=" * 64)
    print(f"settled trades   : {a['settled_trades']}   (real-priced: {a['real_priced']})")
    print(f"win rate         : {a['winrate']:.1%}")
    if a["avg_real_entry_price"] is not None:
        print(f"avg real entry   : ${a['avg_real_entry_price']:.3f}  -> breakeven {a['breakeven']:.1%}")
    print(f"edge vs breakeven: {a['edge_vs_breakeven']:+.1%}")
    print(f"PnL / balance    : ${a['pnl_usd']:+.2f}  /  ${a['final_balance']}")
    print("-" * 64)
    for label, key in [("by confidence", "by_confidence"), ("by entry price", "by_entry_price"),
                       ("by |BTC move|", "by_btc_move")]:
        rows = a[key]
        if rows:
            print(f"{label}:")
            for b in rows:
                print(f"   {str(b['range']):<16} n={b['n']:<4} wr={b['winrate']:.0%}")
    if a["by_side"]:
        print("by side:", "  ".join(f"{s} n={v['n']} wr={v['winrate']:.0%}" for s, v in a["by_side"].items()))
    print("-" * 64)
    print("observations (HYPOTHESES — validate with the backtest before changing live):")
    for o in a["observations"]:
        print(f"  - {o}")
    print("=" * 64)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Analyze a paper-trading JSONL log")
    ap.add_argument("--log", default="out/live.jsonl")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    recs = join_by_round(load_events(args.log))
    a = analyze(recs)
    if args.json:
        print(json.dumps(a, indent=2))
    else:
        _print(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
