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
        stake_pct: float = 0.10,
        require_market_price: bool = False,
    ):
        self.require_market_price = require_market_price
        self.sizing = sizing
        self.stake_pct = stake_pct
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

    def step(self, now: float, bars_1m: list[Bar], spot: Optional[float] = None,
             entry_prices: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
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
            # Use THIS trade's actual entry price (real Polymarket ask when
            # available), not a global assumption.
            ep = pos.get("entry_price", self.entry_price)
            pnl = (1.0 - ep) if win else -ep
            # Dollar economics: spend `stake_usd` buying shares at ep. Win pays
            # $1/share; loss forfeits the stake.
            stake = pos.get("stake_usd", self.stake_usd)
            # Round to whole cents so the running balance always equals the sum of
            # the per-trade figures the user sees (no sub-cent drift).
            pnl_usd = round(stake * (1.0 - ep) / ep, 2) if win else -stake
            self.stats["trades"] += 1
            self.stats["wins"] += 1 if win else 0
            self.stats["pnl"] += pnl
            self.stats["pnl_usd"] += pnl_usd
            self.settled.add(rs)
            events.append(self._emit({
                "ts": int(now), "type": "settle", "round": rs, "side": pos["side"],
                "actual": actual, "result": "win" if win else "loss",
                "entry_price": round(ep, 4),
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
                    # Real Polymarket ask for the predicted side, if provided.
                    real = entry_prices.get(sig.direction) if entry_prices else None
                    real_ok = isinstance(real, (int, float)) and 0.0 < real < 1.0
                    if self.require_market_price and not real_ok:
                        events.append(self._emit({
                            "ts": int(now), "type": "skip", "round": cur,
                            "reason": "no_market_price", "side": sig.direction,
                            "confidence": round(sig.confidence, 4),
                        }))
                        return events
                    ep = float(real) if real_ok else self.entry_price
                    stake = round(stake_for(
                        self.sizing, bankroll=self.balance, base_stake=self.stake_usd,
                        confidence=sig.confidence, entry_price=ep, pct=self.stake_pct,
                        p_est=sig.confidence,  # live has no calibrator; confidence is a rough proxy
                    ), 2)
                    self.positions[cur] = {
                        "side": sig.direction, "entry_price": ep,
                        "confidence": round(sig.confidence, 4), "opened_ts": int(now),
                        "stake_usd": stake, "price_source": "polymarket" if real_ok else "fixed",
                    }
                    events.append(self._emit({
                        "ts": int(now), "type": "entry", "round": cur, "side": sig.direction,
                        "entry_price": round(ep, 4), "price_source": "polymarket" if real_ok else "fixed",
                        "confidence": round(sig.confidence, 4),
                        "stake_usd": stake, "sizing": self.sizing, "balance": round(self.balance, 2),
                    }))
                else:
                    events.append(self._emit({
                        "ts": int(now), "type": "skip", "round": cur,
                        "reason": "below_threshold" if sig.direction else "no_direction",
                        "confidence": round(sig.confidence, 4),
                    }))

        # 3) Heartbeat with the live price and the intra-round move building.
        if self.emit_heartbeat:
            # Prefer a live spot tick (updates every poll) over the 1m candle close.
            last_px = spot if spot is not None else (bars_1m[-1].c if bars_1m else None)
            # Round-open price = open of the 1m bar that started the current round.
            round_open = None
            i_open = model.idx_1m.get(cur) if bars_1m else None
            if i_open is not None:
                round_open = model.bars_1m[i_open].o
            round_move = (last_px - round_open) if (last_px is not None and round_open is not None) else None
            events.append(self._emit({
                "ts": int(now), "type": "heartbeat", "round": cur,
                "seconds_left": round(sec_left, 1),
                "price": round(last_px, 2) if last_px is not None else None,
                "round_move": round(round_move, 2) if round_move is not None else None,
                "open_positions": len(self.positions) - len(self.settled),
                "cum_pnl": round(self.stats["pnl"], 4),
                "balance": round(self.balance, 2),
            }))
        return events


def format_event(ev: dict[str, Any]) -> str:
    t = ev["type"]
    clk = time.strftime("%H:%M:%S", time.gmtime(ev["ts"]))
    if t == "heartbeat":
        if not ev.get("price"):
            return f"{clk}Z  · (no price)"
        mv = ev.get("round_move")
        mv_s = f" move={mv:+.0f}" if mv is not None else ""
        return (f"{clk}Z  · ${ev['price']:,.2f}{mv_s}  round+{300 - ev['seconds_left']:.0f}s  "
                f"open={ev['open_positions']} pnl={ev['cum_pnl']:+.3f}")
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
        ep = f"@${ev['entry_price']:.2f} " if ev.get("entry_price") is not None else ""
        usd = f"  ${ev['pnl_usd']:+.2f} -> bal ${ev['balance']:.2f}" if "pnl_usd" in ev else ""
        return (f"{clk}Z  {mark} SETTLE {ev['side']} -> {ev['actual']} {ep}{ev['result'].upper()}"
                f"{usd}  wr={ev['winrate']:.1%} ({ev['trades']} trades)")
    return f"{clk}Z  {t} {ev}"


def fetch_recent_1m(provider: str, minutes: int = 180) -> list[Bar]:
    from btc_history import fetch_history

    rows = fetch_history(provider, days=minutes / 1440.0)
    return [Bar(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


def fetch_spot(provider: str) -> Optional[float]:
    """A single cheap spot-price call (updates every poll), separate from the
    heavier 1m-kline fetch used for indicators."""
    from btc_price_feeds import build_feeds

    feeds = build_feeds([provider])
    return feeds[0].spot() if feeds else None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live paper-trading + streaming logger (BTC 5m)")
    ap.add_argument("--provider", default="binance", choices=["binance", "cryptocompare"],
                    help="1m data source for live prediction")
    ap.add_argument("--poll", type=float, default=2.0, help="Seconds between polls (live spot price ticks at this cadence)")
    ap.add_argument("--klines-every", type=float, default=15.0, help="Seconds between the heavier 1m-kline/indicator refreshes")
    ap.add_argument("--entry-threshold", type=float, default=0.60, help="Min confidence to open a paper position")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Assumed contract entry price (0.80-0.99)")
    ap.add_argument("--bankroll", type=float, default=100.0, help="Starting paper account balance in USD")
    ap.add_argument("--stake-usd", type=float, default=10.0, help="Base USD deployed per trade")
    ap.add_argument("--sizing", default="flat", choices=["flat", "percent", "confidence", "kelly"],
                    help="Position sizing: flat | percent (of current balance, auto-grows) | confidence-scaled | kelly (live kelly uses confidence as an UNCALIBRATED proxy)")
    ap.add_argument("--stake-pct", type=float, default=0.10, help="Fraction of current balance to stake in --sizing percent (e.g. 0.15 = 15%%)")
    ap.add_argument("--entry-price-source", default="fixed", choices=["fixed", "polymarket"],
                    help="fixed = use --entry-price for every trade (assumption); polymarket = use the REAL CLOB best ask of the predicted side per trade (factual; skips a round if unpriceable)")
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

    use_pm = args.entry_price_source == "polymarket"
    engine = LivePaperEngine(
        entry_threshold=args.entry_threshold, entry_price=args.entry_price, log=logf,
        bankroll=args.bankroll, stake_usd=args.stake_usd, sizing=args.sizing,
        stake_pct=args.stake_pct, require_market_price=use_pm,
    )
    price_desc = ("polymarket (real CLOB ask per trade)" if use_pm
                  else f"fixed ${args.entry_price:.2f} (assumption)")
    print(f"# live paper trader | provider={args.provider} poll={args.poll}s "
          f"entry_threshold={args.entry_threshold} price_source={price_desc} "
          f"bankroll=${args.bankroll:.2f} stake=${args.stake_usd:.2f} "
          f"| PAPER MODE (no real orders)", file=sys.stderr)

    n = 0
    bars: list[Bar] = []
    last_klines = 0.0
    try:
        while args.max_steps is None or n < args.max_steps:
            try:
                now = time.time()
                cur = bucket_5m(int(now))
                sec_left = (cur + 300) - now
                # Refresh the heavier 1m klines only every --klines-every seconds,
                # or when we need them fresh: in the entry window, or to settle a
                # round that has just closed. The spot tick (below) stays live.
                pending_settle = any(rs + 300 <= now and rs not in engine.settled
                                     for rs in engine.positions)
                in_window = engine.min_entry_sec <= sec_left <= engine.entry_window_sec
                if (not bars or now - last_klines >= args.klines_every
                        or in_window or pending_settle):
                    bars = fetch_recent_1m(args.provider, args.history_min)
                    last_klines = now
                spot = fetch_spot(args.provider)  # cheap, every poll -> live price
                # Real Polymarket prices only when we might actually enter (in the
                # window) — one extra call, and only then.
                entry_prices = None
                if use_pm and in_window:
                    try:
                        from btc_polymarket import current_prices
                        pm = current_prices(now)
                        if pm:
                            entry_prices = {"UP": pm.get("UP"), "DOWN": pm.get("DOWN")}
                    except Exception as e:
                        print(f"# polymarket price error: {e}", file=sys.stderr)
                events = engine.step(now, bars, spot=spot, entry_prices=entry_prices)
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
