#!/usr/bin/env python3
"""Limitless.exchange complete-set arb monitor (paper, measurement-only).

Same methodology as the Polymarket arb arm: watch short-dated BTC markets,
and when the YES ask + NO ask sum to <= --max-combined, record a paper clip
(both sides, locked PnL) and immediately re-check the book ~1.5s later —
the fill-echo probe. Echo persistence is the live-transferability metric:
Polymarket's arb showed a 100% paper win rate and 0% echo persistence,
i.e. a mirage. This monitor asks whether Limitless, a younger and less
botted venue, actually lets a slow bot keep those fills.

Writes to {runtime}/limitless/: state.json (counters) + clips.jsonl.

  limitless_arb.py --runtime-dir multi/runtime            # loop (via ctl)
  limitless_arb.py --runtime-dir multi/runtime --summary  # read the verdict
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import limitless_api as ltl  # noqa: E402

STOP = False


def _sig(_s, _f):
    global STOP
    STOP = True


def ts_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_state(p: Path, s: dict) -> None:
    s["updated_at"] = ts_utc()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(p)


def fresh_state() -> dict:
    return {"started_at": ts_utc(), "list_refreshes": 0, "list_errors": 0,
            "book_polls": 0, "book_fails": 0, "opportunity_ticks": 0,
            "best_combined": None, "min_single_spread": None,
            "clips": 0, "cost_sum": 0.0, "locked_sum": 0.0,
            "echo_pass": 0, "echo_fail": 0, "adverse_sum": 0.0, "adverse_n": 0,
            "markets_seen": {}}


def note_market(state: dict, slug: str, title: str, deadline: Optional[float],
                kind: str) -> None:
    seen = state["markets_seen"]
    cur = seen.get(slug) or {}
    rank = {"no_data": 0, "amm": 1, "single_book": 2, "two_sided": 3}
    if rank.get(kind, 0) >= rank.get(cur.get("kind"), -1):
        cur["kind"] = kind
    cur["title"] = title[:90]
    if deadline:
        cur["deadline"] = int(deadline)
    seen[slug] = cur
    if len(seen) > 300:  # keep the state file bounded
        oldest = sorted(seen.items(), key=lambda kv: kv[1].get("deadline") or 0)
        state["markets_seen"] = dict(oldest[-300:])


def do_clip(state: dict, clips_path: Path, m: dict, book: dict,
            stake: float, echo_delay: float) -> None:
    yes_ask, no_ask = book["yes_ask"], book["no_ask"]
    combined = yes_ask + no_ask
    pairs = min(book["yes_ask_size"] or 0, book["no_ask_size"] or 0,
                stake / combined)
    pairs = round(pairs, 6)
    if pairs <= 0:
        return
    cost = round(pairs * combined, 6)
    locked = round(pairs * (1.0 - combined), 6)

    time.sleep(echo_delay)
    after = ltl.fetch_book(m)
    persisted = False
    adverse = None
    if after and after.get("yes_ask") and after.get("no_ask"):
        adverse = round((after["yes_ask"] + after["no_ask"]) - combined, 4)
        persisted = (after["yes_ask"] <= yes_ask + 1e-9
                     and after["no_ask"] <= no_ask + 1e-9
                     and (after["yes_ask_size"] or 0) >= pairs
                     and (after["no_ask_size"] or 0) >= pairs)

    clip = {"ts": ts_utc(), "slug": ltl.slug_of(m), "title": ltl.title_of(m)[:90],
            "deadline": ltl.deadline_ts(m), "yes_ask": yes_ask, "no_ask": no_ask,
            "combined": round(combined, 4), "pairs": pairs, "cost_usdc": cost,
            "locked_pnl_usdc": locked,
            "echo": {"delay_sec": echo_delay, "persisted": persisted,
                     "adverse_move": adverse,
                     "yes_ask_after": (after or {}).get("yes_ask"),
                     "no_ask_after": (after or {}).get("no_ask")}}
    with clips_path.open("a") as f:
        f.write(json.dumps(clip) + "\n")

    state["clips"] += 1
    state["cost_sum"] = round(state["cost_sum"] + cost, 6)
    state["locked_sum"] = round(state["locked_sum"] + locked, 6)
    if persisted:
        state["echo_pass"] += 1
    else:
        state["echo_fail"] += 1
    if adverse is not None:
        state["adverse_sum"] = round(state["adverse_sum"] + adverse, 6)
        state["adverse_n"] += 1
    print(json.dumps({"ts": clip["ts"], "status": "clip", "slug": clip["slug"],
                      "combined": clip["combined"], "locked": locked,
                      "echo_persisted": persisted, "adverse": adverse}),
          flush=True)


def run(args) -> int:
    d = Path(args.runtime_dir) / "limitless"
    d.mkdir(parents=True, exist_ok=True)
    state_path, clips_path = d / "state.json", d / "clips.jsonl"
    state = load_state(state_path) or fresh_state()
    for k, v in fresh_state().items():  # forward-compat for added counters
        state.setdefault(k, v)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    print(json.dumps({"ts": ts_utc(), "status": "monitor_start",
                      "base": ltl.BASE, "max_combined": args.max_combined,
                      "stake_usd": args.stake_usd}), flush=True)

    markets: list[dict] = []
    last_refresh = 0.0
    last_clip_at: dict[str, float] = {}
    last_save = 0.0

    while not STOP:
        loop_t = time.time()
        if loop_t - last_refresh >= args.refresh_sec or not markets:
            try:
                markets = ltl.list_markets()
                state["list_refreshes"] += 1
                last_refresh = loop_t
            except Exception:
                state["list_errors"] += 1
            if not markets:
                state["list_errors"] += 1
                time.sleep(min(60, args.poll_sec * 5))
                continue

        watch = ltl.short_dated_btc(markets, horizon_sec=args.horizon_min * 60)[:12]
        for m in watch:
            if STOP:
                break
            slug = ltl.slug_of(m) or "?"
            title = ltl.title_of(m)
            deadline = ltl.deadline_ts(m)
            book = None
            try:
                book = ltl.fetch_book(m)
            except Exception:
                pass
            state["book_polls"] += 1
            if book is None:
                state["book_fails"] += 1
                kind = "amm" if ltl.amm_prices(m) else "no_data"
                note_market(state, slug, title, deadline, kind)
                continue
            if not book["two_sided"]:
                note_market(state, slug, title, deadline, "single_book")
                if book.get("yes_ask") and book.get("yes_bid"):
                    sp = round(book["yes_ask"] - book["yes_bid"], 4)
                    if state["min_single_spread"] is None or sp < state["min_single_spread"]:
                        state["min_single_spread"] = sp
                continue
            note_market(state, slug, title, deadline, "two_sided")
            if not (book.get("yes_ask") and book.get("no_ask")):
                continue
            combined = book["yes_ask"] + book["no_ask"]
            if state["best_combined"] is None or combined < state["best_combined"]:
                state["best_combined"] = round(combined, 4)
            if args.min_combined <= combined <= args.max_combined:
                state["opportunity_ticks"] += 1
                if loop_t - last_clip_at.get(slug, 0) >= args.clip_cooldown_sec:
                    last_clip_at[slug] = loop_t
                    do_clip(state, clips_path, m, book,
                            args.stake_usd, args.echo_delay_sec)

        if time.time() - last_save >= 15:
            save_state(state_path, state)
            last_save = time.time()
        if args.once:
            break
        elapsed = time.time() - loop_t
        time.sleep(max(0.5, args.poll_sec - elapsed))

    save_state(state_path, state)
    print(json.dumps({"ts": ts_utc(), "status": "monitor_stop"}), flush=True)
    return 0


def summarize(args) -> int:
    d = Path(args.runtime_dir) / "limitless"
    state = load_state(d / "state.json")
    if not state:
        print("no limitless state yet — start the monitor first:")
        print("  multi/scripts/multibot_ctl.sh limitless start")
        return 1
    kinds: dict[str, int] = {}
    for v in (state.get("markets_seen") or {}).values():
        kinds[v.get("kind") or "?"] = kinds.get(v.get("kind") or "?", 0) + 1
    clips, cost = state.get("clips", 0), state.get("cost_sum", 0.0)
    locked = state.get("locked_sum", 0.0)
    ep, ef = state.get("echo_pass", 0), state.get("echo_fail", 0)
    an, asum = state.get("adverse_n", 0), state.get("adverse_sum", 0.0)

    print(f"LIMITLESS ARB MONITOR — since {state.get('started_at')} "
          f"(updated {state.get('updated_at')})")
    print(f"  btc markets seen : {sum(kinds.values())}  {kinds}")
    print(f"  book polls       : {state.get('book_polls', 0)} "
          f"({state.get('book_fails', 0)} failed)")
    print(f"  best combined    : {state.get('best_combined')}")
    if state.get("min_single_spread") is not None:
        print(f"  min single-book spread: {state['min_single_spread']} "
              f"(single-book markets can't arb below $1 by construction)")
    print(f"  opportunity ticks (<= threshold): {state.get('opportunity_ticks', 0)}")
    print(f"  paper clips      : {clips}  capital cycled ${cost:,.2f}  "
          f"locked ${locked:,.2f}"
          + (f"  ({100 * locked / cost:.2f}% on cycled)" if cost else ""))
    if ep + ef:
        rate = 100 * ep / (ep + ef)
        print(f"  fill-echo        : {ep}/{ep + ef} persisted ({rate:.0f}%)"
              + (f", avg adverse move {100 * asum / an:+.2f}c" if an else ""))
        print()
        if rate >= 50:
            print("  VERDICT SO FAR: fills persist — unlike Polymarket (0%), this")
            print("  venue may actually pay a slow arber. Keep collecting before")
            print("  any live sizing decision.")
        else:
            print("  VERDICT SO FAR: same mirage as Polymarket — quotes vanish")
            print("  before a real order would land.")
    else:
        print("  fill-echo        : no clips yet")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-dir", default=str(Path(__file__).resolve().parents[1] / "runtime"))
    ap.add_argument("--poll-sec", type=float, default=3.0)
    ap.add_argument("--refresh-sec", type=float, default=300.0)
    ap.add_argument("--horizon-min", type=float, default=240.0)
    ap.add_argument("--stake-usd", type=float, default=5.0)
    ap.add_argument("--max-combined", type=float, default=0.99)
    ap.add_argument("--min-combined", type=float, default=0.50)
    ap.add_argument("--clip-cooldown-sec", type=float, default=120.0)
    ap.add_argument("--echo-delay-sec", type=float, default=1.5)
    ap.add_argument("--once", action="store_true", help="single pass (testing)")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    return summarize(args) if args.summary else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
