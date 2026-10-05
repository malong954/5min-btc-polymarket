#!/usr/bin/env python3
"""Executor-vs-table audit: did the live trader trade the SAME rounds the
combo table measured — and where did the winrate gap come from?

The combo table (recorder, 5s samples, official labels) said one number; the
live trader executing 'the same rule' produced a worse one. This joins the
two logs by round and splits the trader's record three ways:

  BOTH        — rounds the trader entered AND the table's rule selected
  TRADER-ONLY — rounds the trader entered that the table's rule did NOT pick
                (threshold-grazing 2s-poll entries, price/size mismatches...)
  TABLE-ONLY  — rounds the rule picked that the trader missed (down, late,
                already insolvent, price moved past the cap...)

If the edge lives in BOTH and TRADER-ONLY bleeds, the strategy is fine and
the EXECUTOR is the bug (enter only on sustained signals). If BOTH also
bleeds, the table itself doesn't survive contact — a much worse verdict.

    python3 scripts/btc_executor_check.py --log out/trajectory.jsonl \
        --trader-log out/live.jsonl --floor 0.40 --min-size 100
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Optional

from btc_entry_timing import _first_lead_pick, _split, auto_min_move, load


def executor_check(rec_events: list[dict[str, Any]], trader_events: list[dict[str, Any]],
                   floor: float = 0.40, min_size: float = 0.0,
                   min_move: Optional[float] = None) -> dict[str, Any]:
    samples, results = _split(rec_events)
    if min_move is None:
        min_move = auto_min_move(rec_events)
    official = {r: res["outcome"] for r, res in results.items()
                if res.get("official") and res.get("outcome") in ("UP", "DOWN")}

    table: dict[int, tuple] = {}
    for r, samps in samples.items():
        if r not in official:
            continue
        p = _first_lead_pick(samps, min_move_signal=min_move, min_size=min_size, floor=floor)
        if p is not None:
            table[r] = p

    trades: dict[int, tuple] = {}
    for e in trader_events:
        if e.get("type") != "entry":
            continue
        r = e.get("round")
        if r in official and r not in trades and isinstance(e.get("entry_price"), (int, float)):
            trades[r] = (e.get("side"), float(e["entry_price"]), e.get("confidence"))

    both = sorted(set(trades) & set(table))
    trader_only = sorted(set(trades) - set(table))
    table_only = sorted(set(table) - set(trades))

    def grade(rounds: list[int], pick: dict[int, tuple]) -> dict[str, Any]:
        recs = [(pick[r][0] == official[r], pick[r][1]) for r in rounds]
        n = len(recs)
        if not n:
            return {"n": 0}
        wr = sum(1 for w, _ in recs if w) / n
        ap = sum(p for _, p in recs) / n
        return {"n": n, "winrate": round(wr, 4), "avg_price": round(ap, 4),
                "net_ev_pct": round((wr - ap) / ap, 4) if ap else None}

    confs = [trades[r][2] for r in trader_only if trades[r][2] is not None]
    return {
        "floor": floor, "min_move": round(min_move, 6), "min_size": min_size,
        "n_trader_graded": len(trades), "n_table": len(table),
        "both": grade(both, trades),
        "trader_only": grade(trader_only, trades),
        "table_only": grade(table_only, table),
        "trader_only_avg_conf": round(sum(confs) / len(confs), 3) if confs else None,
    }


def _print(res: dict[str, Any]) -> None:
    import btc_entry_timing as _bet
    print("=" * 74)
    print(f"EXECUTOR vs TABLE  (lead {_bet.LEAD_WIN_LO:g}-{_bet.LEAD_WIN_HI:g}s ask <= "
          f"{_bet.LEAD_MAX_PRICE:g}, floor {res['floor']:g}, |move| >= {res['min_move']:g}, "
          f"size >= {res['min_size']:g}; official labels)")
    print("=" * 74)
    print(f"  trader entries graded: {res['n_trader_graded']}   "
          f"table-rule picks: {res['n_table']}")
    print(f"  {'subset':<14}{'n':<6}{'winrate':<10}{'avg price':<11}{'NET EV'}")
    for key, label in (("both", "BOTH"), ("trader_only", "TRADER-ONLY"),
                       ("table_only", "TABLE-ONLY")):
        g = res[key]
        if not g.get("n"):
            print(f"  {label:<14}0     (none)")
            continue
        print(f"  {label:<14}{g['n']:<6}{g['winrate']:<10.1%}{g['avg_price']:<11.3f}"
              f"{g['net_ev_pct']:+.1%}")
    if res.get("trader_only_avg_conf") is not None:
        print(f"  trader-only entries avg conf: {res['trader_only_avg_conf']:.2f} "
              f"(floor-grazing if barely above the floor)")
    print("-" * 74)
    print("  BOTH healthy + TRADER-ONLY bleeding  -> executor bug: it buys 2s")
    print("  threshold-grazes the 5s-sampled rule never counted. Fix: require the")
    print("  setup to HOLD across consecutive polls. BOTH bleeding too -> the")
    print("  table itself does not survive contact; the edge was an artifact.")
    print("=" * 74)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Join trader entries with combo-table picks by round")
    ap.add_argument("--log", default="out/trajectory.jsonl", help="Recorder log (table + official labels)")
    ap.add_argument("--trader-log", default="out/live.jsonl", help="Trader log (entries)")
    ap.add_argument("--floor", type=float, default=0.40)
    ap.add_argument("--min-size", type=float, default=0.0)
    ap.add_argument("--min-move", type=float, default=None)
    ap.add_argument("--lead-hi", type=float, default=None,
                    help="Lead window opens (seconds left) — set to the TRADER'S actual window, "
                         "or BOTH is empty by construction when configs diverge")
    ap.add_argument("--lead-lo", type=float, default=None, help="Lead window closes (seconds left)")
    ap.add_argument("--lead-max-price", type=float, default=None, help="Lead ask cap (trader's actual)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    # The table rule lives in btc_entry_timing's module globals; rebind them so
    # the table picks mirror the rule the trader actually ran.
    import btc_entry_timing as _bet
    if args.lead_hi is not None:
        _bet.LEAD_WIN_HI = float(args.lead_hi)
    if args.lead_lo is not None:
        _bet.LEAD_WIN_LO = float(args.lead_lo)
    if args.lead_max_price is not None:
        _bet.LEAD_MAX_PRICE = float(args.lead_max_price)

    res = executor_check(load(args.log), load(args.trader_log), floor=args.floor,
                         min_size=args.min_size, min_move=args.min_move)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    _print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
