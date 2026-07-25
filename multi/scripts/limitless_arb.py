#!/usr/bin/env python3
"""Limitless.exchange arb monitor (paper, measurement-only).

Two experiments, same fill-echo methodology that exposed the Polymarket
paper-arb mirage (100% win rate, 0% of fills surviving a 1.5s re-check):

1. SAME-VENUE complete-set arb: only possible with separate yes/no books.
   Probed 2026-07-20: Limitless serves ONE shared book per market, so this
   path stays dormant (a book can't cross itself) — spreads are recorded.

2. CROSS-VENUE arb vs Polymarket — the live experiment. Limitless runs the
   same BTC Up/Down contracts (5m/15m/1h, slot-start-suffixed slugs) and
   resolves on the same Chainlink BTC/USD stream as Polymarket. Same slot +
   same oracle means buying opposite sides on the two venues locks $1:
     dir "pm_down+ltl_up": PM DOWN ask + LTL UP(yes) ask       <= threshold
     dir "pm_up+ltl_down": PM UP ask + (1 - LTL yes bid)       <= threshold
   (LTL down exposure = hitting the yes bid, since there is one book.)
   Caveats recorded, not modeled: taker fees, and the venues' tie rules
   (close == open) are assumed identical.

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
import market_discovery as md  # noqa: E402  (Polymarket side of cross-venue)

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
            "cross_slots_matched": 0, "pm_resolve_fails": 0,
            "cross_ticks": 0, "cross_opportunity_ticks": 0,
            "best_cross_combined": None,
            "cross_clips": 0, "cross_cost_sum": 0.0, "cross_locked_sum": 0.0,
            "cross_echo_pass": 0, "cross_echo_fail": 0,
            "cross_adverse_sum": 0.0, "cross_adverse_n": 0,
            "markets_seen": {}}


def resolve_pm(tf: str, slot_start: int) -> Optional[dict]:
    """Find the Polymarket market for the same (tf, slot_start) contract."""
    for slug in md.candidate_slugs("btc", tf, slot_start):
        try:
            ev = md.fetch_event(slug)
        except Exception:
            continue
        if not ev:
            continue
        mkts = ev.get("markets") or []
        if not mkts:
            continue
        m = md.validate_and_annotate(mkts[0], slug, slot_start, min_seconds_left=5.0)
        if m is not None:
            return m
    return None


def pm_market_for(pm_cache: dict, tf: str, slot_start: int,
                  state: dict) -> Optional[dict]:
    """Cache PM resolution per slot; retry failures every 45s."""
    key = f"{tf}|{slot_start}"
    ent = pm_cache.get(key)
    now = time.time()
    if ent and (ent["m"] is not None or now < ent["next_try"]):
        return ent["m"]
    m = resolve_pm(tf, slot_start)
    pm_cache[key] = {"m": m, "next_try": now + 45}
    if m is not None:
        state["cross_slots_matched"] += 1
    else:
        state["pm_resolve_fails"] += 1
    if len(pm_cache) > 60:
        for k in sorted(pm_cache)[: len(pm_cache) - 60]:
            pm_cache.pop(k, None)
    return m


def cross_dirs(pm_up: tuple, pm_dn: tuple, book: dict) -> list[dict]:
    """Both cross-venue directions with combined cost and available depth."""
    out = []
    _, up_ask, _, up_asz = pm_up
    _, dn_ask, _, dn_asz = pm_dn
    if dn_ask is not None and book.get("yes_ask"):
        out.append({"direction": "pm_down+ltl_up",
                    "combined": dn_ask + book["yes_ask"],
                    "pm_side": "DOWN", "pm_ask": dn_ask, "pm_depth": dn_asz,
                    "ltl_cost": book["yes_ask"],
                    "ltl_depth": book.get("yes_ask_size") or 0})
    if up_ask is not None and book.get("yes_bid"):
        out.append({"direction": "pm_up+ltl_down",
                    "combined": up_ask + (1.0 - book["yes_bid"]),
                    "pm_side": "UP", "pm_ask": up_ask, "pm_depth": up_asz,
                    "ltl_cost": round(1.0 - book["yes_bid"], 4),
                    "ltl_depth": book.get("yes_bid_size") or 0})
    return out


def do_cross_clip(state: dict, clips_path: Path, m: dict, pm: dict, d: dict,
                  stake: float, echo_delay: float) -> None:
    combined = d["combined"]
    pairs = round(min(d["pm_depth"] or 0, d["ltl_depth"] or 0,
                      stake / combined), 6)
    if pairs <= 0:
        return
    cost = round(pairs * combined, 6)
    locked = round(pairs * (1.0 - combined), 6)

    time.sleep(echo_delay)
    persisted = False
    adverse = None
    pm_ask_after = ltl_cost_after = None
    try:
        token = pm["down_token"] if d["pm_side"] == "DOWN" else pm["up_token"]
        _, pm_ask_after, _, pm_asz_after = md.top_of_book(token)
    except Exception:
        pm_asz_after = 0
    after = None
    try:
        after = ltl.fetch_book(m)
    except Exception:
        pass
    if after:
        if d["direction"] == "pm_down+ltl_up":
            ltl_cost_after = after.get("yes_ask")
            ltl_depth_after = after.get("yes_ask_size") or 0
        else:
            b = after.get("yes_bid")
            ltl_cost_after = round(1.0 - b, 4) if b is not None else None
            ltl_depth_after = after.get("yes_bid_size") or 0
        if pm_ask_after is not None and ltl_cost_after is not None:
            adverse = round((pm_ask_after + ltl_cost_after) - combined, 4)
            persisted = (pm_ask_after <= d["pm_ask"] + 1e-9
                         and ltl_cost_after <= d["ltl_cost"] + 1e-9
                         and (pm_asz_after or 0) >= pairs
                         and ltl_depth_after >= pairs)

    clip = {"ts": ts_utc(), "kind": "cross", "direction": d["direction"],
            "slug": ltl.slug_of(m), "pm_slug": pm.get("_slug"),
            "title": ltl.title_of(m)[:90], "deadline": ltl.deadline_ts(m),
            "combined": round(combined, 4), "pairs": pairs,
            "cost_usdc": cost, "locked_pnl_usdc": locked,
            "legs": {"pm": {"side": d["pm_side"], "ask": d["pm_ask"]},
                     "ltl": {"cost": d["ltl_cost"]}},
            "echo": {"delay_sec": echo_delay, "persisted": persisted,
                     "adverse_move": adverse, "pm_ask_after": pm_ask_after,
                     "ltl_cost_after": ltl_cost_after}}
    with clips_path.open("a") as f:
        f.write(json.dumps(clip) + "\n")

    state["cross_clips"] += 1
    state["cross_cost_sum"] = round(state["cross_cost_sum"] + cost, 6)
    state["cross_locked_sum"] = round(state["cross_locked_sum"] + locked, 6)
    if persisted:
        state["cross_echo_pass"] += 1
    else:
        state["cross_echo_fail"] += 1
    if adverse is not None:
        state["cross_adverse_sum"] = round(state["cross_adverse_sum"] + adverse, 6)
        state["cross_adverse_n"] += 1
    print(json.dumps({"ts": clip["ts"], "status": "cross_clip",
                      "direction": d["direction"], "slug": clip["slug"],
                      "combined": clip["combined"], "locked": locked,
                      "echo_persisted": persisted, "adverse": adverse}),
          flush=True)


def cross_check(state: dict, clips_path: Path, m: dict, book: dict, args,
                pm_cache: dict, last_clip_at: dict) -> None:
    slot = ltl.slot_of_slug(ltl.slug_of(m))
    if not slot:
        return
    pm = pm_market_for(pm_cache, slot["tf"], slot["slot_start"], state)
    if pm is None:
        return
    try:
        pm_up = md.top_of_book(pm["up_token"])
        pm_dn = md.top_of_book(pm["down_token"])
    except Exception:
        return
    state["cross_ticks"] += 1
    for d in cross_dirs(pm_up, pm_dn, book):
        if state["best_cross_combined"] is None or d["combined"] < state["best_cross_combined"]:
            state["best_cross_combined"] = round(d["combined"], 4)
        if args.min_combined <= d["combined"] <= args.max_combined:
            state["cross_opportunity_ticks"] += 1
            key = f"{ltl.slug_of(m)}|{d['direction']}"
            if time.time() - last_clip_at.get(key, 0) >= args.clip_cooldown_sec:
                last_clip_at[key] = time.time()
                do_cross_clip(state, clips_path, m, pm, d,
                              args.stake_usd, args.echo_delay_sec)


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
    pm_cache: dict[str, dict] = {}
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
            else:
                note_market(state, slug, title, deadline, "two_sided")

            # cross-venue vs Polymarket works with either book structure
            if not args.no_cross:
                try:
                    cross_check(state, clips_path, m, book, args,
                                pm_cache, last_clip_at)
                except Exception:
                    pass

            # same-venue complete-set path needs separate yes/no books
            if not book["two_sided"]:
                continue
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
    else:
        print("  fill-echo        : no clips yet")

    print()
    print("CROSS-VENUE (Polymarket x Limitless — same slot, same Chainlink oracle):")
    print(f"  slots matched    : {state.get('cross_slots_matched', 0)} "
          f"(pm resolve fails {state.get('pm_resolve_fails', 0)})")
    print(f"  paired ticks     : {state.get('cross_ticks', 0)}   "
          f"best combined: {state.get('best_cross_combined')}")
    print(f"  opportunity ticks (<= threshold): {state.get('cross_opportunity_ticks', 0)}")
    cclips = state.get("cross_clips", 0)
    ccost, clocked = state.get("cross_cost_sum", 0.0), state.get("cross_locked_sum", 0.0)
    print(f"  paper clips      : {cclips}  capital cycled ${ccost:,.2f}  "
          f"locked ${clocked:,.2f}"
          + (f"  ({100 * clocked / ccost:.2f}% on cycled)" if ccost else ""))
    cep, cef = state.get("cross_echo_pass", 0), state.get("cross_echo_fail", 0)
    can, casum = state.get("cross_adverse_n", 0), state.get("cross_adverse_sum", 0.0)
    if cep + cef:
        crate = 100 * cep / (cep + cef)
        print(f"  fill-echo        : {cep}/{cep + cef} persisted ({crate:.0f}%)"
              + (f", avg adverse move {100 * casum / can:+.2f}c" if can else ""))
        print()
        if crate >= 50:
            print("  VERDICT SO FAR: cross-venue quotes persist — a slow bot may")
            print("  actually capture this. Before ANY live sizing: model taker")
            print("  fees on both venues and verify the tie rule matches.")
        else:
            print("  VERDICT SO FAR: cross-venue quotes also vanish before a real")
            print("  order lands — same latency race, two venues.")
    else:
        print("  fill-echo        : no cross clips yet")
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
    ap.add_argument("--no-cross", action="store_true",
                    help="disable the cross-venue (vs Polymarket) experiment")
    ap.add_argument("--once", action="store_true", help="single pass (testing)")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    return summarize(args) if args.summary else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
