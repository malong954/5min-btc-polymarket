#!/usr/bin/env python3
"""
Multi-timeframe (1m / 5m / 15m) technical backtester for the BTC 5-minute
Up/Down strategy.

What it answers
---------------
For each historical 5-minute round, freeze the clock at ~2 minutes before close
(the strategy's entry window) and, using ONLY data available at that instant,
combine indicators across three timeframes into a single directional score:

    - 1m: current-round impulse (BTC move so far), RSI(14), MACD(12,26,9)
    - 5m: EMA-fast/slow trend of closed 5m bars
    - 15m: EMA-fast/slow trend of closed 15m bars
    - volume: relative volume as a confidence multiplier

Then it checks the score against the round's actual settle (did the 5m candle
close above its open?) and reports directional accuracy, coverage, per-feature
correlation with the outcome, and a simulated PnL at a configurable contract
entry price ($0.80-$0.99 -> settles $1).

No-lookahead is enforced structurally: the current (in-progress) 5m round's
close is the outcome and is never fed to any feature; higher-timeframe features
use only bars that have fully closed by the decision instant.

Data
----
Input is 1-minute OHLCV. Three ways to get it:
    --csv PATH        load a 1m CSV (columns: time/timestamp, open, high, low,
                      close, volume; time in unix seconds/ms or ISO)
    --fetch-binance   pull recent 1m klines from Binance (runs on your PC;
                      blocked in restricted sandboxes)
    --synth N         generate N synthetic 1m bars (offline demo / self-test)

Usage
-----
    python3 scripts/btc_backtest.py --synth 5000
    python3 scripts/btc_backtest.py --csv data/btc_1m.csv --entry-price 0.85
    python3 scripts/btc_backtest.py --fetch-binance --days 7 --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from typing import Any, Optional

from btc_indicators import (
    clamp,
    ema,
    macd as macd_ind,
    pearson,
    rolling_mean,
    rsi as rsi_ind,
    stdev,
)

SLOT = 300  # 5-minute round, seconds
DECISION_OFFSET = 180  # freeze the clock 3 min in => ~2 min left


# --------------------------------------------------------------------------
# Bar model
# --------------------------------------------------------------------------
class Bar:
    __slots__ = ("ts", "o", "h", "l", "c", "v")

    def __init__(self, ts: int, o: float, h: float, l: float, c: float, v: float):
        self.ts = ts  # bar OPEN time (unix seconds), aligned to the timeframe
        self.o, self.h, self.l, self.c, self.v = o, h, l, c, v


def resample(bars_1m: list[Bar], tf: int) -> list[Bar]:
    """Aggregate 1m bars into `tf`-second bars aligned to UTC boundaries.

    A higher-tf bar is emitted only when its final constituent minute is present,
    i.e. it is treated as CLOSED. Partial trailing groups are dropped.
    """
    groups: dict[int, list[Bar]] = {}
    for b in bars_1m:
        start = b.ts - (b.ts % tf)
        groups.setdefault(start, []).append(b)
    out: list[Bar] = []
    minutes = tf // 60
    for start in sorted(groups):
        g = sorted(groups[start], key=lambda x: x.ts)
        # Require the closing minute of the group to be present -> bar is closed.
        if g[-1].ts != start + (minutes - 1) * 60:
            continue
        out.append(Bar(
            ts=start,
            o=g[0].o,
            h=max(x.h for x in g),
            l=min(x.l for x in g),
            c=g[-1].c,
            v=sum(x.v for x in g),
        ))
    return out


# --------------------------------------------------------------------------
# Feature extraction at the decision instant (no lookahead)
# --------------------------------------------------------------------------
class Signal:
    def __init__(self):
        self.features: dict[str, float] = {}
        self.score: float = 0.0
        self.confidence: float = 0.0
        self.direction: Optional[str] = None  # 'UP' | 'DOWN' | None


DEFAULT_WEIGHTS = {
    "impulse_1m": 1.0,
    "rsi_1m": 0.4,
    "macd_1m": 0.6,
    "trend_5m": 0.8,
    "trend_15m": 0.6,
}


class MTFModel:
    """Multi-timeframe indicator ensemble.

    Precomputes indicator series per timeframe once, then evaluates each round by
    positional lookup, which keeps the backtest O(n) rather than recomputing
    windows per round.
    """

    def __init__(
        self,
        bars_1m: list[Bar],
        weights: Optional[dict[str, float]] = None,
        ema_fast: int = 9,
        ema_slow: int = 21,
        entry_threshold: float = 0.20,
    ):
        self.bars_1m = sorted(bars_1m, key=lambda b: b.ts)
        self.bars_5m = resample(self.bars_1m, SLOT)
        self.bars_15m = resample(self.bars_1m, 900)
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self.entry_threshold = entry_threshold

        c1 = [b.c for b in self.bars_1m]
        self.rsi_1m = rsi_ind(c1, 14)
        _, _, self.macd_hist = macd_ind(c1, 12, 26, 9)
        self.vol_ma_1m = rolling_mean([b.v for b in self.bars_1m], 20)

        c5 = [b.c for b in self.bars_5m]
        self.ema_f_5m = ema(c5, ema_fast)
        self.ema_s_5m = ema(c5, ema_slow)

        c15 = [b.c for b in self.bars_15m]
        self.ema_f_15m = ema(c15, ema_fast)
        self.ema_s_15m = ema(c15, ema_slow)

        # ts -> index maps for fast positional lookup.
        self.idx_1m = {b.ts: i for i, b in enumerate(self.bars_1m)}
        self._starts_5m = [b.ts for b in self.bars_5m]
        self._starts_15m = [b.ts for b in self.bars_15m]

    def _last_closed_index(self, starts: list[int], tf: int, t_dec: int) -> Optional[int]:
        """Index of the last timeframe bar that has fully closed by t_dec."""
        best = None
        for i, s in enumerate(starts):
            if s + tf <= t_dec:  # bar closes at s+tf; must be <= decision instant
                best = i
            else:
                break
        return best

    def evaluate(self, round_start: int) -> Optional[Signal]:
        """Compute the signal for the 5m round beginning at `round_start`.

        Returns None if there isn't enough closed history / data at the decision
        instant (round is skipped, not counted as a trade).
        """
        t_dec = round_start + DECISION_OFFSET

        # 1m bar closing exactly at the decision instant must exist.
        dec_min_ts = t_dec - 60  # this 1m bar's OPEN ts (closes at t_dec)
        i1 = self.idx_1m.get(dec_min_ts)
        if i1 is None:
            return None
        # Round open = open of the 1m bar that starts the round.
        i_open = self.idx_1m.get(round_start)
        if i_open is None:
            return None
        round_open_px = self.bars_1m[i_open].o
        cur_px = self.bars_1m[i1].c

        sig = Signal()
        subs: dict[str, float] = {}

        # Local volatility scale (recent 1m close-to-close), for normalization.
        lo = max(0, i1 - 30)
        recent = [self.bars_1m[j].c for j in range(lo, i1 + 1)]
        rets = [recent[k] - recent[k - 1] for k in range(1, len(recent))]
        vol = stdev(rets) if len(rets) >= 2 else 0.0
        vol = vol if vol > 1e-9 else max(1.0, abs(cur_px) * 1e-5)

        # (1) Current-round impulse: how far BTC has already moved this round.
        move = cur_px - round_open_px
        subs["impulse_1m"] = math.tanh(move / (3.0 * vol))
        sig.features["btc_move_usd"] = move

        # (2) RSI(14) on 1m.
        r = self.rsi_1m[i1]
        if r is not None:
            subs["rsi_1m"] = clamp((r - 50.0) / 50.0, -1.0, 1.0)
            sig.features["rsi_1m"] = r

        # (3) MACD histogram on 1m.
        hh = self.macd_hist[i1]
        if hh is not None:
            subs["macd_1m"] = math.tanh(hh / (2.0 * vol))
            sig.features["macd_hist_1m"] = hh

        # (4) 5m EMA trend (closed bars only).
        j5 = self._last_closed_index(self._starts_5m, SLOT, t_dec)
        if j5 is not None and self.ema_f_5m[j5] is not None and self.ema_s_5m[j5] is not None:
            gap = self.ema_f_5m[j5] - self.ema_s_5m[j5]
            subs["trend_5m"] = math.tanh(gap / (5.0 * vol))
            sig.features["ema_gap_5m"] = gap

        # (5) 15m EMA trend (closed bars only).
        j15 = self._last_closed_index(self._starts_15m, 900, t_dec)
        if j15 is not None and self.ema_f_15m[j15] is not None and self.ema_s_15m[j15] is not None:
            gap = self.ema_f_15m[j15] - self.ema_s_15m[j15]
            subs["trend_15m"] = math.tanh(gap / (8.0 * vol))
            sig.features["ema_gap_15m"] = gap

        # Need at least the impulse plus one confirming indicator.
        if "impulse_1m" not in subs or len(subs) < 2:
            return None

        # (6) Volume confirmation -> confidence multiplier in [0.5, 1.5].
        vol_factor = 1.0
        vma = self.vol_ma_1m[i1]
        if vma and vma > 0:
            rel = self.bars_1m[i1].v / vma
            vol_factor = clamp(0.5 + 0.5 * rel, 0.5, 1.5)
            sig.features["rel_volume_1m"] = rel

        raw = sum(self.weights.get(k, 0.0) * v for k, v in subs.items())
        sig.features.update({f"sub_{k}": v for k, v in subs.items()})
        sig.score = math.tanh(raw)
        sig.confidence = clamp(abs(sig.score) * vol_factor, 0.0, 1.0)
        if sig.confidence >= self.entry_threshold and sig.score != 0.0:
            sig.direction = "UP" if sig.score > 0 else "DOWN"
        return sig


# --------------------------------------------------------------------------
# Backtest loop + metrics
# --------------------------------------------------------------------------
def outcome_direction(model: MTFModel, round_start: int) -> Optional[str]:
    """Actual settle: did the 5m round close above its open?"""
    starts = model._starts_5m
    try:
        j = starts.index(round_start)
    except ValueError:
        return None
    b = model.bars_5m[j]
    if b.c > b.o:
        return "UP"
    if b.c < b.o:
        return "DOWN"
    return "FLAT"


def run_backtest(
    bars_1m: list[Bar],
    weights: Optional[dict[str, float]] = None,
    entry_threshold: float = 0.20,
    entry_price: float = 0.85,
) -> dict[str, Any]:
    model = MTFModel(bars_1m, weights=weights, entry_threshold=entry_threshold)

    rounds = [b.ts for b in model.bars_5m]
    rows: list[dict[str, Any]] = []
    for rs in rounds:
        sig = model.evaluate(rs)
        if sig is None:
            continue
        actual = outcome_direction(model, rs)
        if actual is None or actual == "FLAT":
            continue
        rows.append({
            "round_start": rs,
            "score": sig.score,
            "confidence": sig.confidence,
            "pred": sig.direction,
            "actual": actual,
            "features": sig.features,
        })

    evaluated = len(rows)
    trades = [r for r in rows if r["pred"] is not None]
    n_trades = len(trades)
    wins = sum(1 for r in trades if r["pred"] == r["actual"])
    acc = wins / n_trades if n_trades else 0.0

    # Baseline: naively follow the current-round impulse direction on every round.
    base_ok = 0
    base_n = 0
    for r in rows:
        mv = r["features"].get("btc_move_usd", 0.0)
        if mv == 0:
            continue
        base_n += 1
        pred = "UP" if mv > 0 else "DOWN"
        if pred == r["actual"]:
            base_ok += 1
    base_acc = base_ok / base_n if base_n else 0.0

    # Accuracy by confidence tercile.
    conf_sorted = sorted(trades, key=lambda r: r["confidence"])
    buckets = []
    if n_trades >= 3:
        third = n_trades // 3
        slices = [conf_sorted[:third], conf_sorted[third:2 * third], conf_sorted[2 * third:]]
        for name, sl in zip(["low", "mid", "high"], slices):
            if sl:
                w = sum(1 for r in sl if r["pred"] == r["actual"])
                buckets.append({
                    "confidence_bucket": name,
                    "n": len(sl),
                    "accuracy": round(w / len(sl), 4),
                    "conf_range": [round(sl[0]["confidence"], 3), round(sl[-1]["confidence"], 3)],
                })

    # Per-feature correlation with the realized outcome (+1 up / -1 down).
    y = [1.0 if r["actual"] == "UP" else -1.0 for r in rows]
    feature_corr = {}
    for key in ["sub_impulse_1m", "sub_rsi_1m", "sub_macd_1m", "sub_trend_5m", "sub_trend_15m", "score"]:
        xs, ys = [], []
        for r, yy in zip(rows, y):
            v = r["features"].get(key) if key != "score" else r["score"]
            if v is not None:
                xs.append(v)
                ys.append(yy)
        c = pearson(xs, ys) if len(xs) >= 2 else None
        if c is not None:
            feature_corr[key] = round(c, 4)

    # Simulated PnL: buy at entry_price, settle $1 on win / $0 on loss.
    win_pnl = 1.0 - entry_price
    loss_pnl = -entry_price
    total_pnl = wins * win_pnl + (n_trades - wins) * loss_pnl
    ev = total_pnl / n_trades if n_trades else 0.0

    return {
        "bars_1m": len(model.bars_1m),
        "rounds_5m": len(model.bars_5m),
        "rounds_evaluated": evaluated,
        "trades": n_trades,
        "coverage": round(n_trades / evaluated, 4) if evaluated else 0.0,
        "directional_accuracy": round(acc, 4),
        "baseline_impulse_accuracy": round(base_acc, 4),
        "breakeven_winrate": entry_price,
        "edge_vs_breakeven": round(acc - entry_price, 4),
        "accuracy_by_confidence": buckets,
        "feature_correlation": feature_corr,
        "sim_pnl": {
            "entry_price": entry_price,
            "win_pnl_per_unit": round(win_pnl, 4),
            "loss_pnl_per_unit": round(loss_pnl, 4),
            "ev_per_trade": round(ev, 5),
            "total_pnl_units": round(total_pnl, 3),
        },
        "weights": model.weights,
        "entry_threshold": entry_threshold,
    }


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------
def load_csv(path: str) -> list[Bar]:
    import csv

    def to_ts(raw: str) -> int:
        raw = raw.strip()
        try:
            v = float(raw)
            if v > 1e12:  # milliseconds
                v /= 1000.0
            return int(v)
        except ValueError:
            iso = raw.replace("Z", "+00:00")
            return int(dt.datetime.fromisoformat(iso).timestamp())

    bars: list[Bar] = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = {name.strip().lower(): i for i, name in enumerate(header)}

        def col(*names: str) -> int:
            for n in names:
                if n in cols:
                    return cols[n]
            raise KeyError(f"CSV missing any of columns: {names}")

        ti = col("time", "timestamp", "open_time", "date")
        oi, hi, li, ci = col("open"), col("high"), col("low"), col("close")
        vi = col("volume", "vol")
        for row in reader:
            if not row:
                continue
            bars.append(Bar(
                to_ts(row[ti]), float(row[oi]), float(row[hi]),
                float(row[li]), float(row[ci]), float(row[vi]),
            ))
    bars.sort(key=lambda b: b.ts)
    return bars


def fetch_binance_1m(days: int = 7, symbol: str = "BTCUSDT") -> list[Bar]:
    """Fetch recent 1m klines from Binance. Runs on your PC; blocked in
    restricted network sandboxes (that's fine — use --synth or --csv there)."""
    import time

    import requests

    end = int(time.time() * 1000)
    start = end - days * 24 * 3600 * 1000
    bars: list[Bar] = []
    cur = start
    while cur < end:
        j = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": cur, "limit": 1000},
            timeout=15,
        ).json()
        if not j:
            break
        for k in j:
            bars.append(Bar(int(k[0]) // 1000, float(k[1]), float(k[2]),
                            float(k[3]), float(k[4]), float(k[5])))
        cur = int(j[-1][0]) + 60_000
        if len(j) < 1000:
            break
    bars.sort(key=lambda b: b.ts)
    return bars


def synth_bars(
    n: int,
    start_ts: int = 1_700_000_000,
    start_px: float = 100_000.0,
    step_vol: float = 12.0,
    autocorr: float = 0.0,
    seed: int = 42,
) -> list[Bar]:
    """Synthetic 1m OHLCV via a seeded LCG random walk.

    `autocorr` > 0 injects momentum (this-minute return partly follows the last),
    which creates a genuine, detectable edge; `autocorr` = 0 is a plain random
    walk. No Date/random module use so it's deterministic across runs.
    """
    start_ts -= start_ts % SLOT  # align to a 5m boundary
    state = seed & 0xFFFFFFFF

    def rand() -> float:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF  # [0,1)

    def gauss() -> float:
        # Box-Muller from two uniforms.
        u1 = max(1e-9, rand())
        u2 = rand()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    bars: list[Bar] = []
    px = start_px
    prev_ret = 0.0
    for i in range(n):
        ret = autocorr * prev_ret + (1.0 - autocorr) * gauss() * step_vol
        prev_ret = ret
        o = px
        c = px + ret
        h = max(o, c) + abs(gauss()) * step_vol * 0.3
        l = min(o, c) - abs(gauss()) * step_vol * 0.3
        v = 5.0 + abs(gauss()) * 3.0 + abs(ret) / step_vol
        bars.append(Bar(start_ts + i * 60, o, h, l, c, v))
        px = c
    return bars


def _print_report(res: dict[str, Any]) -> None:
    print("=" * 60)
    print("BTC 5m Multi-Timeframe Backtest")
    print("=" * 60)
    print(f"1m bars              : {res['bars_1m']}")
    print(f"5m rounds            : {res['rounds_5m']}")
    print(f"rounds evaluated     : {res['rounds_evaluated']}")
    print(f"trades taken         : {res['trades']}  (coverage {res['coverage']:.1%})")
    print(f"directional accuracy : {res['directional_accuracy']:.1%}")
    print(f"baseline (impulse)   : {res['baseline_impulse_accuracy']:.1%}")
    print(f"breakeven win-rate   : {res['breakeven_winrate']:.1%}  (entry ${res['breakeven_winrate']:.2f})")
    print(f"edge vs breakeven    : {res['edge_vs_breakeven']:+.1%}")
    print("-" * 60)
    print("accuracy by confidence:")
    for b in res["accuracy_by_confidence"]:
        print(f"  {b['confidence_bucket']:<4} n={b['n']:<5} acc={b['accuracy']:.1%}  conf {b['conf_range']}")
    print("-" * 60)
    print("feature correlation with outcome:")
    for k, v in res["feature_correlation"].items():
        print(f"  {k:<18} {v:+.4f}")
    print("-" * 60)
    p = res["sim_pnl"]
    print(f"sim PnL @ ${p['entry_price']:.2f} entry: EV/trade {p['ev_per_trade']:+.4f} units, "
          f"total {p['total_pnl_units']:+.2f} units over {res['trades']} trades")
    print("=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-timeframe BTC 5m backtester")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="Load 1m OHLCV CSV")
    src.add_argument("--fetch-binance", action="store_true", help="Fetch 1m klines from Binance (needs network)")
    src.add_argument("--synth", type=int, metavar="N", help="Generate N synthetic 1m bars (offline)")
    ap.add_argument("--days", type=int, default=7, help="Days of history for --fetch-binance")
    ap.add_argument("--autocorr", type=float, default=0.0, help="Momentum for --synth (0=random walk)")
    ap.add_argument("--entry-threshold", type=float, default=0.20, help="Min confidence to take a trade")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Assumed contract entry price (0.80-0.99)")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    args = ap.parse_args()

    if args.csv:
        bars = load_csv(args.csv)
    elif args.fetch_binance:
        bars = fetch_binance_1m(days=args.days)
    else:
        bars = synth_bars(args.synth, autocorr=args.autocorr)

    res = run_backtest(
        bars,
        entry_threshold=args.entry_threshold,
        entry_price=args.entry_price,
    )
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _print_report(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
