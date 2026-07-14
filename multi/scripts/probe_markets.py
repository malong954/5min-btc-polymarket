#!/usr/bin/env python3
"""
Probe which Up/Down markets are resolvable right now for each configured
(asset, timeframe) pair. Run this in your trading environment BEFORE enabling
a timeframe live — it verifies which slug templates actually resolve on the
Gamma API today (Polymarket has changed slug formats over time).

Example:
  python multi/scripts/probe_markets.py
  python multi/scripts/probe_markets.py --assets btc,eth --timeframes 15m,1h,4h
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_discovery as md  # noqa: E402
from multi_worker import load_config, tf_params  # noqa: E402


def probe_pair(asset: str, tf: str, templates) -> list[dict]:
    rows = []
    for slot_name in ("current", "next"):
        if slot_name == "next":
            slot_start, slot_end = md.next_slot_bounds(tf)
        else:
            slot_start, slot_end = md.slot_bounds(tf)
        for slug in md.candidate_slugs(asset, tf, slot_start, templates):
            row = {"asset": asset, "tf": tf, "slot": slot_name, "slug": slug}
            try:
                ev = md.fetch_event(slug)
            except Exception as e:
                row.update(status="fetch_error", error=str(e)[:200])
                rows.append(row)
                continue
            if not ev:
                row["status"] = "not_found"
                rows.append(row)
                continue
            mkts = ev.get("markets") or []
            if not mkts:
                row["status"] = "no_markets"
                rows.append(row)
                continue
            m = md.validate_and_annotate(mkts[0], slug, slot_start)
            if m is None:
                row["status"] = "found_but_invalid(closed/inactive/ended)"
            else:
                row.update(status="OK",
                           seconds_left=round(m["_seconds_left"], 1),
                           up_price=m["up_price"], down_price=m["down_price"])
            rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="multi profiles yaml/json")
    ap.add_argument("--assets", default=None, help="comma list, overrides config")
    ap.add_argument("--timeframes", default=None, help="comma list, overrides config")
    args = ap.parse_args()

    cfg = load_config(args.config)
    assets = ([a.strip().lower() for a in args.assets.split(",")]
              if args.assets else [str(a).lower() for a in cfg.get("assets") or ["btc"]])
    tfs = ([t.strip() for t in args.timeframes.split(",")]
           if args.timeframes else sorted(md.TIMEFRAME_SECONDS,
                                          key=lambda t: md.TIMEFRAME_SECONDS[t]))

    all_rows = []
    ok_templates: dict[str, set] = {}
    for asset in assets:
        for tf in tfs:
            templates = tf_params(cfg, tf).get("slug_templates")
            rows = probe_pair(asset, tf, templates)
            all_rows.extend(rows)
            for r in rows:
                if r["status"] == "OK":
                    ok_templates.setdefault(f"{asset}/{tf}", set()).add(r["slug"])

    for r in all_rows:
        mark = "✔" if r["status"] == "OK" else "✘"
        extra = ""
        if r["status"] == "OK":
            extra = f" sec_left={r['seconds_left']} up={r['up_price']} down={r['down_price']}"
        print(f"{mark} [{r['asset']}/{r['tf']}/{r['slot']}] {r['slug']} -> {r['status']}{extra}")

    print()
    resolvable = sorted(ok_templates)
    unresolvable = sorted({f"{a}/{t}" for a in assets for t in tfs} - set(resolvable))
    summary = {"resolvable_pairs": resolvable, "unresolvable_pairs": unresolvable}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if unresolvable:
        print("\nFor unresolvable pairs: check the market page URL on polymarket.com "
              "and add its slug pattern under timeframes.<tf>.slug_templates in the config.",
              file=sys.stderr)
    return 0 if not unresolvable else 1


if __name__ == "__main__":
    raise SystemExit(main())
