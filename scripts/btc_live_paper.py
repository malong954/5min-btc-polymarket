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


def _rnd(v: Any, nd: int) -> Optional[float]:
    return round(v, nd) if isinstance(v, (int, float)) else None


def feature_snapshot(sig: Any) -> dict[str, Any]:
    """Compact, analysis-ready dump of the indicator readings behind a signal —
    logged on every prediction/entry/skip so the timeline can be correlated with
    outcomes later (RSI, MACD, Bollinger, ROC, divergence, EMA trend, volume,
    agreement)."""
    f = sig.features
    return {
        "rsi_1m": _rnd(f.get("rsi_1m"), 1),
        "macd_hist_1m": _rnd(f.get("macd_hist_1m"), 4),
        "bb_pctb_1m": _rnd(f.get("bb_pctb_1m"), 3),
        "roc_1m": _rnd(f.get("roc_1m"), 5),
        "divergence": f.get("divergence_type"),            # e.g. 'regular_bearish'
        "div_signal": _rnd(f.get("sub_divergence_1m"), 0),  # -1 / 0 / +1
        "ema_gap_5m": _rnd(f.get("ema_gap_5m"), 2),
        "ema_gap_15m": _rnd(f.get("ema_gap_15m"), 2),
        "rel_volume_1m": _rnd(f.get("rel_volume_1m"), 2),
        "agreement": _rnd(f.get("agreement"), 3),
        "vol_factor": _rnd(f.get("vol_factor"), 3),
        "btc_move_usd": _rnd(f.get("btc_move_usd"), 2),
        "score": _rnd(sig.score, 4),
    }


def describe_signal(sig: Any) -> str:
    """Human-readable reason string, e.g. 'high RSI 72, regular bearish, MACD-,
    move +45, agree 80%'. Logged even on skips so every round in the timeline
    carries what the bot saw and would have done."""
    f = sig.features
    parts: list[str] = []
    if sig.direction:
        parts.append(f"lean {sig.direction}")
    rsi = f.get("rsi_1m")
    if isinstance(rsi, (int, float)):
        tag = "high RSI" if rsi >= 70 else "low RSI" if rsi <= 30 else "RSI"
        parts.append(f"{tag} {rsi:.0f}")
    div = f.get("divergence_type")
    if div:
        parts.append(div.replace("_", " "))
    mh = f.get("macd_hist_1m")
    if isinstance(mh, (int, float)):
        parts.append("MACD+" if mh > 0 else "MACD-")
    mv = f.get("btc_move_usd")
    if isinstance(mv, (int, float)):
        parts.append(f"move {mv:+.0f}")
    ag = f.get("agreement")
    if isinstance(ag, (int, float)):
        parts.append(f"agree {ag:.0%}")
    return ", ".join(parts)


class LivePaperEngine:
    """Deterministic core: feed it (now, recent_1m_bars) via step() and it emits
    events and maintains paper-trade state. No network or clock of its own, so it
    is fully unit-testable; the network loop lives in main()."""

    def __init__(
        self,
        entry_threshold: float = 0.60,
        entry_price: float = 0.85,
        weights: Optional[dict[str, float]] = None,
        entry_window_sec: float = 150.0,
        min_entry_sec: float = 30.0,
        log: Optional[TextIO] = None,
        emit_heartbeat: bool = True,
        bankroll: float = 100.0,
        stake_usd: float = 10.0,
        sizing: str = "flat",
        stake_pct: float = 0.10,
        big_conf: float = 0.80,
        big_mult: float = 1.0,
        confluence: float = 0.0,
        require_market_price: bool = False,
        entry_rule: str = "threshold",
        edge_margin: float = 0.03,
        max_entry_price: float = 0.97,
    ):
        # Never pay near $1.00: a contract bought at 0.99-1.00 risks the whole
        # stake to win pennies (observed live: $10 risked to win $0.01). If the
        # ask is above this cap we keep watching — it can dip back — and if it
        # never does, the round is skipped as price_capped.
        self.max_entry_price = max_entry_price
        # entry_rule 'threshold': enter when confidence >= entry_threshold.
        # entry_rule 'edge': enter when confidence >= THE ASK + edge_margin —
        # the market price is the hurdle, so expensive (near-decided) rounds
        # demand near-certainty while cheap early asks need only modest
        # conviction. Edge = our probability minus their price, made literal.
        self.entry_rule = entry_rule
        self.edge_margin = edge_margin
        self.require_market_price = require_market_price
        self.sizing = sizing
        self.stake_pct = stake_pct
        self.big_conf = big_conf      # confidence at/above which to size up
        self.big_mult = big_mult      # stake multiplier for high-confidence trades (1.0 = off)
        self.confluence = confluence  # require indicator agreement for confidence (0..1)
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
        # Shadow book: rounds we PREDICTED but skipped. We settle them too (no
        # money) so the timeline records whether each skip was correct.
        self.shadows: dict[int, dict[str, Any]] = {}
        self.shadow_settled: set[int] = set()
        # Pending book: rounds whose signal cleared the threshold but had no
        # market price at decision time. We keep retrying the price on later
        # polls (while inside the window) instead of throwing the round away.
        self.pending: dict[int, dict[str, Any]] = {}
        # Live watch: the CURRENT round's evolving signal. Confidence is
        # re-evaluated every poll (impulse updated with the live spot) and we
        # enter at the FIRST threshold crossing; the peak snapshot becomes the
        # skip record if the window closes without one.
        self.watch: Optional[dict[str, Any]] = None
        self.skipped: set[int] = set()
        self.stats = {"trades": 0, "wins": 0, "pnl": 0.0, "pnl_usd": 0.0}

    @property
    def balance(self) -> float:
        return self.bankroll + self.stats["pnl_usd"]

    def _emit(self, ev: dict[str, Any]) -> dict[str, Any]:
        if self.log is not None:
            self.log.write(json.dumps(ev, separators=(",", ":")) + "\n")
            self.log.flush()
        return ev

    def _try_enter(self, cur: int, now: float, side: str, confidence: float,
                   feat: dict[str, Any], note: str,
                   entry_prices: Optional[dict[str, float]],
                   retries: int = 0, sec_left: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Open the position if we can price it. Returns the entry event, or
        None when require_market_price is on and no valid ask is available yet
        (the caller keeps the round pending and retries next poll)."""
        real = entry_prices.get(side) if entry_prices else None
        real_ok = isinstance(real, (int, float)) and 0.0 < real < 1.0
        if self.require_market_price and not real_ok:
            return None
        ep = float(real) if real_ok else self.entry_price
        from btc_sizing import stake_for
        stake = stake_for(
            self.sizing, bankroll=self.balance, base_stake=self.stake_usd,
            confidence=confidence, entry_price=ep, pct=self.stake_pct,
            p_est=confidence,  # live has no calibrator; confidence is a rough proxy
        )
        big = self.big_mult > 1.0 and confidence >= self.big_conf
        if big:
            stake = min(stake * self.big_mult, self.balance)
        stake = round(stake, 2)
        self.positions[cur] = {
            "side": side, "entry_price": ep,
            "confidence": round(confidence, 4), "opened_ts": int(now),
            "stake_usd": stake, "price_source": "polymarket" if real_ok else "fixed",
        }
        return self._emit({
            "ts": int(now), "type": "entry", "round": cur, "side": side,
            "entry_price": round(ep, 4), "price_source": "polymarket" if real_ok else "fixed",
            "confidence": round(confidence, 4),
            "seconds_left": round(sec_left, 1) if sec_left is not None else None,
            "stake_usd": stake, "sizing": self.sizing, "big_bet": big,
            "balance": round(self.balance, 2),
            "price_retries": retries,
            "features": feat, "note": note,
        })

    def step(self, now: float, bars_1m: list[Bar], spot: Optional[float] = None,
             entry_prices: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
        now = float(now)
        cur = bucket_5m(int(now))
        sec_left = (cur + 300) - now
        events: list[dict[str, Any]] = []
        model = MTFModel(bars_1m, weights=self.weights, confluence=self.confluence)

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

        # 1b) Settle SHADOW rounds (predicted but skipped) — no money, just record
        # whether the call would have won, so skips are analyzable after the fact.
        for rs in sorted(self.shadows):
            if rs in self.shadow_settled or now < rs + 300:
                continue
            actual = outcome_direction(model, rs)
            if actual not in ("UP", "DOWN"):
                continue
            sh = self.shadows[rs]
            would_win = sh["side"] == actual
            self.shadow_settled.add(rs)
            events.append(self._emit({
                "ts": int(now), "type": "shadow_settle", "round": rs,
                "side": sh["side"], "actual": actual,
                "result": "win" if would_win else "loss",
                "reason": sh.get("reason"), "confidence": sh.get("confidence"),
                "features": sh.get("features"), "note": sh.get("note"),
            }))

        # 1c) Retry pricing for rounds whose signal was ARMED but unpriceable at
        # decision time. Fill as soon as a valid ask appears; if the window runs
        # out, only then record the final no_market_price skip (+ shadow).
        for rs in list(self.pending):
            pend = self.pending[rs]
            rs_left = (rs + 300) - now
            if rs == cur and rs_left >= self.min_entry_sec:
                ev = self._try_enter(rs, now, pend["side"], pend["confidence"],
                                     pend["features"], pend["note"], entry_prices,
                                     retries=pend["retries"], sec_left=rs_left)
                if ev is not None:
                    events.append(ev)
                    del self.pending[rs]
                else:
                    pend["retries"] += 1
            else:
                # Window closed (or round rolled over) without ever pricing it.
                del self.pending[rs]
                self.skipped.add(rs)
                self.shadows[rs] = {"side": pend["side"], "reason": "no_market_price",
                                    "confidence": pend["confidence"],
                                    "features": pend["features"], "note": pend["note"]}
                events.append(self._emit({
                    "ts": int(now), "type": "skip", "round": rs,
                    "reason": "no_market_price", "side": pend["side"],
                    "confidence": pend["confidence"],
                    "price_retries": pend["retries"],
                    "features": pend["features"], "note": pend["note"],
                }))

        # 2) LIVE decision loop. The signal is re-evaluated EVERY poll inside the
        # window — impulse fed the live spot tick, so confidence climbs (or dies)
        # in real time — and we enter at the FIRST threshold crossing. The race
        # is reaching conviction while the ask is still cheap; a fixed decision
        # instant forfeits it.
        in_window = self.min_entry_sec <= sec_left <= self.entry_window_sec

        # 2a) Watched round ended (rolled over / window closed) without an entry
        # -> final skip carrying the PEAK confidence snapshot + shadow-settle it.
        w = self.watch
        if w is not None and (w["round"] != cur or sec_left < self.min_entry_sec):
            rs = w["round"]
            if rs not in self.positions and rs not in self.pending and rs not in self.skipped:
                if not w.get("side"):
                    reason = "no_direction"
                elif w.get("capped"):
                    reason = "price_capped"   # armed, but the ask stayed near $1
                else:
                    reason = "below_threshold"
                self.skipped.add(rs)
                if w.get("side"):
                    self.shadows[rs] = {"side": w["side"], "reason": reason,
                                        "confidence": w["conf"],
                                        "features": w["features"], "note": w["note"]}
                events.append(self._emit({
                    "ts": int(now), "type": "skip", "round": rs, "reason": reason,
                    "side": w.get("side"), "confidence": w.get("conf"),
                    "features": w.get("features"), "note": w.get("note"),
                }))
            if w["round"] != cur:
                self.watch = None

        if (in_window and cur not in self.positions
                and cur not in self.pending and cur not in self.skipped):
            as_of = (int(now) // 60) * 60   # close of the last fully-closed 1m bar
            sig = model.evaluate(cur, as_of_ts=as_of, spot=spot)
            if sig is not None:
                feat = feature_snapshot(sig)
                note = describe_signal(sig)
                conf = round(sig.confidence, 4)
                if self.watch is None or self.watch["round"] != cur:
                    # First evaluable poll of the round -> the one 'prediction'
                    # event (keeps round counting stable for the dashboard).
                    self.watch = {"round": cur, "side": sig.direction, "conf": conf,
                                  "cur_side": sig.direction, "cur_conf": conf,
                                  "features": feat, "note": note}
                    self.decided.add(cur)
                    rsi = sig.features.get("rsi_1m")
                    events.append(self._emit({
                        "ts": int(now), "type": "prediction", "round": cur,
                        "seconds_left": round(sec_left, 1), "direction": sig.direction,
                        "confidence": conf, "score": round(sig.score, 4),
                        "btc_move_usd": round(sig.features.get("btc_move_usd", 0.0), 2),
                        "rsi_1m": round(rsi, 1) if rsi is not None else None,
                        "features": feat, "note": note,
                    }))
                else:
                    self.watch["cur_side"] = sig.direction
                    self.watch["cur_conf"] = conf
                    if conf > self.watch["conf"]:   # peak snapshot for the skip record
                        self.watch.update({"side": sig.direction, "conf": conf,
                                           "features": feat, "note": note})
                ask = entry_prices.get(sig.direction) if (entry_prices and sig.direction) else None
                ask_ok = isinstance(ask, (int, float)) and 0.0 < ask < 1.0
                if self.entry_rule == "edge":
                    # Hurdle = the live ask + margin. No valid ask this poll ->
                    # not armed; the live loop just re-checks next poll.
                    armed = ask_ok and sig.confidence >= ask + self.edge_margin
                else:
                    armed = bool(sig.direction) and sig.confidence >= self.entry_threshold
                if armed and ask_ok and ask > self.max_entry_price:
                    # Armed but the contract is priced near $1 — risking the whole
                    # stake to win pennies. Keep watching; asks can dip back.
                    self.watch["capped"] = True
                    armed = False
                if armed:
                    ev = self._try_enter(cur, now, sig.direction, sig.confidence,
                                         feat, note, entry_prices, sec_left=sec_left)
                    if ev is not None:
                        events.append(ev)
                    else:
                        # ARMED but unpriceable right now — don't throw the round
                        # away; retry the price on later polls inside the window.
                        self.pending[cur] = {"side": sig.direction,
                                             "confidence": conf,
                                             "features": feat, "note": note,
                                             "retries": 1}

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
            hb = {
                "ts": int(now), "type": "heartbeat", "round": cur,
                "seconds_left": round(sec_left, 1),
                "price": round(last_px, 2) if last_px is not None else None,
                "round_move": round(round_move, 2) if round_move is not None else None,
                "open_positions": len(self.positions) - len(self.settled),
                "cum_pnl": round(self.stats["pnl"], 4),
                "balance": round(self.balance, 2),
            }
            # Live confidence ticking with the spot (dashboard shows it climb).
            if self.watch is not None and self.watch["round"] == cur:
                hb["direction"] = self.watch.get("cur_side")
                hb["confidence"] = self.watch.get("cur_conf")
            events.append(self._emit(hb))
        return events


def format_event(ev: dict[str, Any]) -> str:
    t = ev["type"]
    # Local machine time (e.g. US/Eastern), not UTC.
    clk = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
    if t == "heartbeat":
        if not ev.get("price"):
            return f"{clk}  · (no price)"
        mv = ev.get("round_move")
        mv_s = f" move={mv:+.0f}" if mv is not None else ""
        return (f"{clk}  · ${ev['price']:,.2f}{mv_s}  round+{300 - ev['seconds_left']:.0f}s  "
                f"open={ev['open_positions']} pnl={ev['cum_pnl']:+.3f}")
    if t == "prediction":
        return (f"{clk}  ? PREDICT {ev['direction'] or '--'} conf={ev['confidence']:.2f} "
                f"move=${ev['btc_move_usd']:+.0f} rsi={ev['rsi_1m']} ({ev['seconds_left']:.0f}s left)")
    if t == "entry":
        stake = ev.get("stake_usd")
        stake_s = f" ${stake:.2f}" if stake is not None else ""
        return f"{clk}  ▲ ENTER {ev['side']}{stake_s} @ ${ev['entry_price']:.2f}  conf={ev['confidence']:.2f}"
    if t == "skip":
        return f"{clk}  – skip ({ev['reason']}, conf={ev['confidence']:.2f})"
    if t == "settle":
        mark = "[WIN] " if ev["result"] == "win" else "[LOSS]"
        ep = f"@${ev['entry_price']:.2f} " if ev.get("entry_price") is not None else ""
        usd = f"  ${ev['pnl_usd']:+.2f} -> bal ${ev['balance']:.2f}" if "pnl_usd" in ev else ""
        return (f"{clk}  {mark} SETTLE {ev['side']} -> {ev['actual']} {ep}{ev['result'].upper()}"
                f"{usd}  wr={ev['winrate']:.1%} ({ev['trades']} trades)")
    return f"{clk}  {t} {ev}"


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
    ap.add_argument("--entry-threshold", type=float, default=0.60, help="Min confidence to open a paper position (entry-rule threshold)")
    ap.add_argument("--entry-rule", default="threshold", choices=["threshold", "edge"],
                    help="threshold: conf >= --entry-threshold | edge: conf >= live ask + --edge-margin (the price is the hurdle)")
    ap.add_argument("--edge-margin", type=float, default=0.03, help="Required conf minus ask in --entry-rule edge")
    ap.add_argument("--max-entry-price", type=float, default=0.97,
                    help="Never buy above this ask — near $1.00 you risk the whole stake to win pennies")
    ap.add_argument("--entry-window", type=float, default=150.0, help="Start deciding when this many seconds remain in the round")
    ap.add_argument("--min-entry", type=float, default=30.0, help="Stop entering/retrying prices below this many seconds left")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Assumed contract entry price (0.80-0.99)")
    ap.add_argument("--bankroll", type=float, default=100.0, help="Starting paper account balance in USD")
    ap.add_argument("--stake-usd", type=float, default=10.0, help="Base USD deployed per trade")
    ap.add_argument("--sizing", default="flat", choices=["flat", "percent", "confidence", "kelly"],
                    help="Position sizing: flat | percent (of current balance, auto-grows) | confidence-scaled | kelly (live kelly uses confidence as an UNCALIBRATED proxy)")
    ap.add_argument("--stake-pct", type=float, default=0.10, help="Fraction of current balance to stake in --sizing percent (e.g. 0.15 = 15%%)")
    ap.add_argument("--big-conf", type=float, default=0.80, help="Confidence at/above which to size up")
    ap.add_argument("--big-mult", type=float, default=1.0, help="Stake multiplier for confidence >= --big-conf (1.0 = off, e.g. 2.0 = double)")
    ap.add_argument("--confluence", type=float, default=0.0, help="0..1: require indicator AGREEMENT for confidence (0=off, 1=confidence fully scaled by agreement)")
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
        entry_window_sec=args.entry_window, min_entry_sec=args.min_entry,
        bankroll=args.bankroll, stake_usd=args.stake_usd, sizing=args.sizing,
        stake_pct=args.stake_pct, big_conf=args.big_conf, big_mult=args.big_mult,
        confluence=args.confluence, require_market_price=use_pm,
        entry_rule=args.entry_rule, edge_margin=args.edge_margin,
        max_entry_price=args.max_entry_price,
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
