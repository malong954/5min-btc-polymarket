#!/usr/bin/env python3
"""
Correlate the indicator readings on the live timeline with outcomes.

Reads the enriched paper-trader log (entries, skips, settles, shadow_settles —
each carrying a `features` snapshot: RSI, MACD, Bollinger, ROC, divergence, EMA
trend, volume, agreement) and answers:

  1. Confidence vs win-rate — is higher confidence actually more accurate?
  2. Per-indicator correlation with the outcome — which readings track wins?
  3. Divergence type vs win-rate.
  4. Were the SKIPS correct? — shadow-settled would-be trades: if they mostly
     lost, the threshold was right to skip them.

    python3 scripts/btc_timeline_analyze.py --log out/live.jsonl

Entered rounds settle as `settle`; skipped-but-predicted rounds settle as
`shadow_settle` (no money) so every prediction has a win/loss to analyze.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Optional

from btc_indicators import pearson

NUMERIC = ["score", "confidence", "rsi_1m", "macd_hist_1m", "bb_pctb_1m", "roc_1m",
           "div_signal", "ema_gap_5m", "ema_gap_15m", "rel_volume_1m", "agreement",
           "vol_factor", "btc_move_usd"]

CONF_BANDS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


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


def build_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per round that reached an outcome, with features + win flag."""
    feat_by_round: dict[int, dict[str, Any]] = {}
    conf_by_round: dict[int, float] = {}
    for e in events:
        if e.get("type") in ("prediction", "entry", "skip") and e.get("features"):
            r = e.get("round")
            feat_by_round.setdefault(r, e["features"])
            if e.get("confidence") is not None:
                conf_by_round.setdefault(r, e["confidence"])

    recs = []
    for e in events:
        t = e.get("type")
        if t not in ("settle", "shadow_settle"):
            continue
        r = e.get("round")
        feats = dict(e.get("features") or feat_by_round.get(r) or {})
        conf = e.get("confidence")
        if conf is None:
            conf = conf_by_round.get(r)
        if conf is not None:
            feats["confidence"] = conf
        recs.append({
            "round": r, "entered": t == "settle",
            "side": e.get("side"), "actual": e.get("actual"),
            "win": 1 if e.get("result") == "win" else 0,
            "entry_price": e.get("entry_price"),
            "divergence": (feats.get("divergence") if feats else None),
            "features": feats,
        })
    return recs


def _winrate(rows):
    n = len(rows)
    return (sum(r["win"] for r in rows) / n, n) if n else (0.0, 0)


def confidence_table(recs, which):
    rows = [r for r in recs if (which == "all" or (which == "entered") == r["entered"])]
    out = []
    for lo, hi in CONF_BANDS:
        band = [r for r in rows if r["features"].get("confidence") is not None
                and lo <= r["features"]["confidence"] < hi]
        if band:
            wr, n = _winrate(band)
            out.append((f"{lo:.2f}-{min(hi,1.0):.2f}", n, wr))
    return out


def indicator_correlations(recs):
    wins = [float(r["win"]) for r in recs]
    out = []
    for key in NUMERIC:
        xs, ys = [], []
        for r, w in zip(recs, wins):
            v = r["features"].get(key)
            if isinstance(v, (int, float)):
                xs.append(float(v))
                ys.append(w)
        if len(xs) >= 5 and len(set(xs)) > 1:
            out.append((key, pearson(xs, ys), len(xs)))
    out.sort(key=lambda t: -abs(t[1]))
    return out


def divergence_table(recs):
    labels: dict[str, list] = {}
    for r in recs:
        d = r["divergence"] or "none"
        labels.setdefault(d, []).append(r)
    out = []
    for d, rows in labels.items():
        wr, n = _winrate(rows)
        out.append((d, n, wr))
    out.sort(key=lambda t: -t[1])
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Correlate indicator readings with outcomes on the timeline")
    ap.add_argument("--log", default="out/live.jsonl")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    events = load(args.log)
    recs = build_records(events)
    entered = [r for r in recs if r["entered"]]
    shadow = [r for r in recs if not r["entered"]]

    if args.json:
        print(json.dumps({
            "n_total": len(recs), "n_entered": len(entered), "n_shadow": len(shadow),
            "confidence_all": confidence_table(recs, "all"),
            "correlations": indicator_correlations(recs),
            "divergence": divergence_table(recs),
        }, indent=2, default=str))
        return 0

    print("=" * 70)
    print("TIMELINE CORRELATION ANALYSIS")
    print("=" * 70)
    if not recs:
        print("no settled rounds with features yet — let the trader run (the log")
        print("needs the enriched entry/skip/settle events).")
        return 0
    ew, en = _winrate(entered)
    sw, sn = _winrate(shadow)
    print(f"  rounds analyzed: {len(recs)}   entered: {en} (winrate {ew:.1%})   "
          f"skipped/shadow: {sn} (would-be winrate {sw:.1%})")

    print("\n  (1) CONFIDENCE vs WIN-RATE  (all predicted rounds)")
    print(f"      {'confidence':<13}{'n':<6}{'winrate'}")
    for label, n, wr in confidence_table(recs, "all"):
        print(f"      {label:<13}{n:<6}{wr:.1%}")
    print("      -> higher bands should win more; if flat, confidence isn't tracking accuracy.")

    print("\n  (2) INDICATOR CORRELATION with outcome (pearson vs win=1/loss=0)")
    print(f"      {'indicator':<16}{'corr':<9}{'n'}")
    corrs = indicator_correlations(recs)
    if corrs:
        for key, c, n in corrs:
            arrow = "↑ wins" if c > 0.05 else "↓ wins" if c < -0.05 else "~"
            print(f"      {key:<16}{c:<+9.3f}{n:<6}{arrow}")
        print("      -> |corr| near 0 = that reading doesn't predict the outcome on this data.")
    else:
        print("      (not enough varied data yet)")

    print("\n  (3) DIVERGENCE TYPE vs WIN-RATE")
    print(f"      {'type':<20}{'n':<6}{'winrate'}")
    for d, n, wr in divergence_table(recs):
        print(f"      {d:<20}{n:<6}{wr:.1%}")

    print("\n  (4) WERE THE SKIPS CORRECT?")
    if sn:
        print(f"      shadow would-be trades: {sn}, would-be winrate {sw:.1%}")
        if sw < ew:
            print("      -> skipped rounds won LESS than entered ones: the threshold is")
            print("         filtering correctly (it's passing the weaker setups).")
        else:
            print("      -> skipped rounds won as much or MORE than entered ones: the")
            print("         threshold may be too strict (leaving good trades on the table).")
    else:
        print("      no shadow-settled skips yet.")

    # (5) FILL SLIPPAGE — how far the ask moved between deciding and the
    # honest-fill requote one book-read later. This is the paper-vs-live gap
    # made measurable: a fat right tail here means live fills will be worse
    # than the table, no matter what the winrate says.
    slips = [e["slip"] for e in events
             if e.get("type") == "requote" and isinstance(e.get("slip"), (int, float))]
    print("\n  (5) FILL SLIPPAGE  (ask movement during the entry's order flight)")
    if slips:
        moved = [s for s in slips if s > 0]
        worst = max(slips)
        avg_paid = sum(max(s, 0.0) for s in slips) / len(slips)
        print(f"      requoted entries: {len(slips)}   quote moved against us: "
              f"{len(moved)} ({len(moved) / len(slips):.0%})")
        print(f"      avg slip paid: {avg_paid:+.4f}   worst: {worst:+.4f}")
        print("      -> avg slip paid is a direct tax on the edge; if it rivals the")
        print("         NET EV in the combo table, the strategy dies in transit.")
    else:
        print("      no requote events yet (needs entries made after the honest-fill update).")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
