#!/usr/bin/env python3
"""
Cohort scanner: batch-analyze a list of Polymarket wallets (e.g. a top-100
Up/Down leaderboard CSV), classify each into a strategy species, aggregate
the cohort, and compare head-to-head against our own bot's paper results.

Usage (in your trading environment):
  multi/scripts/multibot_ctl.sh cohort --csv top100.csv
  multi/scripts/multibot_ctl.sh cohort --wallets 0xabc,0xdef

CSV format: any file with a header row containing a 'wallet' column
(extra columns like rank/name/copyability are carried through).

Outputs:
  multi/runtime/cohort_scan.json   full per-wallet results (resumable cache)
  multi/runtime/cohort_rows.csv    one classified row per wallet
  stdout                           cohort summary + ours-vs-them comparison
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_discovery as md  # noqa: E402
from analyze_wallet import fetch_activity, normalize, analyze  # noqa: E402

SPECIES = ["favorite_early", "favorite_late", "underdog", "farmer",
           "scalper", "mixed", "inactive", "error"]


def median(vals: list) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


# ---------------------------------------------------------------------------
# Per-wallet classification
# ---------------------------------------------------------------------------

def species_from_fields(both_frac: float, clips: float, vol: float, pnl: float,
                        sold_frac: float, med_px: Optional[float],
                        pos: Optional[float]) -> str:
    """Species rules over the reduced per-wallet fields (also used by
    --reclassify against the cached rows, so keep it field-driven)."""
    # MM/farmer: heavy clip count + near-flat cashflow. True arb needs ~2
    # clips per market; dozens at breakeven is reward farming even when one
    # timeframe's book is patchier (<90% both-sides).
    if both_frac > 0.6 and clips >= 8 and vol > 0 and abs(pnl) / vol < 0.02:
        return "farmer"
    if sold_frac > 0.5:
        return "scalper"
    if med_px is not None and med_px <= 0.35:
        return "underdog"
    if med_px is not None and med_px >= 0.55:
        return "favorite_late" if (pos or 0) >= 60 else "favorite_early"
    return "mixed"


def classify(res: dict) -> dict:
    """Reduce one analyze() result to a classified summary row."""
    tfs = res.get("by_timeframe") or {}
    mkts = sum(g["markets"] for g in tfs.values())
    buys = sum(g["buys"] for g in tfs.values())
    vol = sum(g["volume_usdc"] for g in tfs.values())
    pnl = float(res.get("cash_pnl_total_usdc") or 0)
    if mkts == 0 or buys == 0:
        return {"species": "inactive"}

    both = sum(g["both_sides_markets"] for g in tfs.values())
    sold = sum(g["sold_before_close_markets"] for g in tfs.values())
    resolved = sum(g["resolved_markets"] for g in tfs.values())
    wins = sum(g["win_markets"] for g in tfs.values())
    clips = buys / mkts

    primary_tf = max(tfs, key=lambda t: tfs[t]["buys"])
    p = tfs[primary_tf]
    med_px = p.get("median_buy_price")
    pos = p.get("entry_position_in_window_pct")
    win_rate = wins / resolved if resolved else None
    alpha = (win_rate - med_px) if (win_rate is not None and med_px is not None) else None

    species = species_from_fields(both / mkts, clips, vol, pnl,
                                  sold / mkts, med_px, pos)

    return {
        "species": species,
        "primary_tf": primary_tf,
        "markets": mkts,
        "buys": buys,
        "clips_per_market": round(clips, 1),
        "volume_usdc": round(vol, 2),
        "median_buy_price": med_px,
        "entry_position_pct": pos,
        "entry_secs_before_close_median": (p.get("entry_secs_before_close") or {}).get("median"),
        "win_rate_resolved": round(win_rate, 3) if win_rate is not None else None,
        "alpha_per_share": round(alpha, 3) if alpha is not None else None,
        "both_sides_frac": round(both / mkts, 3),
        "sold_before_close_frac": round(sold / mkts, 3),
        "cash_pnl_usdc": round(pnl, 2),
        "pnl_per_market": round(pnl / mkts, 4),
    }


# ---------------------------------------------------------------------------
# Our side: aggregate paper results per strategy from runtime reports
# ---------------------------------------------------------------------------

def our_stats(reports_dir: Path) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "entries": 0, "wins": 0, "losses": 0, "pnl": 0.0, "pnl_known": 0,
        "entry_px": [], "entry_pos": []})
    if not reports_dir.exists():
        return []
    for f in reports_dir.glob("*.json"):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if r.get("mode") != "paper":
            continue
        opened = r.get("opened") or {}
        if not opened:
            continue
        tf = str(r.get("timeframe") or "?")
        strat = str(r.get("strategy") or "favorite")
        g = groups[(strat, tf)]
        g["entries"] += 1
        if opened.get("entry_price") is not None:
            g["entry_px"].append(float(opened["entry_price"]))
        try:
            slot_start = int(r["slot_start"])
            dur = md.timeframe_seconds(tf)
            ots = dt.datetime.fromisoformat(
                str(opened.get("opened_at")).replace("Z", "+00:00")).timestamp()
            pos = 100.0 * (ots - slot_start) / dur
            if 0.0 <= pos <= 100.0:  # drop reports with inconsistent stamps
                g["entry_pos"].append(pos)
        except Exception:
            pass
        pnl = r.get("realized_cashflow_pnl_usdc")
        if isinstance(pnl, (int, float)):
            g["pnl"] += pnl
            g["pnl_known"] += 1
            if pnl > 0:
                g["wins"] += 1
            elif pnl < 0:
                g["losses"] += 1
    out = []
    for (strat, tf), g in sorted(groups.items()):
        decided = g["wins"] + g["losses"]
        med_px = median(g["entry_px"])
        wr = g["wins"] / decided if decided else None
        out.append({
            "strategy": strat, "tf": tf,
            "entries": g["entries"],
            "median_entry_price": round(med_px, 3) if med_px is not None else None,
            "entry_position_pct": round(median(g["entry_pos"]), 1) if g["entry_pos"] else None,
            "win_rate": round(wr, 3) if wr is not None else None,
            "alpha_per_share": (round(wr - med_px, 3)
                                if wr is not None and med_px is not None else None),
            "pnl_sum_usdc": round(g["pnl"], 4),
            "pnl_per_trade": round(g["pnl"] / g["pnl_known"], 4) if g["pnl_known"] else None,
        })
    return out


# ---------------------------------------------------------------------------

def fmt(v, spec="{:.3f}", dash="-") -> str:
    if v is None:
        return dash
    try:
        return spec.format(v)
    except (TypeError, ValueError):
        return str(v)


def print_summary(rows: list[dict], ours: list[dict]) -> None:
    by_species: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_species[r.get("species") or "error"].append(r)

    print("\n=== COHORT BY SPECIES ===")
    hdr = (f"{'species':<15} {'wallets':>7} {'buys':>7} {'med_px':>7} "
           f"{'pos%':>6} {'winrate':>8} {'alpha':>7} {'pnl_sum':>11} {'pnl/mkt':>8}")
    print(hdr)
    print("-" * len(hdr))
    for sp in SPECIES:
        rs = by_species.get(sp)
        if not rs:
            continue
        n = len(rs)
        buys = sum(r.get("buys") or 0 for r in rs)
        pnl = sum(r.get("cash_pnl_usdc") or 0 for r in rs)
        print(f"{sp:<15} {n:>7} {buys:>7} "
              f"{fmt(median([r.get('median_buy_price') for r in rs])):>7} "
              f"{fmt(median([r.get('entry_position_pct') for r in rs]), '{:.0f}'):>6} "
              f"{fmt(median([r.get('win_rate_resolved') for r in rs]), '{:.1%}'):>8} "
              f"{fmt(median([r.get('alpha_per_share') for r in rs]), '{:+.3f}'):>7} "
              f"{pnl:>11.2f} "
              f"{fmt(median([r.get('pnl_per_market') for r in rs]), '{:+.3f}'):>8}")

    print("\n=== OUR PAPER RESULTS (per strategy) ===")
    hdr = (f"{'strategy':<10} {'tf':<5} {'entries':>7} {'med_px':>7} {'pos%':>6} "
           f"{'winrate':>8} {'alpha':>7} {'pnl_sum':>10} {'pnl/trade':>10}")
    print(hdr)
    print("-" * len(hdr))
    for o in ours:
        print(f"{o['strategy']:<10} {o['tf']:<5} {o['entries']:>7} "
              f"{fmt(o['median_entry_price']):>7} "
              f"{fmt(o['entry_position_pct'], '{:.0f}'):>6} "
              f"{fmt(o['win_rate'], '{:.1%}'):>8} "
              f"{fmt(o['alpha_per_share'], '{:+.3f}'):>7} "
              f"{o['pnl_sum_usdc']:>10.4f} "
              f"{fmt(o['pnl_per_trade'], '{:+.4f}'):>10}")

    print("\n=== HEAD-TO-HEAD (them vs us) ===")
    pairs = [("favorite_early", "impulse"), ("favorite_late", "favorite"),
             ("underdog", "underdog")]
    for sp, strat in pairs:
        rs = by_species.get(sp) or []
        os_ = [o for o in ours if o["strategy"] == strat]
        them = (f"{len(rs)} wallets, med_px {fmt(median([r.get('median_buy_price') for r in rs]))}, "
                f"win {fmt(median([r.get('win_rate_resolved') for r in rs]), '{:.1%}')}, "
                f"alpha {fmt(median([r.get('alpha_per_share') for r in rs]), '{:+.3f}')}"
                if rs else "no wallets")
        print(f"\n{sp} (them): {them}")
        if os_:
            for o in os_:
                print(f"  {strat} {o['tf']} (us): {o['entries']} entries, "
                      f"med_px {fmt(o['median_entry_price'])}, "
                      f"win {fmt(o['win_rate'], '{:.1%}')}, "
                      f"alpha {fmt(o['alpha_per_share'], '{:+.3f}')}, "
                      f"pnl/trade {fmt(o['pnl_per_trade'], '{:+.4f}')}")
        else:
            print(f"  {strat} (us): no paper entries yet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="leaderboard csv with a 'wallet' column")
    ap.add_argument("--wallets", default=None, help="comma-separated wallet list")
    ap.add_argument("--max-pages", type=int, default=4,
                    help="pages per wallet (500 events each); 4 covers most bots")
    ap.add_argument("--out", default=None,
                    help="output prefix (default multi/runtime/cohort_scan)")
    ap.add_argument("--reports-dir",
                    default=str(Path(__file__).resolve().parents[1] / "runtime" / "reports"))
    ap.add_argument("--rescan", action="store_true",
                    help="ignore cached per-wallet results")
    ap.add_argument("--reclassify", action="store_true",
                    help="re-derive species from the cached scan (no API "
                         "calls) — use after classifier rule updates")
    args = ap.parse_args()

    targets: list[dict] = []
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                w = (row.get("wallet") or "").strip().lower()
                if w.startswith("0x"):
                    targets.append({"wallet": w,
                                    "name": row.get("name"),
                                    "rank": row.get("rank"),
                                    "copyability": row.get("copyability"),
                                    "trades_2w": row.get("trades_2w"),
                                    "win_rate_2w": row.get("win_rate_2w")})
    if args.wallets:
        for w in args.wallets.split(","):
            w = w.strip().lower()
            if w.startswith("0x"):
                targets.append({"wallet": w})
    # dedupe, keep order
    seen = set()
    targets = [t for t in targets
               if not (t["wallet"] in seen or seen.add(t["wallet"]))]
    if not targets:
        ap.error("no wallets found — pass --csv or --wallets")
        return 2

    out_prefix = args.out or str(Path(args.reports_dir).parent / "cohort_scan")
    cache_path = Path(out_prefix + ".json")
    cache: dict[str, dict] = {}
    if cache_path.exists() and not args.rescan:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    rows: list[dict] = []
    for i, t in enumerate(targets, 1):
        w = t["wallet"]
        if w in cache:
            row = cache[w]
            if args.reclassify and row.get("species") not in ("inactive", "error"):
                try:
                    row["species"] = species_from_fields(
                        float(row.get("both_sides_frac") or 0),
                        float(row.get("clips_per_market") or 0),
                        float(row.get("volume_usdc") or 0),
                        float(row.get("cash_pnl_usdc") or 0),
                        float(row.get("sold_before_close_frac") or 0),
                        row.get("median_buy_price"),
                        row.get("entry_position_pct"))
                    cache[w] = row
                except (TypeError, ValueError):
                    pass
        else:
            print(f"[{i}/{len(targets)}] scanning {w} ...", file=sys.stderr)
            try:
                raw = fetch_activity(w, max_pages=args.max_pages)
                res = analyze(normalize(raw))
                row = classify(res)
                row["events_fetched"] = len(raw)
            except Exception as e:
                row = {"species": "error", "error": str(e)[:200]}
            cache[w] = row
            cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                                  encoding="utf-8")
            time.sleep(0.2)
        rows.append({**t, **row})

    if args.reclassify:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False),
                              encoding="utf-8")

    csv_path = Path(out_prefix + "_rows.csv")
    fields = ["rank", "name", "wallet", "species", "primary_tf", "markets",
              "buys", "clips_per_market", "median_buy_price",
              "entry_position_pct", "entry_secs_before_close_median",
              "win_rate_resolved", "alpha_per_share", "both_sides_frac",
              "sold_before_close_frac", "volume_usdc", "cash_pnl_usdc",
              "pnl_per_market", "copyability", "trades_2w", "win_rate_2w",
              "events_fetched", "error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        wtr.writeheader()
        wtr.writerows(rows)

    ours = our_stats(Path(args.reports_dir))
    print_summary(rows, ours)
    print(f"\nwrote {csv_path} and {cache_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
