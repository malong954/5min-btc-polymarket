#!/usr/bin/env python3
"""
Live color view for the trajectory recorder (scripts/btc_record.py).

The recorder places no trades; it samples the real Polymarket UP/DOWN asks and
the BTC move every few seconds through each 5m round. This dashboard tails that
log and redraws in place so you can WATCH the entry cost run up:

    250s left  move +40   buy UP  @ $0.80  ......
    190s left  move +55   buy UP  @ $0.88  ############
    130s left  move +70   buy UP  @ $0.94  ####################
     70s left  move +80   buy UP  @ $0.98  #########################

The whole point of the recorder is to find the cheapest still-profitable entry
TIME, so the panel also shows the accumulating entry-time verdict (win rate,
avg price, edge per offset) computed from every completed round so far — the
same table scripts/btc_record.py analyze prints, but updating live.

    python3 scripts/btc_record_monitor.py --log out/trajectory.jsonl

Pure stdlib + ANSI escapes. Reads incrementally (remembers its file offset).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional

from btc_entry_timing import analyze

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREY = "\033[90m"
CLEAR = "\033[H\033[J"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

RECENT_ROUNDS = 8
LADDER_ROWS = 10


def new_state() -> dict[str, Any]:
    return {
        "events": [],            # every event, for the live entry-time table
        "cur_round": None,       # current (open) round id
        "cur_samples": [],       # samples for the current round (build the ladder)
        "results": [],           # completed round results (newest last)
        "n_samples": 0,
    }


def fold_event(state: dict[str, Any], ev: dict[str, Any]) -> None:
    state["events"].append(ev)
    t = ev.get("type")
    if t == "sample":
        r = ev.get("round")
        if r != state["cur_round"]:
            state["cur_round"] = r
            state["cur_samples"] = []
        state["cur_samples"].append(ev)
        state["n_samples"] += 1
    elif t == "result":
        state["results"].append(ev)
        state["results"] = state["results"][-RECENT_ROUNDS:]


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def c(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + RESET


def _side_of(move: float) -> str:
    return "UP" if move > 0 else "DOWN" if move < 0 else "flat"


def render(state: dict[str, Any], p: Painter, log_path: str) -> str:
    lines: list[str] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    lines.append(p.c("  BTC 5m — Entry-Time Recorder ", BOLD, CYAN) + p.c(f" {now}", GREY)
                 + p.c("  OBSERVER (no trades)", GREY))
    lines.append(p.c("  " + "─" * 66, GREY))

    # ---- Current round: the price ladder climbing toward close ----
    samps = state["cur_samples"]
    if samps:
        last = samps[-1]
        mv = last.get("move", 0.0)
        side = _side_of(mv)
        side_col = GREEN if side == "UP" else RED if side == "DOWN" else GREY
        spot = last.get("spot")
        spot_s = f"${spot:,.2f}" if spot is not None else "—"
        sl = last.get("sec_left")
        sl_s = f"{sl:.0f}s left" if sl is not None else "—"
        lines.append(f"  round {p.c(str(state['cur_round']), BOLD)}   spot {p.c(spot_s, BOLD)}   "
                     f"move {p.c(f'{mv:+.0f}', side_col, BOLD)}   leading {p.c(side, side_col, BOLD)}   "
                     f"close in {p.c(sl_s, YELLOW)}")
        lines.append(p.c("  price ladder — cost of the leading side as the round runs down:", GREY))
        # Oldest (most sec_left) at top so the eye reads the run-up downward.
        ladder = sorted(samps, key=lambda s: -(s.get("sec_left") or 0))[:LADDER_ROWS]
        for s in ladder:
            slft = s.get("sec_left") or 0
            m = s.get("move", 0.0)
            sd = _side_of(m)
            col = GREEN if sd == "UP" else RED if sd == "DOWN" else GREY
            ask = s.get("up_ask") if sd == "UP" else s.get("dn_ask") if sd == "DOWN" else None
            ask_s = f"${ask:.2f}" if ask is not None else "  —  "
            bar = ""
            if ask is not None:
                bar = p.c("#" * max(0, min(28, int(ask * 28))), col)
            lines.append(f"   {slft:>4.0f}s  move {p.c(f'{m:+.0f}', col):>10}  buy {p.c(sd, col, BOLD):<12} "
                         f"@ {p.c(ask_s, BOLD)}  {bar}")
    else:
        lines.append(p.c("  waiting for the first in-window sample "
                         "(records at 30–280s before each close)…", GREY))

    lines.append(p.c("  " + "─" * 66, GREY))

    # ---- Live entry-time verdict: the whole reason we're recording ----
    rows = analyze(state["events"])
    n_done = len(set(e.get("round") for e in state["events"] if e.get("type") == "result"))
    lines.append(p.c(f"  entry-time verdict  ({n_done} completed round(s), "
                     f"{state['n_samples']} samples)", BOLD))
    if rows:
        lines.append(p.c(f"    {'sec left':<11}{'n':<5}{'winrate':<9}{'price':<8}{'edge':<9}EV/trade", GREY))
        best = max(rows, key=lambda r: r["edge"])
        for r in rows:
            edge = r["edge"]
            ecol = GREEN if edge > 0 else RED if edge < 0 else GREY
            star = p.c(" *", YELLOW, BOLD) if r is best else ""
            offset_s = str(r["offset"])
            wr_s = f"{r['winrate']:.0%}"
            price_s = f"{r['avg_price']:.2f}"
            edge_s = p.c(f"{edge:+.3f}", ecol, BOLD)
            ev_s = p.c(f"{r['ev_pct']:+.1%}", ecol)
            lines.append(f"    {offset_s:<11}{r['n']:<5}{wr_s:<9}"
                         f"{price_s:<8}{edge_s:<9}{ev_s}{star}")
        if best["edge"] > 0:
            lines.append(p.c(f"    best so far: {best['offset'][0]}-{best['offset'][1]}s left  "
                             f"@ ${best['avg_price']:.2f}  edge {best['edge']:+.3f}  "
                             f"(EV {best['ev_pct']:+.1%}/trade) — provisional", GREEN))
        else:
            lines.append(p.c("    every offset NEGATIVE so far → market efficiently priced "
                             "(no entry time is profitable)", RED))
    else:
        lines.append(p.c("    not enough completed rounds yet — needs ~100+ to judge.", GREY))

    lines.append(p.c("  " + "─" * 66, GREY))

    # ---- Recent completed rounds ----
    lines.append(p.c("  recent rounds (newest first)", BOLD))
    if state["results"]:
        for res in reversed(state["results"]):
            oc = res.get("outcome", "?")
            col = GREEN if oc == "UP" else RED if oc == "DOWN" else GREY
            op = res.get("open")
            cl = res.get("close")
            delta = (cl - op) if (op is not None and cl is not None) else None
            d_s = f"{delta:+.2f}" if delta is not None else "—"
            clk = time.strftime("%H:%M:%S", time.localtime(res.get("ts", 0)))
            lines.append(f"   {clk}  round {p.c(str(res.get('round')), BOLD)}  "
                         f"settled {p.c(oc, col, BOLD)}  (open {op} → close {cl}, Δ {d_s})")
    else:
        lines.append(p.c("   (no completed rounds yet)", GREY))

    lines.append(p.c("  " + "─" * 66, GREY))
    lines.append(p.c(f"  reading {log_path} — Ctrl-C to exit (recorder keeps running)", GREY))
    return "\n".join(lines) + "\n"


def read_new_events(path: str, offset: int) -> tuple[list[dict[str, Any]], int]:
    if not os.path.exists(path):
        return [], 0
    size = os.path.getsize(path)
    if size < offset:
        offset = 0
    events: list[dict[str, Any]] = []
    with open(path, "r") as f:
        f.seek(offset)
        chunk = f.read()
        offset = f.tell()
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events, offset


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live color view for the entry-time trajectory recorder")
    ap.add_argument("--log", default="out/trajectory.jsonl", help="Path to the recorder JSONL")
    ap.add_argument("--interval", type=float, default=2.0, help="Redraw interval (seconds)")
    ap.add_argument("--once", action="store_true", help="Render a single frame and exit")
    color_grp = ap.add_mutually_exclusive_group()
    color_grp.add_argument("--color", dest="color", action="store_true", default=None)
    color_grp.add_argument("--no-color", dest="color", action="store_false")
    args = ap.parse_args(argv)

    color = args.color
    if color is None:
        color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    painter = Painter(color)

    state = new_state()
    offset = 0

    def refresh() -> None:
        nonlocal offset
        evs, offset = read_new_events(args.log, offset)
        for ev in evs:
            fold_event(state, ev)

    if args.once:
        refresh()
        sys.stdout.write(render(state, painter, args.log))
        return 0

    if color:
        sys.stdout.write(HIDE_CURSOR)
    try:
        import shutil
        from btc_live_monitor import CLEAR_SCROLLBACK, fit_frame
        while True:
            refresh()
            term = shutil.get_terminal_size(fallback=(100, 40))
            frame = render(state, painter, args.log)
            sys.stdout.write(CLEAR + CLEAR_SCROLLBACK + fit_frame(frame, term.columns))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if color:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
