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


WINDOW_SLOT = {"5m": 300, "15m": 900}


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def bucket_slot(ts: int, slot: int) -> int:
    return ts - (ts % slot)


def _rnd(v: Any, nd: int) -> Optional[float]:
    return round(v, nd) if isinstance(v, (int, float)) else None


SESSION_NAMES = ("asia", "europe", "us")


def _session_of(ts: float) -> str:
    """Trading session by UTC hour: asia 00-08, europe 08-16, us 16-24.
    (Matches the buckets every analyzer in this repo reports.)"""
    h = time.gmtime(int(ts)).tm_hour
    return "asia" if h < 8 else ("europe" if h < 16 else "us")


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
        "btc_move_usd": _rnd(f.get("btc_move_usd"), 6),  # 6dp: XRP moves live at 4dp
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
        entry_window_sec: float = 240.0,
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
        lead_window: tuple = (240.0, 180.0),
        lead_max_price: float = 0.72,
        lead_min_move: float = 10.0,
        lead_min_conf: float = 0.0,
        lead_persist: float = 5.0,
        official_wait: float = 150.0,
        asset: str = "BTC",
        window: str = "5m",
        regime_min_move: float = 0.0,
        regime_lookback: int = 12,
        sessions: Optional[set] = None,
        tiers: tuple = (0.25, 0.10, 0.05),
        skip_band: Optional[tuple] = None,
        cooldown_loss: int = 0,
        ask_fall_veto: float = 0.0,
        div_mode: str = "off",
        div_boost_mult: float = 2.0,
        quiet_vol_max: float = 0.77,
    ):
        # Divergence mode (deep study 2026-07-23, official labels, split-half):
        #   boost -> same entries, stake x div_boost_mult on rounds WITH a
        #            divergence (BTC crossing EV +8.9% with vs +2.9% without);
        #   veto  -> refuse entries on rounds WITH a divergence (XRP crossing
        #            +4.2% without vs -4.3% with; SOL similar) — skip reason
        #            'divergence_veto', still shadow-settled;
        #   off   -> ignore divergence (measurement default).
        self.div_mode = div_mode
        self.div_boost_mult = div_boost_mult
        # entry_rule 'quiet' (deep study, alts): the alt edge concentrates in
        # LOW-volatility rounds — the book stays uncertain (cheap favorites)
        # while the indicator still resolves the round. vol_factor at/below
        # this cap + no divergence + tradeable ask = enter the predicted side
        # (pooled BOTH-halves: SOL +4.0%, XRP +5.8% at avg ~0.64 asks).
        self.quiet_vol_max = quiet_vol_max
        # Volatility regime gate: only trade when the market itself is moving.
        # Requires the MEDIAN |open->close| of the trailing regime_lookback
        # completed rounds to reach regime_min_move dollars (0 = off). Measured
        # motivation: a weekday segment with median round move $22.9 had every
        # official-label entry band positive; a weekend chop segment with
        # median $4.9 had every band negative. Blocked rounds are skipped as
        # 'flat_regime' but still predicted + shadow-settled, so the gate's
        # cost/benefit stays measurable.
        self.regime_min_move = regime_min_move
        self.regime_lookback = regime_lookback
        # Session gate (measured 2026-07-25 on 972 live trades across BTC 5m +
        # 15m). US hours are where a CHEAP leading side stops meaning "the book
        # is slow" and starts meaning "the book is informed": cheap (<0.70)
        # entries win 71.1% in Asia but 51.5% in US — a coin flip — and US alone
        # accounts for -$95 of BTC 5m's -$65 lifetime P&L. Europe is the only
        # session positive in BOTH halves of history on BOTH books. None = trade
        # every session (measurement default); otherwise a set of
        # {"asia","europe","us"} by UTC hour: asia 00-08, europe 08-16, us 16-24.
        self.sessions = sessions
        # Sizing tiers for --sizing tiered (fractions of balance at >=100% /
        # 50-100% / <50% of the starting bankroll).
        self.tiers = tiers
        # Mid-band price skip (winners/losers study 2026-07-27): entered trades
        # win at CHEAP prices (<0.60: we disagree with a slow book and are
        # right) and at CONFIRMED prices (0.80-0.85: the book agrees), and lose
        # in the 0.60-0.80 middle — the maximum-uncertainty zone where the ask
        # already reflects our information. BTC15 ex-US: +3.9c -> +9.6c/share
        # with the band skipped (both halves). None = off; else (lo, hi).
        self.skip_band = skip_band
        # After-loss cooldown (same study, BTC 5m): the next entered trade
        # after a loser ran -4.8c/share both halves (loss-clustering: a loss
        # marks a regime the rule is mis-reading). Skip the next N armed
        # rounds after each settled loss. 0 = off.
        self.cooldown_loss = cooldown_loss
        self.cooldown_pending = 0
        # Ask-momentum veto (loser autopsy 2026-07-29): refuse entries when the
        # entry side's ask FELL by at least this much over the trailing ~60s.
        # Measured on 565 ex-US BTC5 trades: ask-fell-2c entries ran 55.1% win,
        # -14.3c/share (both halves) -- the book repricing AGAINST our side
        # while spot still shows it leading is informed flow calling the
        # reversal early. Rising asks ("chasing") are FINE: +3.0c both halves.
        # 0 = off. Keeps watching (a falling ask can stabilize).
        self.ask_fall_veto = ask_fall_veto
        self.ask_hist: list = []   # (ts, up_ask, dn_ask) trailing ~120s
        # Market interval: 5m (300s rounds) or 15m (900s rounds). Stamped on
        # every event so the dashboard can separate e.g. BTC 5m from BTC 15m.
        self.window = window
        self.slot = WINDOW_SLOT[window]
        # Lead rule: the FULL setup must hold continuously this many seconds
        # before entering. The measured combo table is built from 5s samples,
        # so it only counts rounds where the setup survives a sampling
        # interval; the 2s-polling executor was also buying momentary
        # threshold-grazes — audited at 60% winrate / -15.8% EV (vs 75% /
        # +9.0% on rule-matched rounds). Persistence makes the executor
        # trade the rule that was actually validated. 0 = legacy fire-at-once.
        self.lead_persist = lead_persist
        # Which market this engine trades ("BTC"/"ETH"); stamped on every
        # emitted event so a merged multi-market dashboard can tell them apart.
        self.asset = asset
        # Conviction floor for the lead rule (the combo candidate): 0 = off.
        self.lead_min_conf = lead_min_conf
        # How long past round close to wait for Polymarket's official
        # resolution before falling back to the spot label.
        self.official_wait = official_wait
        # 'lead' rule — the measured candidate strategy: buy whichever side BTC
        # already leads, inside the sweet-spot window (seconds-left hi..lo),
        # only while the ask is still cheap and the move is decisive. From the
        # robust entry-time table: ~72-77% winrate @ ~0.64-0.69 in 180-240s.
        self.lead_hi, self.lead_lo = float(lead_window[0]), float(lead_window[1])
        self.lead_max_price = lead_max_price
        self.lead_min_move = lead_min_move
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

    def _regime_move(self, model: Any, cur: int) -> Optional[float]:
        """Median |open -> close| of the trailing regime_lookback COMPLETED
        rounds, from the 1m bars. None when there isn't a single measurable
        round in the history window (gate then fails closed: no trade without
        evidence the market is moving)."""
        moves: list[float] = []
        rs = cur - self.slot
        while len(moves) < self.regime_lookback and rs >= 0:
            i_open = model.idx_1m.get(rs)
            i_close = model.idx_1m.get(rs + self.slot - 60)
            if i_open is None or i_close is None:
                break
            moves.append(abs(model.bars_1m[i_close].c - model.bars_1m[i_open].o))
            rs -= self.slot
        if not moves:
            return None
        moves.sort()
        mid = len(moves) // 2
        return moves[mid] if len(moves) % 2 else (moves[mid - 1] + moves[mid]) / 2.0

    def _emit(self, ev: dict[str, Any]) -> dict[str, Any]:
        ev.setdefault("asset", self.asset)
        ev.setdefault("window", self.window)
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
        # Settles can now WAIT for the official resolution, so a prior round's
        # position may still be open when this entry fires — size from the
        # balance net of money already at risk, or a losing overlap can spend
        # the same dollars twice (and drive the balance negative).
        reserved = sum(p.get("stake_usd", 0.0)
                       for r0, p in self.positions.items() if r0 not in self.settled)
        avail = max(0.0, self.balance - reserved)
        stake = stake_for(
            self.sizing, bankroll=avail, base_stake=self.stake_usd,
            confidence=confidence, entry_price=ep, pct=self.stake_pct,
            p_est=confidence,  # live has no calibrator; confidence is a rough proxy
            start_bankroll=self.bankroll, balance=self.balance, tiers=self.tiers,
        )
        big = self.big_mult > 1.0 and confidence >= self.big_conf
        if big:
            stake = min(stake * self.big_mult, avail)
        # Divergence boost: same entries, more size where the edge doubled
        # (measured on rounds WITH a divergence vs without — see div_mode).
        boosted = self.div_mode == "boost" and bool(feat.get("divergence"))
        if boosted:
            stake = min(stake * self.div_boost_mult, avail)
        stake = round(stake, 2)
        if stake <= 0:
            return None   # fully reserved/insolvent -> cannot open
        self.positions[cur] = {
            "side": side, "entry_price": ep,
            "confidence": round(confidence, 4), "opened_ts": int(now),
            "stake_usd": stake, "price_source": "polymarket" if real_ok else "fixed",
        }
        ask_sz = entry_prices.get(side + "_size") if entry_prices else None
        return self._emit({
            "ts": int(now), "type": "entry", "round": cur, "side": side,
            "entry_price": round(ep, 4), "price_source": "polymarket" if real_ok else "fixed",
            "ask_size": ask_sz,   # shares at the best ask — dust = phantom fill
            "confidence": round(confidence, 4),
            "seconds_left": round(sec_left, 1) if sec_left is not None else None,
            "stake_usd": stake, "sizing": self.sizing, "big_bet": big,
            "div_boost": boosted,   # divergence-present stake multiplier applied
            "balance": round(self.balance, 2),
            "avail": round(avail, 2),   # balance net of still-open stakes
            "price_retries": retries,
            "features": feat, "note": note,
        })

    def apply_requote(self, rs: int, entry_prices: Optional[dict[str, Any]],
                      now: float) -> Optional[dict[str, Any]]:
        """Honest-fill simulation: right after a paper entry, the caller
        re-reads the live book and this re-prices the position to the WORSE of
        the decision-time ask and the fresh ask — a real taker order is in
        flight while the quote moves, and paper must pay for that too. Never
        re-prices better (that would be gifting ourselves price improvement).
        Emits a requote event carrying the measured slip either way, so the
        log accumulates a real distribution of quote movement at our latency."""
        pos = self.positions.get(rs)
        if pos is None or rs in self.settled or pos.get("requoted"):
            return None
        if pos.get("price_source") != "polymarket":
            return None
        fresh = entry_prices.get(pos["side"]) if entry_prices else None
        if not isinstance(fresh, (int, float)) or not (0.0 < fresh < 1.0):
            return None
        old = pos["entry_price"]
        pos["requoted"] = True
        if fresh > old:
            pos["entry_price"] = float(fresh)
        return self._emit({
            "ts": int(now), "type": "requote", "round": rs, "side": pos["side"],
            "decision_ask": round(old, 4), "fresh_ask": round(float(fresh), 4),
            "slip": round(float(fresh) - old, 4),
            "filled_at": round(pos["entry_price"], 4),
        })

    def step(self, now: float, bars_1m: list[Bar], spot: Optional[float] = None,
             entry_prices: Optional[dict[str, float]] = None,
             official: Optional[dict[int, str]] = None,
             provisional: Optional[dict[int, str]] = None) -> list[dict[str, Any]]:
        now = float(now)
        cur = bucket_slot(int(now), self.slot)
        sec_left = (cur + self.slot) - now
        events: list[dict[str, Any]] = []
        model = MTFModel(bars_1m, weights=self.weights, confluence=self.confluence)

        def spot_label(rs: int) -> Optional[str]:
            """Window-aware spot grade: outcome_direction() reads the 5m bar
            aggregation, so 15m rounds are graded straight from the 1m bars
            (open of the round's first bar vs close of its last)."""
            if self.slot == 300:
                return outcome_direction(model, rs)
            i_open = model.idx_1m.get(rs)
            i_close = model.idx_1m.get(rs + self.slot - 60)
            if i_open is None or i_close is None:
                return None
            o = model.bars_1m[i_open].o
            c = model.bars_1m[i_close].c
            return "UP" if c > o else "DOWN" if c < o else "FLAT"

        def settle_outcome(rs: int) -> tuple[Optional[str], str]:
            """Prefer Polymarket's OFFICIAL resolution; our spot label breaks
            ties the wrong way (measured: 5/6 disagreements were spot=DOWN,
            official=UP, one at |move|=$0.00). Wait official_wait seconds for
            the official settle, then a Chainlink-sourced provisional grade
            (same feed the market resolves on) if the caller supplied one,
            then fall back to the spot label."""
            if official and official.get(rs) in ("UP", "DOWN"):
                return official[rs], "official"
            if now < rs + self.slot + self.official_wait:
                return None, "waiting"
            if provisional and provisional.get(rs) in ("UP", "DOWN"):
                return provisional[rs], "chainlink"
            return spot_label(rs), "spot_fallback"

        # 1) Settle any entered round that has closed.
        for rs in sorted(self.positions):
            if rs in self.settled or now < rs + self.slot:
                continue
            actual, settle_src = settle_outcome(rs)
            if actual not in ("UP", "DOWN"):
                continue  # official not posted yet / spot data missing; retry next poll
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
            if not win and self.cooldown_loss > 0:
                # Arm the after-loss cooldown: the next N otherwise-armed rounds
                # are refused (loss-clustering; see constructor note).
                self.cooldown_pending = self.cooldown_loss
            events.append(self._emit({
                "ts": int(now), "type": "settle", "round": rs, "side": pos["side"],
                "actual": actual, "result": "win" if win else "loss",
                "settle_source": settle_src,
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
            if rs in self.shadow_settled or now < rs + self.slot:
                continue
            actual, settle_src = settle_outcome(rs)
            if actual not in ("UP", "DOWN"):
                continue
            sh = self.shadows[rs]
            would_win = sh["side"] == actual
            self.shadow_settled.add(rs)
            events.append(self._emit({
                "ts": int(now), "type": "shadow_settle", "round": rs,
                "side": sh["side"], "actual": actual,
                "result": "win" if would_win else "loss",
                "settle_source": settle_src,
                "reason": sh.get("reason"), "confidence": sh.get("confidence"),
                "features": sh.get("features"), "note": sh.get("note"),
            }))

        # 1c) Retry pricing for rounds whose signal was ARMED but unpriceable at
        # decision time. Fill as soon as a valid ask appears; if the window runs
        # out, only then record the final no_market_price skip (+ shadow).
        for rs in list(self.pending):
            pend = self.pending[rs]
            rs_left = (rs + self.slot) - now
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
                elif w.get("flat_regime"):
                    reason = "flat_regime"    # setup armed, but the market is flat
                elif w.get("ask_fell"):
                    reason = "ask_falling"    # armed, but the book was repricing against us
                elif w.get("cooldown"):
                    reason = "cooldown"       # armed, but inside the after-loss pause
                elif w.get("mid_band"):
                    reason = "mid_band"       # armed, but the ask sat in the dead zone
                elif w.get("off_session"):
                    reason = "off_session"    # armed, but outside the traded sessions
                elif w.get("div_veto"):
                    reason = "divergence_veto"  # armed, but a divergence blocked it
                elif w.get("capped"):
                    reason = "price_capped"   # armed, but the ask stayed near $1
                elif self.entry_rule == "quiet":
                    # Loudness is the binding gate; divergence only matters once
                    # the round is calm.
                    reason = ("loud_market" if w.get("loud")
                              else "divergence_present" if w.get("diverged")
                              else "no_quiet_setup")
                elif self.entry_rule == "edge":
                    # Edge rule: confidence never exceeded the ask + margin —
                    # the book was already priced at/above our conviction.
                    reason = "no_edge"
                elif self.entry_rule == "lead":
                    reason = "no_lead_setup"   # window/ask/move never lined up
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
                    "ask": w.get("ask"),   # the price the rule refused (edge/capped skips)
                    "regime_move": w.get("regime_move"),  # trailing median move (flat_regime skips)
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
                        "btc_move_usd": round(sig.features.get("btc_move_usd", 0.0), 6),
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
                if entry_prices:
                    ua, da = entry_prices.get("UP"), entry_prices.get("DOWN")
                    self.ask_hist.append((now, ua if isinstance(ua, (int, float)) else None,
                                          da if isinstance(da, (int, float)) else None))
                    self.ask_hist = [h for h in self.ask_hist if now - h[0] <= 120]
                if ask_ok:
                    self.watch["ask"] = round(float(ask), 4)   # for skip diagnostics
                if self.entry_rule == "edge":
                    # Hurdle = the live ask + margin. No valid ask this poll ->
                    # not armed; the live loop just re-checks next poll.
                    armed = ask_ok and sig.confidence >= ask + self.edge_margin
                elif self.entry_rule == "lead":
                    # Candidate strategy: leading side, sweet-spot window, cheap
                    # ask, decisive move — plus an optional conviction floor
                    # (the lead+confidence combo from the official-label sweep).
                    mv = sig.features.get("btc_move_usd", 0.0) or 0.0
                    armed = (ask_ok and sig.direction is not None
                             and self.lead_lo <= sec_left <= self.lead_hi
                             and abs(mv) >= self.lead_min_move
                             and ask <= self.lead_max_price
                             and sig.confidence >= self.lead_min_conf)
                elif self.entry_rule == "quiet":
                    # Quiet-market rule: enter the predicted side only when the
                    # market is CALM (vol_factor at/below the cap) and clean (no
                    # divergence). Loud rounds are the book-wins-the-race regime
                    # where the ask prices the move before we act; divergence on
                    # the alts marks indicator confusion, not confirmation.
                    # ONE-SHOT: the decision is taken at the FIRST evaluable poll
                    # only — the validated rule is a single snapshot per round.
                    # Re-arming all window long was measured live to triple the
                    # trade count and pay ~5c more for the same winrate (rounds
                    # that BECOME quiet later are bought after the book converged).
                    if "quiet_armed" not in self.watch:
                        vf = sig.features.get("vol_factor")
                        quiet_ok = isinstance(vf, (int, float)) and vf <= self.quiet_vol_max
                        self.watch["loud"] = not quiet_ok
                        self.watch["diverged"] = bool(feat.get("divergence"))
                        ok = bool(sig.direction) and quiet_ok and not feat.get("divergence")
                        # Price the snapshot too: an ask over the cap at decision
                        # time is a refusal, not a wait-for-a-dip (a later cheaper
                        # ask means the market moved against the prediction).
                        if ok and ask_ok and ask > self.max_entry_price:
                            self.watch["capped"] = True
                            ok = False
                        # (ask may be None on the snapshot poll — fixed-price mode
                        # or a momentary book gap; pending-retry prices it later.)
                        self.watch["quiet_armed"] = ok
                    armed = self.watch["quiet_armed"]
                else:
                    armed = bool(sig.direction) and sig.confidence >= self.entry_threshold
                if armed and self.sessions is not None and _session_of(now) not in self.sessions:
                    # Session gate: this market's edge does not survive the
                    # informed-flow session(s) we excluded.
                    self.watch["off_session"] = True
                    armed = False
                if armed and self.div_mode == "veto" and feat.get("divergence"):
                    # Divergence veto: this market's edge lives in divergence-FREE
                    # rounds; refuse the entry and let the shadow book grade it.
                    self.watch["div_veto"] = True
                    armed = False
                if armed and ask_ok and ask > self.max_entry_price:
                    # Armed but the contract is priced near $1 — risking the whole
                    # stake to win pennies. Keep watching; asks can dip back.
                    self.watch["capped"] = True
                    armed = False
                if (armed and self.skip_band and ask_ok
                        and self.skip_band[0] <= ask < self.skip_band[1]):
                    # Mid-band price skip: the maximum-uncertainty zone where the
                    # ask already prices our signal. Keep watching — the ask can
                    # leave the band while the setup still holds.
                    self.watch["mid_band"] = True
                    armed = False
                if armed and self.ask_fall_veto > 0 and ask_ok:
                    # Entry-side ask ~45-90s ago (closest sample in that window).
                    then = [h for h in self.ask_hist if 45 <= now - h[0] <= 90]
                    prev = None
                    if then:
                        h = then[-1]
                        prev = h[1] if sig.direction == "UP" else h[2]
                    if isinstance(prev, (int, float)) and (ask - prev) <= -self.ask_fall_veto:
                        self.watch["ask_fell"] = True
                        armed = False
                if armed and (self.cooldown_pending > 0 or self.watch.get("cooldown")):
                    # After-loss cooldown: refuse this armed round (the flag
                    # sticks for the WHOLE round — one pending unit is consumed
                    # at the first armed poll, and later polls of the same round
                    # must not re-arm), then shadow-grade the refusal.
                    if not self.watch.get("cooldown"):
                        self.watch["cooldown"] = True
                        self.cooldown_pending -= 1
                    armed = False
                if armed and self.regime_min_move > 0 and self.entry_rule != "quiet":
                    # Regime gate: the setup fired, but is the market actually
                    # moving? Median trailing round move below the floor -> a
                    # coin-flip regime where the leading side mean-reverts.
                    # (The quiet rule is EXEMPT — it deliberately trades the calm
                    # rounds this gate exists to block; gating it would erase it.)
                    rm = self._regime_move(model, cur)
                    self.watch["regime_move"] = round(rm, 6) if rm is not None else None
                    if rm is None or rm < self.regime_min_move:
                        self.watch["flat_regime"] = True
                        armed = False
                if self.entry_rule == "lead" and self.lead_persist > 0:
                    # Sustained-arming gate: a single armed poll is not enough —
                    # the setup must still be armed lead_persist seconds after it
                    # first appeared. Any unarmed poll resets the clock.
                    if armed:
                        since = self.watch.get("armed_since")
                        if since is None:
                            self.watch["armed_since"] = now
                            armed = False
                        elif now - since < self.lead_persist:
                            armed = False
                    else:
                        self.watch.pop("armed_since", None)
                if armed:
                    reserved = sum(p.get("stake_usd", 0.0)
                                   for r0, p in self.positions.items() if r0 not in self.settled)
                    if self.balance - reserved < 0.01:
                        # ARMED but broke (or fully reserved): say so honestly
                        # instead of pretending the market was unpriceable. The
                        # bot keeps predicting + shadow-settling regardless.
                        self.skipped.add(cur)
                        self.shadows[cur] = {"side": sig.direction, "reason": "insolvent",
                                             "confidence": conf,
                                             "features": feat, "note": note}
                        events.append(self._emit({
                            "ts": int(now), "type": "skip", "round": cur,
                            "reason": "insolvent", "side": sig.direction,
                            "confidence": conf, "balance": round(self.balance, 2),
                            "features": feat, "note": note,
                        }))
                    else:
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
                "price": round(last_px, 6) if last_px is not None else None,
                "round_move": round(round_move, 6) if round_move is not None else None,
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
        slot = WINDOW_SLOT.get(str(ev.get("window") or "5m"), 300)
        return (f"{clk}  · ${ev['price']:,.2f}{mv_s}  round+{slot - ev['seconds_left']:.0f}s  "
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
    if t == "requote":
        return (f"{clk}  ~ requote {ev['side']} {ev['decision_ask']:.2f} -> {ev['fresh_ask']:.2f} "
                f"(slip {ev['slip']:+.2f}, filled @ {ev['filled_at']:.2f})")
    if t == "settle":
        mark = "[WIN] " if ev["result"] == "win" else "[LOSS]"
        ep = f"@${ev['entry_price']:.2f} " if ev.get("entry_price") is not None else ""
        usd = f"  ${ev['pnl_usd']:+.2f} -> bal ${ev['balance']:.2f}" if "pnl_usd" in ev else ""
        return (f"{clk}  {mark} SETTLE {ev['side']} -> {ev['actual']} {ep}{ev['result'].upper()}"
                f"{usd}  wr={ev['winrate']:.1%} ({ev['trades']} trades)")
    return f"{clk}  {t} {ev}"


def fetch_recent_1m(provider: str, minutes: int = 180, symbol: str = "BTCUSDT") -> list[Bar]:
    from btc_history import fetch_history

    kw = {"symbol": symbol} if provider == "binance" else {}
    rows = fetch_history(provider, days=minutes / 1440.0, **kw)
    return [Bar(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


def fetch_spot(provider: str, symbol: str = "BTCUSDT") -> Optional[float]:
    """A single cheap spot-price call (updates every poll), separate from the
    heavier 1m-kline fetch used for indicators."""
    from btc_price_feeds import build_feeds

    feeds = build_feeds([provider], symbol=symbol)
    return feeds[0].spot() if feeds else None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Live paper-trading + streaming logger (5m Up/Down)")
    ap.add_argument("--asset", default="btc", choices=["btc", "eth", "sol", "xrp"],
                    help="Which Polymarket Up/Down market to trade (sets slug + spot symbol)")
    ap.add_argument("--window", default="5m", choices=["5m", "15m"],
                    help="Market interval: 5m (300s rounds) or 15m (900s rounds). Window-shaped "
                         "defaults (--entry-window, --min-entry, --lead-hi/--lead-lo) scale x3 "
                         "for 15m unless passed explicitly")
    ap.add_argument("--provider", default="binance", choices=["binance", "cryptocompare"],
                    help="1m data source for live prediction")
    ap.add_argument("--poll", type=float, default=2.0, help="Seconds between polls (live spot price ticks at this cadence)")
    ap.add_argument("--klines-every", type=float, default=15.0, help="Seconds between the heavier 1m-kline/indicator refreshes")
    ap.add_argument("--entry-threshold", type=float, default=0.60, help="Min confidence to open a paper position (entry-rule threshold)")
    ap.add_argument("--entry-rule", default="threshold", choices=["threshold", "edge", "lead", "quiet"],
                    help="threshold: conf >= --entry-threshold | edge: conf >= live ask + --edge-margin | "
                         "lead: buy the leading side in the sweet-spot window when the ask is cheap (the measured candidate) | "
                         "quiet: predicted side when vol_factor <= --quiet-vol-max and no divergence (the alt deep-study candidate)")
    ap.add_argument("--lead-hi", type=float, default=None, help="lead rule: window opens at this many seconds left (default 240; x3 for --window 15m)")
    ap.add_argument("--lead-lo", type=float, default=None, help="lead rule: window closes at this many seconds left (default 180; x3 for --window 15m)")
    ap.add_argument("--lead-max-price", type=float, default=0.72, help="lead rule: only enter while the ask is at/below this")
    ap.add_argument("--lead-min-move", type=float, default=None,
                    help="lead rule: require the asset moved at least this many dollars from the round "
                         "open (default is price-scaled per asset: BTC 10, ETH 0.35)")
    ap.add_argument("--lead-min-conf", type=float, default=0.0,
                    help="lead rule: also require confidence >= this (the lead+confidence combo; 0 = off)")
    ap.add_argument("--lead-persist", type=float, default=5.0,
                    help="lead rule: the FULL setup must hold this many seconds before entering "
                         "(matches the 5s-sampled validation; audited: momentary grazes lose). 0 = off")
    ap.add_argument("--regime-frac", type=float, default=0.0,
                    help="Volatility regime gate: require the MEDIAN |open->close| of the trailing "
                         "--regime-lookback completed rounds to reach this FRACTION of the (auto-scaled) "
                         "--lead-min-move before any entry; blocked rounds skip as flat_regime. 0 = off. "
                         "E.g. 0.5 on BTC (~$10 decisive move) demands a ~$5 median trailing round move")
    ap.add_argument("--regime-lookback", type=int, default=12,
                    help="How many trailing completed rounds the regime gate measures (median)")
    ap.add_argument("--sessions", default=None,
                    help="Comma-separated sessions to trade (asia,europe,us; UTC hours 00-08/08-16/"
                         "16-24). Default: all. Measured on 972 live trades — cheap (<0.70) leading "
                         "sides win 71%% in asia but 51%% in us (informed flow), and us alone is "
                         "-$95 of BTC 5m's lifetime P&L. Blocked rounds skip as off_session")
    ap.add_argument("--skip-band", default=None,
                    help="Refuse entries while the ask is inside this price band, e.g. 0.60:0.80 "
                         "(the maximum-uncertainty zone; measured on BTC15 ex-US: +3.9c -> +9.6c/share "
                         "with the band skipped). Blocked rounds skip as mid_band. Default: off")
    ap.add_argument("--ask-fall-veto", type=float, default=0.0,
                    help="Refuse entries when the entry side's ask FELL by at least this much over "
                         "the trailing ~60s (measured: ask-fell-2c entries ran -14.3c/share on BTC5 "
                         "while rising-ask entries were +3.0c). Skips as ask_falling. 0 = off")
    ap.add_argument("--cooldown-loss", type=int, default=0,
                    help="After each settled LOSS, refuse the next N otherwise-armed rounds "
                         "(loss-clustering: BTC 5m's next trade after a loser ran -4.8c/share both "
                         "halves). Blocked rounds skip as cooldown. 0 = off")
    ap.add_argument("--tiers", default="0.25,0.10,0.05",
                    help="--sizing tiered fractions: at/above the starting bankroll, 50-100%% of it, "
                         "below 50%%. Default 0.25,0.10,0.05 (aggressive above water, defensive below)")
    ap.add_argument("--div-mode", default="off", choices=["off", "boost", "veto"],
                    help="Divergence handling (deep-study candidates): boost = stake x --div-boost-mult "
                         "on rounds WITH a divergence (BTC: +8.9%% with vs +2.9%% without) | veto = refuse "
                         "entries on divergence rounds (XRP/SOL: divergence-present rounds lose their edge) | off")
    ap.add_argument("--div-boost-mult", type=float, default=2.0,
                    help="Stake multiplier for --div-mode boost on divergence-present rounds")
    ap.add_argument("--quiet-vol-max", type=float, default=0.77,
                    help="entry-rule quiet: max vol_factor that still counts as a calm round "
                         "(0.77 = pooled bottom tercile; the alt edge lives below it)")
    ap.add_argument("--edge-margin", type=float, default=0.03, help="Required conf minus ask in --entry-rule edge")
    ap.add_argument("--max-entry-price", type=float, default=0.97,
                    help="Never buy above this ask — near $1.00 you risk the whole stake to win pennies")
    ap.add_argument("--entry-window", type=float, default=None,
                    help="Start deciding when this many seconds remain (default 240; x3 for --window "
                         "15m; the measured 5m sweet-spot crossings fire at 170-190s left, so "
                         "evaluation must start well above them)")
    ap.add_argument("--min-entry", type=float, default=None, help="Stop entering/retrying prices below this many seconds left (default 30; x3 for --window 15m)")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Assumed contract entry price (0.80-0.99)")
    ap.add_argument("--bankroll", type=float, default=100.0, help="Starting paper account balance in USD")
    ap.add_argument("--stake-usd", type=float, default=10.0, help="Base USD deployed per trade")
    ap.add_argument("--sizing", default="flat", choices=["flat", "percent", "confidence", "kelly", "tiered"],
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

    asset = args.asset.lower()
    symbol = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}[asset]
    slot = WINDOW_SLOT[args.window]
    scale = slot / 300.0   # window-shaped defaults were measured on 5m rounds
    if args.entry_window is None:
        args.entry_window = 240.0 * scale
    if args.min_entry is None:
        args.min_entry = 30.0 * scale
    if args.lead_hi is None:
        args.lead_hi = 240.0 * scale
    if args.lead_lo is None:
        args.lead_lo = 180.0 * scale
    if asset != "btc" and args.provider != "binance":
        print("non-BTC assets need --provider binance (other feeds are BTC-pinned)", file=sys.stderr)
        return 2
    if args.lead_min_move is None:
        # 'Decisive move' is a dollar threshold, so it must scale with the
        # asset's price. Auto-calibrate from the live spot: 0.016% of price
        # (exactly BTC's $10 rule at ~$62k). Fallbacks if the feed is down.
        s0 = None
        try:
            s0 = fetch_spot(args.provider, symbol=symbol)
        except Exception:
            pass
        if isinstance(s0, (int, float)) and s0 > 0:
            args.lead_min_move = round(s0 * 1.6e-4, 6)
        else:
            args.lead_min_move = {"btc": 10.0, "eth": 0.30}.get(asset)
            if args.lead_min_move is None:
                print("no spot available to auto-scale --lead-min-move for this asset; "
                      "pass it explicitly", file=sys.stderr)
                return 2
        print(f"# lead min move auto-scaled to {args.lead_min_move:g} "
              f"(0.016% of {symbol} spot)", file=sys.stderr)

    sessions = None
    if args.sessions:
        sessions = {s.strip().lower() for s in args.sessions.split(",") if s.strip()}
        bad = sessions - set(SESSION_NAMES)
        if bad:
            print(f"unknown session(s): {','.join(sorted(bad))} "
                  f"(allowed: {','.join(SESSION_NAMES)})", file=sys.stderr)
            return 2
        print(f"# session gate: trading only {','.join(sorted(sessions))} "
              f"(UTC asia 00-08 / europe 08-16 / us 16-24)", file=sys.stderr)

    skip_band = None
    if args.skip_band:
        try:
            lo, hi = (float(x) for x in args.skip_band.replace(":", ",").split(","))
            if not (0.0 <= lo < hi <= 1.0):
                raise ValueError
            skip_band = (lo, hi)
        except ValueError:
            print(f"bad --skip-band {args.skip_band!r} (want e.g. 0.60:0.80)", file=sys.stderr)
            return 2
    try:
        tiers = tuple(float(x) for x in args.tiers.split(","))
        assert len(tiers) == 3 and all(0.0 < t <= 1.0 for t in tiers)
    except Exception:
        print(f"bad --tiers {args.tiers!r} (want three fractions, e.g. 0.25,0.10,0.05)", file=sys.stderr)
        return 2

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
        lead_window=(args.lead_hi, args.lead_lo),
        lead_max_price=args.lead_max_price, lead_min_move=args.lead_min_move,
        lead_min_conf=args.lead_min_conf, lead_persist=args.lead_persist,
        asset=asset.upper(), window=args.window,
        regime_min_move=args.regime_frac * args.lead_min_move,
        regime_lookback=args.regime_lookback,
        sessions=sessions,
        tiers=tiers, skip_band=skip_band, cooldown_loss=args.cooldown_loss,
        ask_fall_veto=args.ask_fall_veto,
        div_mode=args.div_mode, div_boost_mult=args.div_boost_mult,
        quiet_vol_max=args.quiet_vol_max,
    )
    price_desc = ("polymarket (real CLOB ask per trade)" if use_pm
                  else f"fixed ${args.entry_price:.2f} (assumption)")
    print(f"# live paper trader | asset={asset.upper()} {args.window} ({symbol}) provider={args.provider} poll={args.poll}s "
          f"entry_threshold={args.entry_threshold} price_source={price_desc} "
          f"bankroll=${args.bankroll:.2f} stake=${args.stake_usd:.2f} "
          f"| PAPER MODE (no real orders)", file=sys.stderr)
    # Announce the ACTUAL config into the event stream so the dashboard shows
    # the truth instead of guessing from its own launch flags (observed: a
    # lead-rule trader under a header claiming 'need >= 0.60').
    engine._emit({
        "ts": int(time.time()), "type": "config",
        "window": args.window,
        "entry_rule": args.entry_rule, "entry_threshold": args.entry_threshold,
        "edge_margin": args.edge_margin, "max_entry_price": args.max_entry_price,
        "lead_hi": args.lead_hi, "lead_lo": args.lead_lo,
        "lead_max_price": args.lead_max_price, "lead_min_move": args.lead_min_move,
        "lead_min_conf": args.lead_min_conf, "lead_persist": args.lead_persist,
        "regime_frac": args.regime_frac, "regime_lookback": args.regime_lookback,
        "regime_min_move": round(args.regime_frac * args.lead_min_move, 6),
        "div_mode": args.div_mode, "div_boost_mult": args.div_boost_mult,
        "quiet_vol_max": args.quiet_vol_max,
        "sessions": sorted(sessions) if sessions else None,
        "skip_band": list(skip_band) if skip_band else None,
        "cooldown_loss": args.cooldown_loss,
        "ask_fall_veto": args.ask_fall_veto,
        "tiers": list(tiers),
        "sizing": args.sizing, "stake_usd": args.stake_usd,
        "bankroll": args.bankroll, "price_source": args.entry_price_source,
        "provider": args.provider,
    })

    n = 0
    bars: list[Bar] = []
    last_klines = 0.0
    # Official Polymarket resolutions for closed rounds (settle ground truth).
    official: dict[int, str] = {}
    official_tries: dict[int, int] = {}
    # Chainlink-sourced provisional grades (used after official_wait, before
    # the spot fallback — same feed the market resolves on). Free method A.
    try:
        import chainlink_feed as cl
        cl_ok = cl.candle_creds_ok()
    except Exception:
        cl = None
        cl_ok = False
    if cl_ok:
        print("# chainlink provisional grading: ON", file=sys.stderr)
    provisional: dict[int, str] = {}
    prov_tries: dict[int, int] = {}
    try:
        while args.max_steps is None or n < args.max_steps:
            try:
                now = time.time()
                cur = bucket_slot(int(now), slot)
                sec_left = (cur + slot) - now
                # Fetch the official resolution for any closed round we still
                # hold (or shadow) — the spot label breaks ties the wrong way.
                if use_pm:
                    from btc_polymarket import current_slug, resolved_outcome
                    waiting = [rs for rs in list(engine.positions) + list(engine.shadows)
                               if rs not in engine.settled and rs not in engine.shadow_settled
                               and rs not in official and now >= rs + slot + 45
                               and official_tries.get(rs, 0) < 6]
                    for rs in waiting[:2]:   # bounded per poll
                        try:
                            oc = resolved_outcome(current_slug(rs, asset, args.window))
                        except Exception:
                            oc = None
                        if oc in ("UP", "DOWN"):
                            official[rs] = oc
                        else:
                            official_tries[rs] = official_tries.get(rs, 0) + 1
                # Chainlink provisional grade for closed rounds the official
                # settle hasn't covered yet (bounded: one round per poll).
                if cl_ok:
                    want = [rs for rs in list(engine.positions) + list(engine.shadows)
                            if rs not in engine.settled and rs not in engine.shadow_settled
                            and rs not in official and rs not in provisional
                            and now >= rs + slot + 5 and prov_tries.get(rs, 0) < 6]
                    for rs in want[:1]:
                        try:
                            up = cl.settle_up(asset, rs, rs + slot)
                        except Exception:
                            up = None
                        if up is None:
                            prov_tries[rs] = prov_tries.get(rs, 0) + 1
                        else:
                            provisional[rs] = "UP" if up else "DOWN"
                # Refresh the heavier 1m klines only every --klines-every seconds,
                # or when we need them fresh: in the entry window, or to settle a
                # round that has just closed. The spot tick (below) stays live.
                pending_settle = any(rs + slot <= now and rs not in engine.settled
                                     for rs in engine.positions)
                in_window = engine.min_entry_sec <= sec_left <= engine.entry_window_sec
                if (not bars or now - last_klines >= args.klines_every
                        or in_window or pending_settle):
                    bars = fetch_recent_1m(args.provider, args.history_min, symbol=symbol)
                    last_klines = now
                spot = fetch_spot(args.provider, symbol=symbol)  # cheap, every poll -> live price
                # Real Polymarket prices only when we might actually enter (in the
                # window) — one extra call, and only then.
                entry_prices = None
                if use_pm and in_window:
                    try:
                        from btc_polymarket import current_prices
                        pm = current_prices(now, asset=asset, window=args.window)
                        if pm:
                            entry_prices = {"UP": pm.get("UP"), "DOWN": pm.get("DOWN"),
                                            "UP_size": pm.get("UP_size"),
                                            "DOWN_size": pm.get("DOWN_size")}
                    except Exception as e:
                        print(f"# polymarket price error: {e}", file=sys.stderr)
                events = engine.step(now, bars, spot=spot, entry_prices=entry_prices,
                                     official=official, provisional=provisional)
                for ev in events:
                    if args.quiet and ev["type"] == "heartbeat":
                        continue
                    print(format_event(ev))
                # Honest fill: after any entry, re-read the book once and pay
                # the worse of the two asks (quote movement during order
                # flight). Also logs the slip, so paper measures how fast
                # quotes move under our feet at this polling latency.
                if use_pm:
                    for ev in events:
                        if ev.get("type") != "entry":
                            continue
                        try:
                            from btc_polymarket import current_prices
                            pm2 = current_prices(time.time(), asset=asset, window=args.window)
                        except Exception:
                            pm2 = None
                        if pm2:
                            rq = engine.apply_requote(
                                ev["round"], {"UP": pm2.get("UP"), "DOWN": pm2.get("DOWN")},
                                time.time())
                            if rq is not None:
                                print(format_event(rq))
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
