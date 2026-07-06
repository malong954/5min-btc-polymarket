#!/usr/bin/env python3
"""
Live terminal dashboard for the paper trader's JSONL stream.

Tails out/live.jsonl (written by btc_live_paper.py) and redraws a color panel
in place: current price + movement prediction, cumulative PnL (green profit /
red loss), win-rate vs breakeven, and a colored recent-trades list
(green wins, red losses).

    python3 scripts/btc_live_monitor.py --log out/live.jsonl

Reads incrementally (remembers its file offset), so it stays cheap even when the
log grows to hundreds of thousands of lines. Pure stdlib + ANSI escapes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREY = "\033[90m"
CLEAR = "\033[H\033[J"  # cursor home + clear to end
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

RECENT_N = 12


ACTIVITY_MAX = 5000  # cap the in-memory live feed (the full log stays on disk)


def new_state(bankroll: float = 100.0, stake_usd: float = 10.0, entry_price: float = 0.85,
              entry_threshold: float = 0.60, recent_n: int = RECENT_N) -> dict[str, Any]:
    return {
        "trades": 0, "wins": 0, "pnl": 0.0, "pnl_usd": 0.0,
        "bankroll": bankroll, "stake_usd": stake_usd, "entry_price": entry_price,
        "entry_threshold": entry_threshold, "recent_n": recent_n,
        "balance": bankroll, "peak_bal": bankroll, "max_dd_usd": 0.0,
        "entry_sum": 0.0, "entry_n": 0,  # realized entry prices (real varies per trade)
        "last_stake": stake_usd,          # most recent actual stake (grows with balance)
        "skips_since_trade": 0, "last_skip": None,
        "total_entries": 0, "total_wins": 0, "total_losses": 0,
        "total_skips": 0, "total_decided": 0,   # cumulative: skipped vs decided rounds
        "last_hb": None, "last_pred": None, "last_entry": None,
        "open_positions": {}, "recent_entries": [],  # entered, awaiting close
        "activity": [],                              # unified feed: entry/skip/settle
        "recent": [], "peak_pnl": 0.0, "max_drawdown": 0.0,
    }


def _push_activity(state: dict[str, Any], ev: dict[str, Any]) -> None:
    state["activity"].append(ev)
    if len(state["activity"]) > ACTIVITY_MAX:
        state["activity"] = state["activity"][-ACTIVITY_MAX:]


def _unit_to_usd(unit_pnl: float, stake_usd: float, entry_price: float) -> float:
    # shares bought = stake/entry; per-share pnl is unit_pnl -> dollars = unit_pnl*shares.
    return unit_pnl * stake_usd / entry_price if entry_price else 0.0


def fold_event(state: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    t = ev.get("type")
    if t == "heartbeat":
        state["last_hb"] = ev
    elif t == "prediction":
        state["last_pred"] = ev
        state["total_decided"] += 1          # every round we made a call on
    elif t == "skip":
        state["skips_since_trade"] += 1
        state["total_skips"] += 1
        state["last_skip"] = ev
        _push_activity(state, ev)
    elif t == "entry":
        state["last_entry"] = ev
        state["skips_since_trade"] = 0
        state["total_entries"] += 1
        rnd = ev.get("round")
        if rnd is not None:
            state["open_positions"][rnd] = ev   # live until its round settles
        state["recent_entries"].append(ev)
        state["recent_entries"] = state["recent_entries"][-RECENT_N:]
        _push_activity(state, ev)
    elif t == "settle":
        state["trades"] += 1
        won = ev.get("result") == "win"
        state["wins"] += 1 if won else 0
        state["total_wins"] += 1 if won else 0
        state["total_losses"] += 0 if won else 1
        state["pnl"] += float(ev.get("pnl", 0.0))
        # Prefer the trader's own dollar figure; else derive from the unit pnl so
        # the account view works even on logs written before dollars were added.
        if "pnl_usd" in ev:
            pnl_usd = float(ev["pnl_usd"])
        else:
            pnl_usd = _unit_to_usd(float(ev.get("pnl", 0.0)), state["stake_usd"], state["entry_price"])
        state["pnl_usd"] += pnl_usd
        state["balance"] = state["bankroll"] + state["pnl_usd"]
        if ev.get("entry_price") is not None:
            state["entry_sum"] += float(ev["entry_price"])
            state["entry_n"] += 1
        if ev.get("stake_usd") is not None:
            state["last_stake"] = float(ev["stake_usd"])
        state["peak_pnl"] = max(state["peak_pnl"], state["pnl"])
        state["max_drawdown"] = min(state["max_drawdown"], state["pnl"] - state["peak_pnl"])
        state["peak_bal"] = max(state["peak_bal"], state["balance"])
        state["max_dd_usd"] = min(state["max_dd_usd"], state["balance"] - state["peak_bal"])
        # Attach a derived dollar figure so the recent-trades rows can show it.
        ev = dict(ev)
        ev.setdefault("pnl_usd", round(pnl_usd, 2))
        # Always use the monitor's own cumulative balance for the row — never the
        # trader-emitted one. A trader RESTART resets its balance to the bankroll,
        # which otherwise makes the newest row jump (e.g. $64 -> $100) while the
        # account panel stays correct. Monitor-cumulative keeps them in sync.
        ev["balance"] = round(state["balance"], 2)
        state["open_positions"].pop(ev.get("round"), None)  # closed -> no longer open
        state["recent"].append(ev)
        state["recent"] = state["recent"][-RECENT_N:]
        _push_activity(state, ev)
    return state


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def c(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + RESET

    def money(self, v: float, width: int = 0, plus: bool = True) -> str:
        s = f"{v:+.3f}" if plus else f"{v:.3f}"
        if width:
            s = s.rjust(width)
        return self.c(s, GREEN if v > 0 else RED if v < 0 else GREY, BOLD)


def _activity_row(ev: dict[str, Any], p: Painter) -> str:
    """One timeline line for an entry / skip / settle event (newest-first feed
    and the full --history dump share this format)."""
    clk = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
    t = ev.get("type")
    if t == "entry":
        side = ev.get("side", "?")
        col = GREEN if side == "UP" else RED
        ep = ev.get("entry_price")
        cost = f"@{ep:.2f}" if ep is not None else "  -   "
        conf = ev.get("confidence")
        conf_s = f"conf {conf:.2f}" if conf is not None else ""
        stake = ev.get("stake_usd")
        stake_s = f"${stake:.2f}" if stake is not None else ""
        big = p.c(" BIG", YELLOW, BOLD) if ev.get("big_bet") else ""
        note = ev.get("note")
        note_s = p.c(f"  [{note}]", GREY) if note else ""
        return (f"   {clk}  {p.c('ENTER', CYAN, BOLD)} {p.c(f'{side:<5}', col, BOLD)} "
                f"{cost:<7}{stake_s:<8}{conf_s}{big}{note_s}")
    if t == "skip":
        reason = ev.get("reason", "") or "below_threshold"
        conf = ev.get("confidence")
        conf_s = f"conf {conf:.2f}" if conf is not None else "conf  -  "  # show conf even on skips
        note = ev.get("note")
        note_s = p.c(f"  [{note}]", GREY) if note else ""   # why: high RSI, hidden divergence, ...
        return (f"   {clk}  {p.c('SKIP ', GREY, BOLD)} {p.c(f'{reason:<16}', GREY)} "
                f"{p.c(conf_s, GREY)}{note_s}")
    # settle
    win = ev.get("result") == "win"
    col = GREEN if win else RED
    tag = "WIN " if win else "LOSS"
    side = ev.get("side", "?")
    act = ev.get("actual", "?")
    pair = f"{side}->{act}"
    ep = ev.get("entry_price")
    cost = f"@{ep:.2f}" if ep is not None else "  -   "
    pnl_usd = float(ev.get("pnl_usd", 0.0))
    bal = ev.get("balance")
    bal_s = f"bal ${bal:,.2f}" if bal is not None else ""
    pnl_str = f"${pnl_usd:+,.2f}"
    return (f"   {clk}  {p.c(tag, col, BOLD)}  {pair:<12}{cost:<7}"
            f"{p.c(f'{pnl_str:<8}', col, BOLD)} {bal_s}")


def render(state: dict[str, Any], entry_price: float, p: Painter) -> str:
    hb = state["last_hb"] or {}
    pred = state["last_pred"] or {}
    trades = state["trades"]
    wins = state["wins"]
    losses = trades - wins
    wr = (wins / trades) if trades else 0.0
    # Breakeven = the entry price. Prefer the REALIZED average entry price when
    # trades carry real Polymarket prices; otherwise the fixed reference.
    be = (state["entry_sum"] / state["entry_n"]) if state.get("entry_n") else entry_price
    pnl = state["pnl"]

    lines: list[str] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    lines.append(p.c("  BTC 5m — Live Paper Trader ", BOLD, CYAN) + p.c(f" {now}", GREY))
    lines.append(p.c("  " + "─" * 56, GREY))

    price = hb.get("price")
    price_s = f"${price:,.2f}" if price is not None else "—"
    sl = hb.get("seconds_left")
    countdown = f"{sl:.0f}s left" if sl is not None else "—"
    mv = hb.get("round_move")
    mv_s = ""
    if mv is not None:
        mv_s = "  move " + p.c(f"{mv:+.0f}", GREEN if mv > 0 else RED if mv < 0 else GREY, BOLD)
    lines.append(f"  price   {p.c(price_s, BOLD)}{mv_s}   round close in {p.c(countdown, YELLOW)}")

    # Current movement prediction + why we are / aren't trading.
    thr = state.get("entry_threshold", 0.60)
    d = pred.get("direction")
    if d:
        col = GREEN if d == "UP" else RED
        conf = pred.get("confidence", 0.0)
        move = pred.get("btc_move_usd", 0.0)
        armed = conf >= thr
        status = p.c(f"ARMED >= {thr:.2f}", GREEN, BOLD) if armed else p.c(f"waiting (need >= {thr:.2f})", YELLOW)
        lines.append(f"  predict {p.c(d, col, BOLD)}  conf {p.c(f'{conf:.2f}', BOLD)}  "
                     f"move {p.money(move, plus=True)}  rsi {pred.get('rsi_1m')}   {status}")
    else:
        lines.append(p.c(f"  predict —  (evaluates ~2m before each close; enters at conf >= {thr:.2f})", GREY))
    # Show that it's alive and deliberately skipping, not stuck.
    skips = state.get("skips_since_trade", 0)
    if skips:
        last = state.get("last_skip") or {}
        reason = last.get("reason", "")
        lines.append(p.c(f"  no trade in last {skips} decided round(s) "
                         f"(last skip: {reason or 'below threshold'}) — selectivity is normal", GREY))

    lines.append(p.c("  " + "─" * 56, GREY))

    # Headline account balance (dollars) — the number the user cares about.
    bankroll = state["bankroll"]
    balance = state["balance"]
    pnl_usd = state["pnl_usd"]
    bal_col = GREEN if balance >= bankroll else RED
    bal_s = p.c(f"${balance:,.2f}", bal_col, BOLD)
    pl_s = p.c(f"${pnl_usd:+,.2f}", bal_col, BOLD)
    lines.append(f"  account {bal_s}  (start ${bankroll:,.2f}, P/L {pl_s})")
    peak_s = f"${state['peak_bal']:,.2f}"
    dd_s = p.c(f"${state['max_dd_usd']:,.2f}", RED)
    stake_s = f"${state.get('last_stake', state['stake_usd']):.2f}"
    lines.append(f"  peak {peak_s}   maxDD {dd_s}   stake {stake_s}/trade")

    # Record + winrate vs breakeven (realized avg entry price when available).
    be_label = f"breakeven {be:.0%}" + (" real" if state.get("entry_n") else " assumed")
    wr_col = GREEN if wr >= be else RED
    verdict = "ABOVE breakeven ✓" if wr >= be else "below breakeven"
    lines.append(f"  trades  {p.c(str(trades), BOLD)}   "
                 f"{p.c(f'{wins}W', GREEN, BOLD)} / {p.c(f'{losses}L', RED, BOLD)}   "
                 f"winrate {p.c(f'{wr:.1%}', wr_col, BOLD)}  "
                 f"({be_label}, {p.c(verdict, wr_col)})")
    dec = state.get("total_decided", 0)
    if dec:
        entered = dec - state.get("total_skips", 0)
        rate = state.get("total_skips", 0) / dec
        lines.append(f"  rounds  {p.c(str(dec), BOLD)} decided   "
                     f"{p.c(str(entered), GREEN)} entered   "
                     f"{p.c(str(state.get('total_skips', 0)), GREY)} skipped "
                     f"({rate:.0%})")

    # Open positions: entered and waiting for the round to close. This is where
    # you watch a trade the moment it's placed, before it wins or loses.
    opens = state.get("open_positions", {})
    lines.append(p.c("  " + "─" * 56, GREY))
    lines.append(p.c(f"  open positions ({len(opens)} live) — entered, awaiting close", BOLD))
    if opens:
        for rnd in sorted(opens):
            e = opens[rnd]
            clk = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            side = e.get("side", "?")
            col = GREEN if side == "UP" else RED
            ep = e.get("entry_price")
            cost = f"@{ep:.2f}" if ep is not None else "  -  "
            conf = e.get("confidence")
            conf_s = f"conf {conf:.2f}" if conf is not None else ""
            stake = e.get("stake_usd")
            stake_s = f"${stake:.2f}" if stake is not None else ""
            big = p.c(" BIG", YELLOW, BOLD) if e.get("big_bet") else ""
            side_s = p.c(f"{side:<5}", col, BOLD)
            lines.append(f"   {clk}  {p.c('ENTER', col, BOLD)} {side_s} "
                         f"{cost:<7}{stake_s:<8}{conf_s}{big}")
    else:
        lines.append(p.c("   (none open — waiting for the next ARMED signal)", GREY))

    # Unified activity timeline: entries, skips (with confidence), wins, losses,
    # newest first. Bounded to --recent for the live redraw; the FULL history is
    # `scripts/btc_live_monitor.py --history` (or `start.sh history`).
    recent_n = state.get("recent_n", RECENT_N)
    act = state.get("activity", [])
    e = state.get("total_entries", 0)
    w = state.get("total_wins", 0)
    ll = state.get("total_losses", 0)
    sk = state.get("total_skips", 0)
    lines.append(p.c("  " + "─" * 56, GREY))
    lines.append(p.c(f"  activity (newest first) — showing {min(recent_n, len(act))} of {len(act)}   ", BOLD)
                 + p.c(f"[{e} entered  {w}W {ll}L  {sk} skipped]", GREY))
    if act:
        for ev in list(reversed(act))[:recent_n]:
            lines.append(_activity_row(ev, p))
    else:
        lines.append(p.c("   (no activity yet)", GREY))

    lines.append(p.c("  " + "─" * 56, GREY))
    lines.append(p.c("  Ctrl-C to exit  |  full timeline: start.sh history", GREY))
    return "\n".join(lines) + "\n"


def read_new_events(path: str, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Return (new events, new offset). Handles missing file and truncation."""
    if not os.path.exists(path):
        return [], 0
    size = os.path.getsize(path)
    if size < offset:  # file was rotated/truncated -> restart
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


def print_history(events: list[dict[str, Any]], p: Painter, bankroll: float,
                  stake_usd: float, entry_price: float) -> None:
    """Dump the COMPLETE chronological timeline (oldest first) with a running
    balance — every entry, skip, win and loss, not just the last N."""
    state = new_state(bankroll=bankroll, stake_usd=stake_usd, entry_price=entry_price)
    print(p.c("BTC 5m — full activity history (oldest first)", BOLD, CYAN))
    print(p.c("─" * 60, GREY))
    shown = 0
    for ev in events:
        fold_event(state, ev)
        if ev.get("type") in ("entry", "skip", "settle"):
            # settle rows were copied inside fold with the cumulative balance;
            # use that copy so the printed bal matches the account.
            row_ev = state["activity"][-1] if state["activity"] else ev
            print(_activity_row(row_ev, p))
            shown += 1
    if shown == 0:
        print(p.c("  (no activity in this log yet)", GREY))
    print(p.c("─" * 60, GREY))
    e = state.get("total_entries", 0)
    w = state.get("total_wins", 0)
    ll = state.get("total_losses", 0)
    sk = state.get("total_skips", 0)
    tr = state.get("trades", 0)
    wr = (w / tr) if tr else 0.0
    bal = state.get("balance", bankroll)
    pl = state.get("pnl_usd", 0.0)
    bal_col = GREEN if bal >= bankroll else RED
    pl_s = p.c(f"${pl:+,.2f}", bal_col, BOLD)
    print(p.c(f"  {e} entered   {w}W / {ll}L  (winrate {wr:.1%})   {sk} skipped", BOLD))
    print(f"  account {p.c(f'${bal:,.2f}', bal_col, BOLD)}  "
          f"(start ${bankroll:,.2f}, P/L {pl_s})")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live color dashboard for the paper-trader stream")
    ap.add_argument("--log", default="out/live.jsonl", help="Path to the JSONL stream")
    ap.add_argument("--interval", type=float, default=1.0, help="Redraw interval (seconds)")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Breakeven reference (contract entry price)")
    ap.add_argument("--bankroll", type=float, default=100.0, help="Starting account balance in USD")
    ap.add_argument("--stake-usd", type=float, default=10.0, help="USD per trade (for deriving $ from older logs)")
    ap.add_argument("--entry-threshold", type=float, default=0.60, help="Confidence needed to trade (shown as ARMED/waiting)")
    ap.add_argument("--recent", type=int, default=15, help="How many activity rows the LIVE view shows (full log via --history)")
    ap.add_argument("--once", action="store_true", help="Render a single frame and exit (no loop)")
    ap.add_argument("--history", action="store_true", help="Print the COMPLETE activity timeline (all entries/skips/wins/losses) and exit")
    color_grp = ap.add_mutually_exclusive_group()
    color_grp.add_argument("--color", dest="color", action="store_true", default=None)
    color_grp.add_argument("--no-color", dest="color", action="store_false")
    args = ap.parse_args(argv)

    color = args.color
    if color is None:
        color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    painter = Painter(color)

    if args.history:
        events, _ = read_new_events(args.log, 0)   # entire file
        print_history(events, painter, args.bankroll, args.stake_usd, args.entry_price)
        return 0

    state = new_state(bankroll=args.bankroll, stake_usd=args.stake_usd, entry_price=args.entry_price,
                      entry_threshold=args.entry_threshold, recent_n=args.recent)
    offset = 0

    def refresh() -> None:
        nonlocal offset
        evs, offset = read_new_events(args.log, offset)
        for ev in evs:
            fold_event(state, ev)

    if args.once:
        refresh()
        sys.stdout.write(render(state, args.entry_price, painter))
        return 0

    if color:
        sys.stdout.write(HIDE_CURSOR)
    try:
        while True:
            refresh()
            frame = render(state, args.entry_price, painter)
            sys.stdout.write(CLEAR + frame)
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
