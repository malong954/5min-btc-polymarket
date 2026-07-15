#!/usr/bin/env python3
"""
Backfill settlement for reports stuck at result=settle_unresolved.

Early hold-to-resolution sessions polled gamma for only ~2 minutes; these
markets have long since resolved, so re-query gamma now (and fall back to
spot klines) and rewrite the reports with the real outcome and PnL. Only
touches analytics files — the portfolio guard's daily state is historical
and is left alone.

Run via: multi/scripts/multibot_ctl.sh settle
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_discovery as md  # noqa: E402
from multi_worker import resolve_outcome, spot_settle, ts_utc  # noqa: E402


def default_reports_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "runtime" / "reports")


def settle_report(path: Path, dry_run: bool) -> str:
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "unreadable"
    if rep.get("result") != "settle_unresolved":
        return "skip"
    opened = rep.get("opened") or {}
    slug, side = rep.get("slug"), opened.get("side")
    tf, slot_start = rep.get("timeframe"), rep.get("slot_start")
    if not (slug and side in ("UP", "DOWN") and tf and slot_start):
        return "missing_fields"
    end_ts = md._slot_end_ts(tf, int(slot_start))

    won, settled_px = resolve_outcome(slug, side, retries=2, delay=2.0)
    settle_source = "gamma" if won is not None else None
    spot_open_px = spot_close_px = None
    if won is None:
        up_won, spot_open_px, spot_close_px = spot_settle(
            rep.get("asset") or "btc", int(slot_start), end_ts)
        if up_won is not None:
            won = up_won if side == "UP" else (not up_won)
            settle_source = "spot_klines"
    if won is None:
        return "still_unresolved"

    shares = float(opened.get("shares") or 0)
    cost = float(opened.get("cost_usdc") or 0)
    proceeds = round(shares * (1.0 if won else 0.0), 6)
    pnl = round(proceeds - cost, 6)

    closed = rep.get("closed") or {}
    closed.update({
        "close_success": True,
        "won": won,
        "close_price": 1.0 if won else 0.0,
        "settle_source": settle_source,
        "settled_gamma_price": settled_px,
        "spot_open": spot_open_px,
        "spot_close": spot_close_px,
        "close_usdc": proceeds,
        "backfilled_at": ts_utc(),
    })
    rep["closed"] = closed
    rep["realized_cashflow_pnl_usdc"] = pnl
    rep["result"] = "done"
    if not dry_run:
        path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return f"settled won={won} pnl={pnl} via={settle_source}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports-dir", default=default_reports_dir())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rdir = Path(args.reports_dir)
    files = sorted(rdir.glob("*.json")) if rdir.exists() else []
    counts: dict[str, int] = {}
    for p in files:
        outcome = settle_report(p, args.dry_run)
        if outcome.startswith("settled"):
            print(f"{p.name}: {outcome}")
        counts[outcome.split(" ")[0]] = counts.get(outcome.split(" ")[0], 0) + 1
    print(json.dumps({"scanned": len(files), "outcomes": counts,
                      "dry_run": args.dry_run}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
