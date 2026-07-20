#!/usr/bin/env python3
"""Map the Limitless.exchange public API before wiring the arb monitor.

Answers, in order:
  1. Which list endpoint serves active markets, and what shape?
  2. Are there short-dated BTC markets (hourly/daily cadence)?
  3. Does the venue serve SEPARATE yes/no order books? This decides
     everything: complete-set arb (both asks summing < $1, the thing that
     printed 195 paper wins on Polymarket) is only structurally possible
     with separate books per outcome. A single shared book can never sum
     below $1 — the "arb" would just be the bid/ask spread.

Run on the Mac (the sandbox proxy blocks the host) and paste the output back:
  multi/scripts/multibot_ctl.sh limitless probe
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import limitless_api as ltl  # noqa: E402


def compact(obj, n: int = 380) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "…"


def main() -> int:
    print(f"=== LIMITLESS PROBE  base={ltl.BASE} ===")

    # --- 1. list endpoints -------------------------------------------------
    markets = ltl.list_markets()
    for a in ltl.ATTEMPTS:
        tag = a.get("status", a.get("error"))
        print(f"  list {a['path']} params={a.get('params')} -> {tag}")
    if not markets:
        print("✘ no list endpoint produced rows — paste this output back, the")
        print("  candidate paths in limitless_api.py need adjusting.")
        return 1
    print(f"✔ {len(markets)} markets via {ltl._working_list_variant}")
    print(f"  first market keys: {sorted(markets[0].keys())}")
    print(f"  sample: {compact(markets[0])}")

    # --- 2. short-dated BTC markets ---------------------------------------
    btc_all = [m for m in markets if ltl.is_btc(m)]
    watch = ltl.short_dated_btc(markets, horizon_sec=6 * 3600)
    print(f"\nBTC markets: {len(btc_all)} total, {len(watch)} open and ending "
          f"within 6h:")
    now = time.time()
    for m in watch[:12]:
        d = ltl.deadline_ts(m) or 0
        mins = (d - now) / 60
        amm = ltl.amm_prices(m)
        print(f"  [{mins:6.0f}min] {ltl.title_of(m)[:70]}")
        print(f"           slug={ltl.slug_of(m)} amm_mid={amm} "
              f"type={m.get('tradeType') or m.get('marketType') or m.get('type') or '?'}")
    if not watch:
        print("  (none — if BTC hourlies exist on the site but not here, the")
        print("   deadline/status field names need adjusting; see sample above)")

    # --- 3. the decisive question: separate yes/no books? ------------------
    probe_targets = watch[:3] or btc_all[:3] or markets[:3]
    verdicts = []
    for m in probe_targets:
        slug = ltl.slug_of(m)
        print(f"\n--- orderbook attempts for {slug} ({ltl.title_of(m)[:50]}) ---")
        n_before = len(ltl.ATTEMPTS)
        book = ltl.fetch_book(m)
        for a in ltl.ATTEMPTS[n_before:]:
            tag = a.get("status", a.get("error"))
            print(f"  GET {a['path']} -> {tag}")
        if book is None:
            print("  ✘ no orderbook variant matched (CLOB may use another host/path)")
            # detail endpoint sometimes reveals book location or embedded quotes
            for dp in (f"/markets/{slug}",):
                try:
                    r = ltl._get(dp)
                    j = r.json() if r.status_code == 200 else {}
                    print(f"  GET {dp} -> {r.status_code} keys="
                          f"{sorted(j.keys()) if isinstance(j, dict) else type(j).__name__}")
                    if isinstance(j, dict):
                        print(f"    sample: {compact(j)}")
                except Exception as e:
                    print(f"  GET {dp} -> {str(e)[:100]}")
            continue
        verdicts.append(book["two_sided"])
        print(f"  ✔ book via {ltl._working_ob_variant}  raw_keys={book['raw_keys']}")
        print(f"    two_sided={book['two_sided']}  "
              f"yes {book['yes_bid']}/{book['yes_ask']} (ask_sz {book['yes_ask_size']:.1f})  "
              f"no {book['no_bid']}/{book['no_ask']} (ask_sz {book['no_ask_size']:.1f})")
        if book["two_sided"] and book["yes_ask"] and book["no_ask"]:
            comb = book["yes_ask"] + book["no_ask"]
            print(f"    combined asks RIGHT NOW: {comb:.4f} "
                  f"({'CROSSED — free money on the book' if comb < 1 else 'above $1, no arb this tick'})")

    # --- verdict ------------------------------------------------------------
    print("\n=== VERDICT ===")
    if any(verdicts):
        print("✔ SEPARATE yes/no books confirmed — complete-set arb is structurally")
        print("  possible here. Start the monitor:")
        print("    multi/scripts/multibot_ctl.sh limitless start")
    elif verdicts:
        print("✘ single shared book — same-venue complete-set arb is impossible on")
        print("  this venue by construction (a book can't cross itself). The")
        print("  monitor will still measure spreads; the interesting variant")
        print("  becomes CROSS-venue vs Polymarket, which needs matching contracts.")
    else:
        print("? could not fetch any orderbook — paste this whole output back and")
        print("  the endpoint list gets adjusted to what the API really serves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
