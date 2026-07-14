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
import shutil
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
MAGENTA = "\033[35m"
BLUE = "\033[34m"
GREY = "\033[90m"
CLEAR = "\033[H\033[J"          # cursor home + clear to end
CLEAR_SCROLLBACK = "\033[3J"    # drop scrollback (ignored by Apple Terminal!)
# Alternate screen buffer (what htop/vim/less use): the dashboard draws on a
# separate screen with NO scrollback, so stale frames can never pile up above
# the live panel; on exit the user's normal shell screen is restored intact.
ALT_SCREEN_ON = "\033[?1049h\033[H"
ALT_SCREEN_OFF = "\033[?1049l"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

RECENT_N = 12

# Row tag colors when the feed mixes markets (multiple traders in one panel).
ASSET_COLOR = {"BTC": YELLOW, "ETH": CYAN, "SOL": MAGENTA, "XRP": BLUE}


def _fmt_move(mv: float) -> str:
    """Dollar-move string at a precision matching the asset's scale: BTC moves
    are whole dollars, ETH/SOL cents, XRP fractions of a cent."""
    a = abs(mv)
    if a >= 100:
        return f"{mv:+,.0f}"
    if a >= 0.05:
        return f"{mv:+,.2f}"
    return f"{mv:+.4f}"

# 5-row block font for the startup banner (only the letters FLIPPOLYBOT needs).
_FONT = {
    "F": ["#####", "#    ", "#### ", "#    ", "#    "],
    "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
    "I": ["#####", "  #  ", "  #  ", "  #  ", "#####"],
    "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
    "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
    "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
}


def banner_lines(text: str = "FLIPPOLYBOT") -> list[str]:
    rows = ["", "", "", "", ""]
    for ch in text:
        glyph = _FONT.get(ch.upper())
        if glyph is None:
            continue
        for i in range(5):
            rows[i] += glyph[i] + " "
    return [r.rstrip() for r in rows]


def show_banner(p: "Painter", hold: float = 1.8) -> None:
    """Splash screen at dashboard start: big FLIPPOLYBOT, then the live panel."""
    lines = [""]
    for r in banner_lines():
        lines.append("  " + p.c(r, CYAN, BOLD))
    lines.append("")
    lines.append("  " + p.c("BTC 5m — live paper trading lab", GREY))
    lines.append("")
    sys.stdout.write(CLEAR + "\n".join(lines) + "\n")
    sys.stdout.flush()
    time.sleep(hold)


def _fmt_uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}")


def _build_stamp() -> str:
    """Short git hash of the running checkout — shown in the header so 'which
    version is the Mac actually running?' is answerable from a screenshot."""
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=os.path.dirname(os.path.abspath(__file__)),
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or "?"
    except Exception:
        return "?"


BUILD = _build_stamp()


ACTIVITY_MAX = 5000  # cap the in-memory live feed (the full log stays on disk)


def new_state(bankroll: float = 100.0, stake_usd: float = 10.0, entry_price: float = 0.85,
              entry_threshold: float = 0.60, recent_n: int = RECENT_N,
              entry_rule: str = "threshold", edge_margin: float = 0.03) -> dict[str, Any]:
    return {
        "trades": 0, "wins": 0, "pnl": 0.0, "pnl_usd": 0.0,
        # Per-market accounting ({"BTC": {bankroll, pnl_usd, hb, pred}, ...}).
        # Each trader process runs ONE market with its own bankroll; the panel
        # shows the combined account plus a line per market when >1 is live.
        "assets": {}, "default_bankroll": bankroll,
        "bankroll": bankroll, "stake_usd": stake_usd, "entry_price": entry_price,
        "entry_threshold": entry_threshold, "recent_n": recent_n,
        "entry_rule": entry_rule, "edge_margin": edge_margin,
        "first_ts": None,   # first event ts -> bot session uptime
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


def _asset_state(state: dict[str, Any], a: str) -> dict[str, Any]:
    """Get (or create) the per-market sub-state. Events without an asset field
    (older logs) count as BTC. New markets start at the default bankroll until
    their trader's config event announces the real one; the combined bankroll
    is re-summed whenever the set changes."""
    assets = state["assets"]
    if a not in assets:
        assets[a] = {"bankroll": state.get("default_bankroll", 100.0),
                     "pnl_usd": 0.0, "hb": None, "pred": None}
        state["bankroll"] = sum(x["bankroll"] for x in assets.values())
        state["balance"] = state["bankroll"] + state["pnl_usd"]
        state["peak_bal"] = max(state["peak_bal"], state["balance"])
    return assets[a]


def _push_activity(state: dict[str, Any], ev: dict[str, Any]) -> None:
    state["activity"].append(ev)
    cap = state.get("activity_cap", ACTIVITY_MAX)
    if len(state["activity"]) > cap:
        state["activity"] = state["activity"][-cap:]


def _unit_to_usd(unit_pnl: float, stake_usd: float, entry_price: float) -> float:
    # shares bought = stake/entry; per-share pnl is unit_pnl -> dollars = unit_pnl*shares.
    return unit_pnl * stake_usd / entry_price if entry_price else 0.0


def fold_event(state: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    t = ev.get("type")
    ts = ev.get("ts")
    a = str(ev.get("asset") or "BTC").upper()
    if ts and (state.get("first_ts") is None or ts < state["first_ts"]):
        state["first_ts"] = ts   # session start = first event in the log
    if t == "config":
        # The trader announces its real settings — display those, never the
        # monitor's own launch flags (they can disagree after restarts).
        ast = _asset_state(state, a)
        if ev.get("bankroll") is not None:
            ast["bankroll"] = float(ev["bankroll"])
            state["bankroll"] = sum(x["bankroll"] for x in state["assets"].values())
            state["balance"] = state["bankroll"] + state["pnl_usd"]
            state["peak_bal"] = max(state["peak_bal"], state["balance"])
        state["entry_rule"] = ev.get("entry_rule", state.get("entry_rule"))
        if ev.get("entry_threshold") is not None:
            state["entry_threshold"] = ev["entry_threshold"]
        if ev.get("edge_margin") is not None:
            state["edge_margin"] = ev["edge_margin"]
        if ev.get("stake_usd") is not None:
            state["stake_usd"] = ev["stake_usd"]
        if ev.get("lead_min_conf") is not None:
            state["lead_min_conf"] = ev["lead_min_conf"]
        return state
    if t == "heartbeat":
        state["last_hb"] = ev
        _asset_state(state, a)["hb"] = ev
    elif t == "prediction":
        state["last_pred"] = ev
        _asset_state(state, a)["pred"] = ev
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
            # Keyed by (market, round): BTC and ETH share the same 5m buckets,
            # so a bare round number would collide across the two traders.
            state["open_positions"][(a, rnd)] = ev   # live until its round settles
        state["recent_entries"].append(ev)
        state["recent_entries"] = state["recent_entries"][-RECENT_N:]
        _push_activity(state, ev)
    elif t == "shadow_settle":
        # The trader grades every skipped-but-predicted round (no money). Attach
        # the resolution to the skip row already in the feed, so each SKIP line
        # shows how the contract actually resolved and whether the shadow call
        # would have won.
        for row in reversed(state["activity"]):
            if (row.get("type") == "skip" and row.get("round") == ev.get("round")
                    and str(row.get("asset") or "BTC").upper() == a):
                row["actual"] = ev.get("actual")
                row["shadow_result"] = ev.get("result")
                break
    elif t == "settle":
        ast = _asset_state(state, a)
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
        ast["pnl_usd"] += pnl_usd
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
        # account panel stays correct. The row shows THIS market's balance.
        ev["balance"] = round(ast["bankroll"] + ast["pnl_usd"], 2)
        state["open_positions"].pop((a, ev.get("round")), None)  # closed -> no longer open
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


def _asset_tag(ev: dict[str, Any], p: Painter) -> str:
    """'BTC ' / 'ETH ' column so a merged multi-market feed says which market
    each line belongs to. Events from older single-market logs have no asset
    field and get no tag (rows render exactly as before)."""
    a = ev.get("asset")
    if not a:
        return ""
    a = str(a).upper()
    return p.c(f"{a:<4}", ASSET_COLOR.get(a, GREY), BOLD)


def _activity_row(ev: dict[str, Any], p: Painter) -> str:
    """One timeline line for an entry / skip / settle event (newest-first feed
    and the full --history dump share this format)."""
    clk = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
    atag = _asset_tag(ev, p)
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
        return (f"   {clk}  {atag}{p.c('ENTER', CYAN, BOLD)} {p.c(f'{side:<5}', col, BOLD)} "
                f"{cost:<7}{stake_s:<8}{conf_s}{big}{note_s}")
    if t == "skip":
        reason = ev.get("reason", "") or "below_threshold"
        conf = ev.get("confidence")
        conf_s = f"conf {conf:.2f}" if conf is not None else "conf  -  "  # show conf even on skips
        # The SHADOW call: what it WOULD have bet had the threshold allowed it.
        side = ev.get("side")
        if side in ("UP", "DOWN"):
            scol = GREEN if side == "UP" else RED
            side_s = p.c(f"{side + '?':<6}", scol)
        else:
            side_s = " " * 6
        note = ev.get("note")
        note_s = p.c(f"  [{note}]", GREY) if note else ""   # why: high RSI, hidden divergence, ...
        # Once the round resolves, the shadow settle grades the skip: what the
        # contract actually did, and whether the call would have won.
        res_s = ""
        actual = ev.get("actual")
        if actual in ("UP", "DOWN"):
            ok = ev.get("shadow_result") == "win"
            res_s = " " + p.c(f"-> {actual} {'✓' if ok else '✗'}", GREEN if ok else RED, BOLD)
        return (f"   {clk}  {atag}{p.c('SKIP ', GREY, BOLD)} {side_s}"
                f"{p.c(f'{reason:<16}', GREY)} {p.c(conf_s, GREY)}{res_s}{note_s}")
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
    stake = ev.get("stake_usd")
    stk_s = f"  stk ${stake:.2f}" if stake is not None else ""
    pnl_str = f"${pnl_usd:+,.2f}"
    return (f"   {clk}  {atag}{p.c(tag, col, BOLD)}  {pair:<12}{cost:<7}"
            f"{p.c(f'{pnl_str:<8}', col, BOLD)} {bal_s}{p.c(stk_s, GREY)}")


def render(state: dict[str, Any], entry_price: float, p: Painter,
           max_rows: Optional[int] = None) -> str:
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

    assets = state.get("assets") or {}
    names = sorted(assets) or ["BTC"]
    multi = len(names) > 1

    lines: list[str] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    up_s = ""
    if state.get("first_ts"):
        up_s = "  up " + _fmt_uptime(time.time() - state["first_ts"])
    lines.append(p.c("  FLIPPOLYBOT ", BOLD, CYAN) + p.c("+".join(names) + " 5m", CYAN)
                 + p.c(f"  {now}", GREY) + p.c(up_s, YELLOW) + p.c(f"  build {BUILD}", GREY))
    lines.append(p.c("  " + "─" * 56, GREY))

    # The entry rule, stated once (both traders run the same rule).
    thr = state.get("entry_threshold", 0.60)
    rule = state.get("entry_rule", "threshold")
    margin = state.get("edge_margin", 0.03)
    lmc = state.get("lead_min_conf") or 0.0
    lead_desc = "lead rule: leading side in the sweet-spot window" + (
        f", conf >= {lmc:.2f}" if lmc > 0 else "")

    if multi:
        # One compact line per market: live price, round move, live call.
        sl = None
        for a in names:
            ast = assets[a]
            ahb = ast.get("hb") or {}
            apred = ast.get("pred") or {}
            if sl is None:
                sl = ahb.get("seconds_left")
            price = ahb.get("price")
            price_s = f"${price:,.2f}" if price is not None else "—"
            mv = ahb.get("round_move")
            mv_s = ""
            if mv is not None:
                mv_s = "  move " + p.c(_fmt_move(mv),
                                       GREEN if mv > 0 else RED if mv < 0 else GREY, BOLD)
            live = ahb.get("confidence") is not None and ahb.get("round") == apred.get("round")
            d = (ahb.get("direction") if live else None) or apred.get("direction")
            tag = p.c(f"{a:<4}", ASSET_COLOR.get(a, GREY), BOLD)
            if d:
                conf = ahb.get("confidence") if live else apred.get("confidence")
                conf = conf if conf is not None else 0.0
                dcol = GREEN if d == "UP" else RED
                call = f"predict {p.c(d, dcol, BOLD)} conf {p.c(f'{conf:.2f}', BOLD)}"
            else:
                call = p.c("predict —", GREY)
            lines.append(f"  {tag} {p.c(price_s, BOLD)}{mv_s}   {call}")
        countdown = f"{sl:.0f}s left" if sl is not None else "—"
        if rule == "edge":
            status = p.c(f"edge rule: enters when conf >= ask + {margin:.2f}", CYAN)
        elif rule == "lead":
            status = p.c(lead_desc, CYAN)
        else:
            status = p.c(f"enters at conf >= {thr:.2f}", CYAN)
        lines.append(f"  round close in {p.c(countdown, YELLOW)}   {status}")
    else:
        price = hb.get("price")
        price_s = f"${price:,.2f}" if price is not None else "—"
        sl = hb.get("seconds_left")
        countdown = f"{sl:.0f}s left" if sl is not None else "—"
        mv = hb.get("round_move")
        mv_s = ""
        if mv is not None:
            mv_s = "  move " + p.c(f"{mv:+.0f}", GREEN if mv > 0 else RED if mv < 0 else GREY, BOLD)
        lines.append(f"  price   {p.c(price_s, BOLD)}{mv_s}   round close in {p.c(countdown, YELLOW)}")

        # Current movement prediction + why we are / aren't trading. Prefer the
        # heartbeat's LIVE confidence (ticks with the spot every poll) over the
        # round's initial prediction snapshot.
        live = hb.get("confidence") is not None and hb.get("round") == pred.get("round")
        d = (hb.get("direction") if live else None) or pred.get("direction")
        if d:
            col = GREEN if d == "UP" else RED
            conf = hb.get("confidence") if live else pred.get("confidence", 0.0)
            conf = conf if conf is not None else 0.0
            move = pred.get("btc_move_usd", 0.0)
            if rule == "edge":
                # Armed-ness depends on the live ask, which the monitor doesn't
                # stream — state the rule instead of a bogus threshold check.
                status = p.c(f"edge rule: enters when conf >= ask + {margin:.2f}", CYAN)
            elif rule == "lead":
                status = p.c(lead_desc, CYAN)
            else:
                armed = conf >= thr
                status = p.c(f"ARMED >= {thr:.2f}", GREEN, BOLD) if armed else p.c(f"waiting (need >= {thr:.2f})", YELLOW)
            lines.append(f"  predict {p.c(d, col, BOLD)}  conf {p.c(f'{conf:.2f}', BOLD)}  "
                         f"move {p.money(move, plus=True)}  rsi {pred.get('rsi_1m')}   {status}")
        else:
            if rule == "edge":
                lines.append(p.c(f"  predict —  (edge rule: enters when conf >= ask + {margin:.2f})", GREY))
            elif rule == "lead":
                extra = f", conf >= {lmc:.2f}" if lmc > 0 else ""
                lines.append(p.c(f"  predict —  (lead rule: leading side in the sweet-spot window{extra})", GREY))
            else:
                lines.append(p.c(f"  predict —  (enters at conf >= {thr:.2f})", GREY))
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
    if multi:
        # One balance per market — each trader runs its own bankroll.
        parts = []
        for a in names:
            ast = assets[a]
            abal = ast["bankroll"] + ast["pnl_usd"]
            acol = GREEN if abal >= ast["bankroll"] else RED
            parts.append(p.c(f"{a:<4}", ASSET_COLOR.get(a, GREY), BOLD)
                         + p.c(f"${abal:,.2f}", acol, BOLD)
                         + p.c(f" (start ${ast['bankroll']:,.0f})", GREY))
        lines.append("  " + "   ".join(parts))
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
        show = sorted(opens, key=str)[-4:]  # cap; >1-2 live per market is abnormal anyway
        if len(opens) > len(show):
            lines.append(p.c(f"   … {len(opens) - len(show)} older not shown", GREY))
        for key in show:
            e = opens[key]
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
            lines.append(f"   {clk}  {_asset_tag(e, p)}{p.c('ENTER', col, BOLD)} {side_s} "
                         f"{cost:<7}{stake_s:<8}{conf_s}{big}")
    else:
        lines.append(p.c("   (none open — waiting for the next ARMED signal)", GREY))

    # Unified activity timeline: entries, skips (with confidence), wins, losses,
    # newest first. Bounded to --recent for the live redraw; the FULL history is
    # `scripts/btc_live_monitor.py --history` (or `start.sh history`).
    recent_n = state.get("recent_n", RECENT_N)
    # Fit the frame inside the terminal: if it would scroll, the in-place redraw
    # stacks stale frames in scrollback instead of updating one panel. Shrink
    # the activity list so header + feed + footer always fit the window height.
    if max_rows:
        budget = max_rows - len(lines) - 5   # activity header + footer + margin
        recent_n = max(3, min(recent_n, budget))
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


def clip_line(line: str, width: int) -> str:
    """Truncate to `width` VISIBLE chars, passing ANSI color codes through
    without counting them. A line wider than the terminal wraps onto a second
    physical row, which breaks the height budget and makes the in-place redraw
    scroll (stacking stale frames) — so no rendered line may ever wrap."""
    out: list[str] = []
    n = 0
    i = 0
    had_ansi = False
    while i < len(line):
        ch = line[i]
        if ch == "\x1b":                      # ANSI escape: copy through, zero width
            j = line.find("m", i)
            if j == -1:
                break
            out.append(line[i:j + 1])
            had_ansi = True
            i = j + 1
            continue
        if n >= width:
            break
        out.append(ch)
        n += 1
        i += 1
    clipped = "".join(out)
    if had_ansi and i < len(line):            # truncated mid-color -> close it
        clipped += RESET
    return clipped


def fit_frame(frame: str, columns: int, rows: Optional[int] = None) -> str:
    """Make the frame safe for in-place redraw: clip every line to the terminal
    width, HARD-truncate to the window height (a frame even one row taller than
    the terminal scrolls on every redraw and stacks stale copies in scrollback
    — the soft budget can't help when the window is shorter than the fixed
    sections), and drop the trailing newline (a newline written on the bottom
    row also scrolls)."""
    lines = [clip_line(ln, max(20, columns - 1)) for ln in frame.splitlines()]
    if rows is not None and len(lines) > rows - 1:
        keep = max(5, rows - 1)
        cut = len(lines) - (keep - 1)
        lines = lines[:keep - 1] + [f"{DIM}  … {cut} more rows — make the window taller{RESET}"]
    return "\n".join(lines)


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
    state["activity_cap"] = len(events) + 1   # the FULL history, never trimmed
    print(p.c("FLIPPOLYBOT — full activity history (oldest first)", BOLD, CYAN))
    print(p.c("─" * 60, GREY))
    # Fold EVERYTHING first, then print: a skip's resolution (shadow_settle)
    # arrives minutes after the skip itself, and folding annotates the stored
    # row in place — printing as we fold would miss it.
    for ev in events:
        fold_event(state, ev)
    shown = 0
    for row_ev in state["activity"]:
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
    start = state.get("bankroll", bankroll)   # summed across markets when >1
    bal = state.get("balance", start)
    pl = state.get("pnl_usd", 0.0)
    bal_col = GREEN if bal >= start else RED
    pl_s = p.c(f"${pl:+,.2f}", bal_col, BOLD)
    print(p.c(f"  {e} entered   {w}W / {ll}L  (winrate {wr:.1%})   {sk} skipped", BOLD))
    print(f"  account {p.c(f'${bal:,.2f}', bal_col, BOLD)}  "
          f"(start ${start:,.2f}, P/L {pl_s})")
    assets = state.get("assets") or {}
    if len(assets) > 1:
        parts = []
        for a in sorted(assets):
            ast = assets[a]
            abal = ast["bankroll"] + ast["pnl_usd"]
            acol = GREEN if abal >= ast["bankroll"] else RED
            parts.append(p.c(f"{a} ", ASSET_COLOR.get(a, GREY), BOLD)
                         + p.c(f"${abal:,.2f}", acol, BOLD))
        print("  " + "   ".join(parts))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live color dashboard for the paper-trader stream")
    ap.add_argument("--log", default="out/live.jsonl", help="Path to the JSONL stream")
    ap.add_argument("--eth-log", default=None,
                    help="Optional second trader stream (the ETH market) merged into the same "
                         "panel; rows are tagged BTC/ETH. Safe to pass even before the file exists.")
    ap.add_argument("--merge", action="append", default=[], metavar="PATH",
                    help="Additional trader stream(s) to merge (repeatable — one per extra "
                         "market, e.g. SOL/XRP). Rows are tagged by each event's asset field.")
    ap.add_argument("--interval", type=float, default=1.0, help="Redraw interval (seconds)")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Breakeven reference (contract entry price)")
    ap.add_argument("--bankroll", type=float, default=100.0, help="Starting account balance in USD")
    ap.add_argument("--stake-usd", type=float, default=10.0, help="USD per trade (for deriving $ from older logs)")
    ap.add_argument("--entry-threshold", type=float, default=0.60, help="Confidence needed to trade (shown as ARMED/waiting)")
    ap.add_argument("--entry-rule", default="threshold", choices=["threshold", "edge", "lead"],
                    help="Display which rule the trader runs (edge shows conf >= ask + margin)")
    ap.add_argument("--edge-margin", type=float, default=0.03, help="Displayed margin for --entry-rule edge")
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

    paths = [args.log] + ([args.eth_log] if args.eth_log else []) + list(args.merge)
    paths = list(dict.fromkeys(paths))   # dedupe, keep order

    if args.history:
        events: list[dict[str, Any]] = []
        for pth in paths:
            evs, _ = read_new_events(pth, 0)   # entire file(s)
            events.extend(evs)
        events.sort(key=lambda e: e.get("ts") or 0)   # merged chronological timeline
        print_history(events, painter, args.bankroll, args.stake_usd, args.entry_price)
        return 0

    state = new_state(bankroll=args.bankroll, stake_usd=args.stake_usd, entry_price=args.entry_price,
                      entry_threshold=args.entry_threshold, recent_n=args.recent,
                      entry_rule=args.entry_rule, edge_margin=args.edge_margin)
    offsets = {pth: 0 for pth in paths}

    def refresh() -> None:
        evs: list[dict[str, Any]] = []
        for pth in paths:
            new, offsets[pth] = read_new_events(pth, offsets[pth])
            evs.extend(new)
        evs.sort(key=lambda e: e.get("ts") or 0)   # interleave the two streams in time
        for ev in evs:
            fold_event(state, ev)

    if args.once:
        refresh()
        sys.stdout.write(render(state, args.entry_price, painter))
        return 0

    if color:
        sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR)
        show_banner(painter)   # FLIPPOLYBOT splash, then the live panel
    try:
        while True:
            refresh()
            term = shutil.get_terminal_size(fallback=(100, 40))
            frame = render(state, args.entry_price, painter, max_rows=term.lines)
            sys.stdout.write(CLEAR + CLEAR_SCROLLBACK + fit_frame(frame, term.columns, term.lines))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if color:
            sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
