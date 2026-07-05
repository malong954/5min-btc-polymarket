#!/usr/bin/env python3
"""
Live paper-trading + streaming logger for the BTC 5m strategy.

Runs the SAME multi-timeframe prediction that the backtester validates, but on
live data, in real time. Every poll it streams:
    - heartbeat   : current price, round, seconds-left
    - prediction  : direction / confidence / score ~2 min before each round close
    - entry       : a (paper) position opened when confidence clears the threshold
    - settle      : the round's real outcome, win/loss, and running PnL

PAPER BY DEFAULT: this never places real orders. It simulates buying the
predicted side at --entry-price and settling $1 (win) / $0 (loss) from the real
BTC move. Real execution stays in scripts/test_btc_5m_session_exit_sl.py, which
needs py_clob_client + Polymarket credentials. Prove the paper stream is
profitable before wiring real money.

Logs stream to stdout (human) and, with --log, to a JSONL file (one event per
line) you can tail, ship to a dashboard, or replay.

    python3 scripts/btc_live_paper.py --provider binance --poll 3 --log out/live.jsonl
    tail -f out/live.jsonl | python3 -m json.tool   # in another terminal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Optional, TextIO

from btc_backtest import Bar, DEFAULT_WEIGHTS, MTFModel, outcome_direction


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


class LivePaperEngine:
    """Deterministic core: feed it (now, recent_1m_bars) via step() and it emits
    events and maintains paper-trade state. No network or clock of its own, so it
    is fully unit-testable; the network loop lives in main()."""

    def __init__(
        self,
        entry_threshold: float = 0.60,
        entry_price: float = 0.85,
        weights: Optional[dict[str, float]] = None,
        entry_window_sec: float = 120.0,
        min_entry_sec: float = 45.0,
        log: Optional[TextIO] = None,
        emit_heartbeat: bool = True,
        bankroll: float = 100.0,
        stake_usd: float = 10.0,
        sizing: str = "flat",
    ):
        self.sizing = sizing
        self.entry_threshold = entry_threshold
        self.entry_price = entry_price
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self.entry_window_sec = entry_window_sec
        self.min_entry_sec = min_entry_sec
        self.log = log
        self.emit_heartbeat = emit_heartbeat
        self.bankroll = bankroll          # starting account balance ($)
        self.stake_usd = stake_usd        # dollars deployed per trade

        self.decided: set[int] = set()          # rounds we've made a call on
        self.positions: dict[int, dict[str, Any]] = {}
        self.settled: set[int] = set()
        self.stats = {"trades": 0, "wins": 0, "pnl": 0.0, "pnl_usd": 0.0}

    @property
    def balance(self) -> float:
        return self.bankroll + self.stats["pnl_usd"]

    def _emit(self, ev: dict[str, Any]) -> dict[str, Any]:
        if self.log is not None:
            self.log.write(json.dumps(ev, separators=(",", ":")) + "\n")
            self.log.flush()
        return ev

    def step(self, now: float, bars_1m: list[Bar]) -> list[dict[str, Any]]:
        now = float(now)
        cur = bucket_5m(int(now))
        sec_left = (cur + 300) - now
        events: list[dict[str, Any]] = []
        model = MTFModel(bars_1m, weights=self.weights)

        # 1) Settle any entered round that has closed.
        for rs in sorted(self.positions):
            if rs in self.settled or now < rs + 300:
                continue
            actual = outcome_direction(model, rs)
            if actual not in ("UP", "DOWN"):
                continue  # settle data not available yet; retry next poll
            pos = self.positions[rs]
            win = pos["side"] == actual
            pnl = (1.0 - self.entry_price) if win else -self.entry_price
            # Dollar economics: spend `stake_usd` buying shares at entry_price.
            # Win pays $1/share; loss forfeits the stake.
            stake = pos.get("stake_usd", self.stake_usd)
            # Round to whole cents so the running balance always equals the sum of
            # the per-trade figures the user sees (no sub-cent drift).
            pnl_usd = round(stake * (1.0 - self.entry_price) / self.entry_price, 2) if win else -stake
            self.stats["trades"] += 1
            self.stats["wins"] += 1 if win else 0
            self.stats["pnl"] += pnl
            self.stats["pnl_usd"] += pnl_usd
            self.settled.add(rs)
            events.append(self._emit({
                "ts": int(now), "type": "settle", "round": rs, "side": pos["side"],
                "actual": actual, "result": "win" if win else "loss",
                "pnl": round(pnl, 4), "cum_pnl": round(self.stats["pnl"], 4),
                "pnl_usd": round(pnl_usd, 2), "balance": round(self.balance, 2),
                "stake_usd": round(stake, 2),
                "trades": self.stats["trades"],
                "winrate": round(self.stats["wins"] / self.stats["trades"], 4),
            }))

        # 2) One prediction / entry decision per round, inside the entry window.
        if cur not in self.decided and self.min_entry_sec <= sec_left <= self.entry_window_sec:
            sig = model.evaluate(cur)
            if sig is not None:
                self.decided.add(cur)
                rsi = sig.features.get("rsi_1m")
                events.append(self._emit({
                    "ts": int(now), "type": "prediction", "round": cur,
                    "seconds_left": round(sec_left, 1), "direction": sig.direction,
                    "confidence": round(sig.confidence, 4), "score": round(sig.score, 4),
                    "btc_move_usd": round(sig.features.get("btc_move_usd", 0.0), 2),
                    "rsi_1m": round(rsi, 1) if rsi is not None else None,
                }))
                if sig.direction and sig.confidence >= self.entry_threshold:
                    from btc_sizing import stake_for
                    stake = round(stake_for(
                        self.sizing, bankroll=self.balance, base_stake=self.stake_usd,
                        confidence=sig.confidence, entry_price=self.entry_price,
                        p_est=sig.confidence,  # live has no calibrator; confidence is a rough proxy
                    ), 2)
                    self.positions[cur] = {
                        "side": sig.direction, "entry_price": self.entry_price,
                        "confidence": round(sig.confidence, 4), "opened_ts": int(now),
                        "stake_usd": stake,
                    }
                    events.append(self._emit({
                        "ts": int(now), "type": "entry", "round": cur, "side": sig.direction,
                        "entry_price": self.entry_price, "confidence": round(sig.confidence, 4),
                        "stake_usd": stake, "sizing": self.sizing, "balance": round(self.balance, 2),
                    }))
                else:
                    events.append(self._emit({
                        "ts": int(now), "type": "skip", "round": cur,
                        "reason": "below_threshold" if sig.direction else "no_direction",
                        "confidence": round(sig.confidence, 4),
                    }))

        # 3) Heartbeat with the current price and open prediction.
        if self.emit_heartbeat:
            last_px = bars_1m[-1].c if bars_1m else None
            events.append(self._emit({
                "ts": int(now), "type": "heartbeat", "round": cur,
                "seconds_left": round(sec_left, 1),
                "price": round(last_px, 2) if last_px is not None else None,
                "open_positions": len(self.positions) - len(self.settled),
                "cum_pnl": round(self.stats["pnl"], 4),
                "balance": round(self.balance, 2),
            }))
        return events


def format_event(ev: dict[str, Any]) -> str:
    t = ev["type"]
    clk = time.strftime("%H:%M:%S", time.gmtime(ev["ts"]))
    if t == "heartbeat":
        return (f"{clk}Z  · ${ev['price']:,.2f}  round+{300 - ev['seconds_left']:.0f}s  "
                f"open={ev['open_positions']} pnl={ev['cum_pnl']:+.3f}") if ev["price"] else f"{clk}Z  · (no price)"
    if t == "prediction":
        return (f"{clk}Z  ? PREDICT {ev['direction'] or '--'} conf={ev['confidence']:.2f} "
                f"move=${ev['btc_move_usd']:+.0f} rsi={ev['rsi_1m']} ({ev['seconds_left']:.0f}s left)")
    if t == "entry":
        stake = ev.get("stake_usd")
        stake_s = f" ${stake:.2f}" if stake is not None else ""
        return f"{clk}Z  ▲ ENTER {ev['side']}{stake_s} @ ${ev['entry_price']:.2f}  conf={ev['confidence']:.2f}"
    if t == "skip":
        return f"{clk}Z  – skip ({ev['reason']}, conf={ev['confidence']:.2f})"
    if t == "settle":
        mark = "[WIN] " if ev["result"] == "win" else "[LOSS]"
        usd = f"  ${ev['pnl_usd']:+.2f} -> bal ${ev['balance']:.2f}" if "pnl_usd" in ev else ""
        return (f"{clk}Z  {mark} SETTLE {ev['side']} -> {ev['actual']} {ev['result'].upper()} "
                f"pnl={ev['pnl']:+.3f}{usd}  "
                f"wr={ev['winrate']:.1%} ({ev['trades']} trades)")
    return f"{clk}Z  {t} {ev}"


def fetch_recent_1m(provider: str, minutes: int = 180) -> list[Bar]:
    from btc_history import fetch_history

    rows = fetch_history(provider, days=minutes / 1440.0)
    return [Bar(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live paper-trading + streaming logger (BTC 5m)")
    ap.add_argument("--provider", default="binance", choices=["binance", "cryptocompare"],
                    help="1m data source for live prediction")
    ap.add_argument("--poll", type=float, default=3.0, help="Seconds between polls")
    ap.add_argument("--entry-threshold", type=float, default=0.60, help="Min confidence to open a paper position")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Assumed contract entry price (0.80-0.99)")
    ap.add_argument("--bankroll", type=float, default=100.0, help="Starting paper account balance in USD")
    ap.add_argument("--stake-usd", type=float, default=10.0, help="Base USD deployed per trade")
    ap.add_argument("--sizing", default="flat", choices=["flat", "confidence", "kelly"],
                    help="Position sizing: flat | confidence-scaled | kelly (live kelly uses confidence as an UNCALIBRATED proxy — validate with backtest --sizing-report first)")
    ap.add_argument("--history-min", type=int, default=180, help="Minutes of 1m history to fetch each poll")
    ap.add_argument("--log", metavar="PATH", help="Append JSONL event stream to this file")
    ap.add_argument("--max-steps", type=int, default=None, help="Stop after N polls (default: run forever)")
    ap.add_argument("--quiet", action="store_true", help="Suppress heartbeat lines on stdout")
    args = ap.parse_args(argv)

    logf: Optional[TextIO] = None
    if args.log:
        d = os.path.dirname(args.log)
        if d:
            os.makedirs(d, exist_ok=True)
        logf = open(args.log, "a")

    engine = LivePaperEngine(
        entry_threshold=args.entry_threshold, entry_price=args.entry_price, log=logf,
        bankroll=args.bankroll, stake_usd=args.stake_usd, sizing=args.sizing,
    )
    print(f"# live paper trader | provider={args.provider} poll={args.poll}s "
          f"entry_threshold={args.entry_threshold} entry_price=${args.entry_price:.2f} "
          f"bankroll=${args.bankroll:.2f} stake=${args.stake_usd:.2f} "
          f"| PAPER MODE (no real orders)", file=sys.stderr)

    n = 0
    try:
        while args.max_steps is None or n < args.max_steps:
            try:
                bars = fetch_recent_1m(args.provider, args.history_min)
                events = engine.step(time.time(), bars)
                for ev in events:
                    if args.quiet and ev["type"] == "heartbeat":
                        continue
                    print(format_event(ev))
            except Exception as e:
                print(f"# poll error: {e}", file=sys.stderr)
            n += 1
            if args.max_steps is not None and n >= args.max_steps:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n# stopped", file=sys.stderr)
    finally:
        s = engine.stats
        wr = (s["wins"] / s["trades"]) if s["trades"] else 0.0
        print(f"# summary: {s['trades']} settled trades, winrate {wr:.1%} "
              f"(breakeven {args.entry_price:.0%}), "
              f"account ${engine.balance:.2f} (started ${args.bankroll:.2f}, "
              f"pnl ${s['pnl_usd']:+.2f})", file=sys.stderr)
        if logf:
            logf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
