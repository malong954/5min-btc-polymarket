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
        # Stored so a round can be re-weighted later (walk-forward weight tuning)
        # from its sub-signals alone, without recomputing indicators.
        sig.features["vol_factor"] = vol_factor
        sig.score = math.tanh(raw)
        sig.confidence = clamp(abs(sig.score) * vol_factor, 0.0, 1.0)
        # Direction is threshold-independent (pure sign of the score); whether a
        # round is actually *traded* is decided later by comparing confidence to
        # the entry threshold. Keeping these separate lets the walk-forward tuner
        # re-thresholds the same scored rounds without recomputing indicators.
        if sig.score != 0.0:
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


def score_rounds(model: MTFModel) -> list[dict[str, Any]]:
    """Score every evaluable 5m round once (threshold-independent).

    Each row carries the model score, confidence, predicted direction (sign of
    score), the realized outcome, and the raw features. Whether a round is
    *traded* is a later decision (confidence vs entry threshold), so these rows
    can be re-thresholded cheaply — which is what walk-forward tuning needs.
    """
    rows: list[dict[str, Any]] = []
    for rs in [b.ts for b in model.bars_5m]:
        sig = model.evaluate(rs)
        if sig is None or sig.direction is None:
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
    return rows


def metrics_from_rows(
    rows: list[dict[str, Any]],
    entry_threshold: float,
    entry_price: float,
) -> dict[str, Any]:
    """Compute accuracy / coverage / PnL for a set of scored rows at a threshold.

    A round is traded when its confidence >= entry_threshold.
    """
    evaluated = len(rows)
    trades = [r for r in rows if r["confidence"] >= entry_threshold]
    n_trades = len(trades)
    wins = sum(1 for r in trades if r["pred"] == r["actual"])
    acc = wins / n_trades if n_trades else 0.0

    # Baseline: naively follow the current-round impulse direction on every round.
    base_ok = base_n = 0
    for r in rows:
        mv = r["features"].get("btc_move_usd", 0.0)
        if mv == 0:
            continue
        base_n += 1
        if ("UP" if mv > 0 else "DOWN") == r["actual"]:
            base_ok += 1
    base_acc = base_ok / base_n if base_n else 0.0

    # Accuracy by confidence tercile (over traded rounds).
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
            v = r["score"] if key == "score" else r["features"].get(key)
            if v is not None:
                xs.append(v)
                ys.append(yy)
        c = pearson(xs, ys) if len(xs) >= 2 else None
        if c is not None:
            feature_corr[key] = round(c, 4)

    win_pnl = 1.0 - entry_price
    loss_pnl = -entry_price
    total_pnl = wins * win_pnl + (n_trades - wins) * loss_pnl
    ev = total_pnl / n_trades if n_trades else 0.0

    return {
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
        "entry_threshold": entry_threshold,
    }


TRADE_LOG_FEATURES = [
    "btc_move_usd", "rsi_1m", "macd_hist_1m",
    "ema_gap_5m", "ema_gap_15m", "rel_volume_1m",
]


def write_trade_log(path: str, rows: list[dict[str, Any]], entry_threshold: float) -> int:
    """Write a per-round CSV: one line per evaluated round, `taken` flags the
    ones that cleared the threshold, `result` is win/loss for taken trades.
    Lets you eyeball exactly which rounds were traded and which lost."""
    import csv

    header = (
        ["round_start", "utc_time", "score", "confidence", "pred", "taken", "actual", "result"]
        + TRADE_LOG_FEATURES
    )
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            taken = r["confidence"] >= entry_threshold
            result = ""
            if taken:
                result = "win" if r["pred"] == r["actual"] else "loss"
            iso = dt.datetime.fromtimestamp(r["round_start"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            feats = [round(r["features"][k], 4) if r["features"].get(k) is not None else "" for k in TRADE_LOG_FEATURES]
            w.writerow([
                r["round_start"], iso, round(r["score"], 5), round(r["confidence"], 5),
                r["pred"], int(taken), r["actual"], result, *feats,
            ])
    return len(rows)


def run_backtest(
    bars_1m: list[Bar],
    weights: Optional[dict[str, float]] = None,
    entry_threshold: float = 0.20,
    entry_price: float = 0.85,
    trade_log: Optional[str] = None,
) -> dict[str, Any]:
    model = MTFModel(bars_1m, weights=weights, entry_threshold=entry_threshold)
    rows = score_rounds(model)
    res = metrics_from_rows(rows, entry_threshold, entry_price)
    res["bars_1m"] = len(model.bars_1m)
    res["rounds_5m"] = len(model.bars_5m)
    res["weights"] = model.weights
    if trade_log:
        n = write_trade_log(trade_log, rows, entry_threshold)
        res["trade_log"] = {"path": trade_log, "rows": n}
    return res


def _best_threshold(train_rows: list[dict[str, Any]], entry_price: float,
                    grid: list[float], min_trades: int) -> tuple[float, float]:
    """Pick the entry threshold that maximizes in-sample EV/trade, requiring at
    least `min_trades` trades. Ties break toward higher coverage (lower
    threshold). Falls back to the lowest grid value if nothing qualifies."""
    best_thr = grid[0]
    best_ev = None
    for thr in grid:
        m = metrics_from_rows(train_rows, thr, entry_price)
        if m["trades"] < min_trades:
            continue
        ev = m["sim_pnl"]["ev_per_trade"]
        if best_ev is None or ev > best_ev + 1e-9:
            best_ev, best_thr = ev, thr
    return best_thr, (best_ev if best_ev is not None else float("nan"))


def run_walk_forward(
    bars_1m: list[Bar],
    weights: Optional[dict[str, float]] = None,
    entry_price: float = 0.85,
    train: int = 500,
    test: int = 250,
    min_trades: int = 15,
    grid: Optional[list[float]] = None,
    trade_log: Optional[str] = None,
) -> dict[str, Any]:
    """Walk-forward evaluation: slide a train/test window over the scored rounds,
    tune the entry threshold on each in-sample train block, and measure it purely
    on the following out-of-sample test block. Aggregates OOS results so the
    reported edge is never fit on the same data it's scored on."""
    model = MTFModel(bars_1m, weights=weights)
    rows = score_rounds(model)
    rows.sort(key=lambda r: r["round_start"])
    if grid is None:
        grid = [round(0.10 + 0.05 * i, 2) for i in range(18)]  # 0.10 .. 0.95

    folds = []
    oos_rows: list[dict[str, Any]] = []       # test rows that were traded, with the fold threshold
    oos_all_rows: list[dict[str, Any]] = []    # every test row (for the trade log)
    start = train
    while start + 1 <= len(rows):
        tr = rows[start - train:start]
        te = rows[start:start + test]
        if not te:
            break
        thr, tr_ev = _best_threshold(tr, entry_price, grid, min_trades)
        te_metrics = metrics_from_rows(te, thr, entry_price)
        for r in te:
            oos_all_rows.append({**r, "_thr": thr})
            if r["confidence"] >= thr:
                oos_rows.append(r)
        folds.append({
            "fold": len(folds) + 1,
            "train_rounds": len(tr),
            "test_rounds": len(te),
            "chosen_threshold": thr,
            "train_ev_per_trade": round(tr_ev, 5) if tr_ev == tr_ev else None,
            "oos_trades": te_metrics["trades"],
            "oos_accuracy": te_metrics["directional_accuracy"],
            "oos_ev_per_trade": te_metrics["sim_pnl"]["ev_per_trade"],
        })
        start += test

    # Aggregate out-of-sample.
    n = len(oos_rows)
    wins = sum(1 for r in oos_rows if r["pred"] == r["actual"])
    acc = wins / n if n else 0.0
    win_pnl, loss_pnl = 1.0 - entry_price, -entry_price
    total_pnl = wins * win_pnl + (n - wins) * loss_pnl
    thresholds = [f["chosen_threshold"] for f in folds]

    if trade_log:
        # Log OOS decisions: `taken` uses each row's own fold threshold.
        import csv
        with open(trade_log, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["round_start", "utc_time", "fold_threshold", "score", "confidence",
                        "pred", "taken", "actual", "result"] + TRADE_LOG_FEATURES)
            for r in oos_all_rows:
                taken = r["confidence"] >= r["_thr"]
                result = ("win" if r["pred"] == r["actual"] else "loss") if taken else ""
                iso = dt.datetime.fromtimestamp(r["round_start"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                feats = [round(r["features"][k], 4) if r["features"].get(k) is not None else "" for k in TRADE_LOG_FEATURES]
                w.writerow([r["round_start"], iso, r["_thr"], round(r["score"], 5),
                            round(r["confidence"], 5), r["pred"], int(taken), r["actual"], result, *feats])

    return {
        "mode": "walk_forward",
        "bars_1m": len(model.bars_1m),
        "rounds_5m": len(model.bars_5m),
        "scored_rounds": len(rows),
        "train_window": train,
        "test_window": test,
        "folds": folds,
        "chosen_threshold_range": [min(thresholds), max(thresholds)] if thresholds else None,
        "oos_trades": n,
        "oos_accuracy": round(acc, 4),
        "oos_edge_vs_breakeven": round(acc - entry_price, 4),
        "oos_ev_per_trade": round(total_pnl / n, 5) if n else 0.0,
        "oos_total_pnl_units": round(total_pnl, 3),
        "breakeven_winrate": entry_price,
        "weights": model.weights,
        "trade_log": {"path": trade_log, "rows": len(oos_all_rows)} if trade_log else None,
    }


def run_ablation(
    bars_1m: list[Bar],
    weights: Optional[dict[str, float]] = None,
    entry_price: float = 0.85,
    train: int = 500,
    test: int = 250,
    min_trades: int = 15,
) -> dict[str, Any]:
    """Leave-one-out feature ablation, all measured out-of-sample.

    Runs the walk-forward once with the full ensemble (baseline), then once per
    indicator with that indicator's weight zeroed. The drop in out-of-sample
    EV/trade when a feature is removed is its contribution: a positive
    contribution means the feature earns its weight; a negative one means it is
    dead weight (or actively hurting) and could be dropped.
    """
    base_w = dict(weights or DEFAULT_WEIGHTS)
    base = run_walk_forward(bars_1m, weights=base_w, entry_price=entry_price,
                            train=train, test=test, min_trades=min_trades)
    base_ev = base["oos_ev_per_trade"]
    base_acc = base["oos_accuracy"]

    ablations = []
    for feat in base_w:
        w = dict(base_w)
        w[feat] = 0.0
        r = run_walk_forward(bars_1m, weights=w, entry_price=entry_price,
                             train=train, test=test, min_trades=min_trades)
        ablations.append({
            "removed": feat,
            "oos_trades": r["oos_trades"],
            "oos_accuracy": r["oos_accuracy"],
            "oos_ev_per_trade": r["oos_ev_per_trade"],
            "ev_contribution": round(base_ev - r["oos_ev_per_trade"], 5),
            "acc_contribution": round(base_acc - r["oos_accuracy"], 4),
        })
    # Rank most-valuable first.
    ablations.sort(key=lambda a: a["ev_contribution"], reverse=True)

    return {
        "mode": "ablation",
        "bars_1m": base["bars_1m"],
        "rounds_5m": base["rounds_5m"],
        "train_window": train,
        "test_window": test,
        "baseline": {
            "oos_trades": base["oos_trades"],
            "oos_accuracy": base_acc,
            "oos_ev_per_trade": base_ev,
            "oos_edge_vs_breakeven": base["oos_edge_vs_breakeven"],
        },
        "breakeven_winrate": entry_price,
        "weights": base_w,
        "ablations": ablations,
    }


def rescore_rows(rows: list[dict[str, Any]], weights: dict[str, float]) -> list[dict[str, Any]]:
    """Recompute score / confidence / direction for pre-scored rows under a new
    weight vector, using only the stored sub-signals and volume factor. No
    indicators are recomputed, so weight search over many candidates is cheap."""
    out: list[dict[str, Any]] = []
    for r in rows:
        feats = r["features"]
        raw = 0.0
        for k, w in weights.items():
            sv = feats.get(f"sub_{k}")
            if sv is not None:
                raw += w * sv
        score = math.tanh(raw)
        vf = feats.get("vol_factor", 1.0)
        conf = clamp(abs(score) * vf, 0.0, 1.0)
        pred = None
        if score != 0.0:
            pred = "UP" if score > 0 else "DOWN"
        out.append({**r, "score": score, "confidence": conf, "pred": pred})
    return out


WEIGHT_CANDIDATES = [0.0, 0.25, 0.5, 1.0, 1.5]


def _optimize_weights(
    train_rows: list[dict[str, Any]],
    entry_price: float,
    grid_thr: list[float],
    min_trades: int,
) -> tuple[dict[str, float], float, float]:
    """Coordinate-ascent search for the weight vector that maximizes in-sample
    EV/trade (jointly picking the best entry threshold). Coordinate ascent is
    used over an exhaustive grid deliberately: it touches far fewer combinations,
    which keeps the in-sample fit shallow and less prone to overfitting — the
    out-of-sample test block is what ultimately judges it."""
    NEG = -1e9

    def eval_w(w: dict[str, float]) -> tuple[float, float]:
        rr = rescore_rows(train_rows, w)
        thr, ev = _best_threshold(rr, entry_price, grid_thr, min_trades)
        if ev != ev:  # nan => no qualifying threshold
            return NEG, thr
        return ev, thr

    weights = dict(DEFAULT_WEIGHTS)
    best_ev, best_thr = eval_w(weights)
    for _ in range(2):  # two refinement passes
        for k in list(weights):
            keep_val, keep_ev, keep_thr = weights[k], best_ev, best_thr
            for cv in WEIGHT_CANDIDATES:
                if cv == keep_val:
                    continue
                weights[k] = cv
                ev, thr = eval_w(weights)
                if ev > keep_ev + 1e-9:
                    keep_val, keep_ev, keep_thr = cv, ev, thr
            weights[k] = keep_val
            best_ev, best_thr = keep_ev, keep_thr
    return dict(weights), best_thr, (best_ev if best_ev > NEG else float("nan"))


def weight_opt_over_rows(
    rows: list[dict[str, Any]],
    entry_price: float = 0.85,
    train: int = 500,
    test: int = 250,
    min_trades: int = 15,
    grid: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Core walk-forward, fixed-vs-optimized comparison over pre-scored rows.
    Separated from data loading so a label-shuffle null test can drive it
    directly (see test suite)."""
    rows = sorted(rows, key=lambda r: r["round_start"])
    if grid is None:
        grid = [round(0.10 + 0.05 * i, 2) for i in range(18)]

    def agg(oos: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(oos)
        wins = sum(1 for r in oos if r["pred"] == r["actual"])
        acc = wins / n if n else 0.0
        win_pnl, loss_pnl = 1.0 - entry_price, -entry_price
        total = wins * win_pnl + (n - wins) * loss_pnl
        return {
            "oos_trades": n,
            "oos_accuracy": round(acc, 4),
            "oos_edge_vs_breakeven": round(acc - entry_price, 4),
            "oos_ev_per_trade": round(total / n, 5) if n else 0.0,
            "oos_total_pnl_units": round(total, 3),
        }

    base_oos: list[dict[str, Any]] = []
    opt_oos: list[dict[str, Any]] = []
    folds = []
    start = train
    while start + 1 <= len(rows):
        tr = rows[start - train:start]
        te = rows[start:start + test]
        if not te:
            break
        # Baseline: fixed default weights, tune threshold only.
        b_thr, _ = _best_threshold(tr, entry_price, grid, min_trades)
        base_oos.extend(r for r in te if r["confidence"] >= b_thr)
        # Optimized: tune weights + threshold on train, apply to test.
        w, o_thr, tr_ev = _optimize_weights(tr, entry_price, grid, min_trades)
        te_re = rescore_rows(te, w)
        opt_oos.extend(r for r in te_re if r["confidence"] >= o_thr)
        folds.append({
            "fold": len(folds) + 1,
            "baseline_threshold": b_thr,
            "opt_threshold": o_thr,
            "opt_weights": {k: round(v, 2) for k, v in w.items()},
            "train_opt_ev": round(tr_ev, 5) if tr_ev == tr_ev else None,
        })
        start += test

    baseline = agg(base_oos)
    optimized = agg(opt_oos)
    return {
        "scored_rounds": len(rows),
        "train_window": train,
        "test_window": test,
        "breakeven_winrate": entry_price,
        "default_weights": dict(DEFAULT_WEIGHTS),
        "folds": folds,
        "baseline_oos": baseline,
        "optimized_oos": optimized,
        "oos_ev_improvement": round(optimized["oos_ev_per_trade"] - baseline["oos_ev_per_trade"], 5),
        "verdict": (
            "optimization helps out-of-sample"
            if optimized["oos_ev_per_trade"] > baseline["oos_ev_per_trade"] + 1e-9
            else "fixed weights are as good or better (tuning would overfit)"
        ),
    }


def run_weight_optimization(
    bars_1m: list[Bar],
    entry_price: float = 0.85,
    train: int = 500,
    test: int = 250,
    min_trades: int = 15,
    grid: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Head-to-head, out-of-sample: fixed default weights vs per-fold optimized
    weights. Both tune only on each train block and are scored on the following
    unseen test block, so the comparison is honest. If optimization does not beat
    the fixed baseline out-of-sample, that is itself the finding (the hand-set
    weights are good enough and tuning would be curve-fitting)."""
    model = MTFModel(bars_1m)
    rows = score_rounds(model)
    res = weight_opt_over_rows(rows, entry_price, train, test, min_trades, grid)
    res["mode"] = "weight_optimization"
    res["bars_1m"] = len(model.bars_1m)
    res["rounds_5m"] = len(model.bars_5m)
    return res


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


def fetch_binance_1m(days: float = 7, symbol: str = "BTCUSDT") -> list[Bar]:
    """Fetch recent 1m klines from Binance via the shared connector. Runs on your
    PC; blocked in restricted network sandboxes (use --synth or --csv there)."""
    from btc_binance import download_history

    rows = download_history(symbol=symbol, interval="1m", days=days)
    return [Bar(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


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
    if res.get("trade_log"):
        print(f"trade log written    : {res['trade_log']['path']} ({res['trade_log']['rows']} rows)")
    print("=" * 60)


def _print_wf_report(res: dict[str, Any]) -> None:
    print("=" * 68)
    print("BTC 5m Walk-Forward Backtest (out-of-sample)")
    print("=" * 68)
    print(f"1m bars / 5m rounds  : {res['bars_1m']} / {res['rounds_5m']}")
    print(f"scored rounds        : {res['scored_rounds']}")
    print(f"train/test window    : {res['train_window']} / {res['test_window']} rounds")
    print(f"folds                : {len(res['folds'])}")
    print(f"chosen threshold rng : {res['chosen_threshold_range']}")
    print("-" * 68)
    print(f"{'fold':<5}{'thr':<7}{'train_ev':<11}{'oos_trades':<12}{'oos_acc':<10}{'oos_ev':<10}")
    for f in res["folds"]:
        tev = f["train_ev_per_trade"]
        print(f"{f['fold']:<5}{f['chosen_threshold']:<7}"
              f"{(f'{tev:+.4f}' if tev is not None else 'n/a'):<11}"
              f"{f['oos_trades']:<12}{f['oos_accuracy']:<10.3f}{f['oos_ev_per_trade']:<+10.4f}")
    print("-" * 68)
    print(f"AGGREGATE out-of-sample ({res['oos_trades']} trades):")
    print(f"  accuracy           : {res['oos_accuracy']:.1%}")
    print(f"  breakeven win-rate : {res['breakeven_winrate']:.1%}")
    print(f"  edge vs breakeven  : {res['oos_edge_vs_breakeven']:+.1%}")
    print(f"  EV/trade           : {res['oos_ev_per_trade']:+.4f} units")
    print(f"  total PnL          : {res['oos_total_pnl_units']:+.2f} units")
    if res.get("trade_log"):
        print(f"  trade log          : {res['trade_log']['path']} ({res['trade_log']['rows']} rows)")
    print("=" * 68)


def _print_ablation_report(res: dict[str, Any]) -> None:
    b = res["baseline"]
    print("=" * 72)
    print("BTC 5m Feature Ablation (leave-one-out, out-of-sample)")
    print("=" * 72)
    print(f"1m bars / 5m rounds  : {res['bars_1m']} / {res['rounds_5m']}")
    print(f"train/test window    : {res['train_window']} / {res['test_window']} rounds")
    print(f"breakeven win-rate   : {res['breakeven_winrate']:.1%}")
    print("-" * 72)
    print(f"BASELINE (full ensemble): OOS acc {b['oos_accuracy']:.1%}  "
          f"EV/trade {b['oos_ev_per_trade']:+.4f}  trades {b['oos_trades']}  "
          f"edge {b['oos_edge_vs_breakeven']:+.1%}")
    print("-" * 72)
    print("drop-one-feature (ranked by EV contribution; higher = more valuable):")
    print(f"  {'removed':<14}{'oos_acc':<10}{'oos_ev':<11}{'EV contrib':<13}{'acc contrib':<12}")
    for a in res["ablations"]:
        flag = "  <- earns weight" if a["ev_contribution"] > 0 else ("  <- dead/hurts" if a["ev_contribution"] < 0 else "")
        print(f"  {a['removed']:<14}{a['oos_accuracy']:<10.3f}{a['oos_ev_per_trade']:<+11.4f}"
              f"{a['ev_contribution']:<+13.4f}{a['acc_contribution']:<+12.4f}{flag}")
    print("-" * 72)
    print("Positive EV contribution => removing the feature lowers out-of-sample")
    print("EV, i.e. it is pulling its weight. Negative => consider dropping it.")
    print("=" * 72)


def _print_weight_opt_report(res: dict[str, Any]) -> None:
    b, o = res["baseline_oos"], res["optimized_oos"]
    print("=" * 72)
    print("BTC 5m Weight Optimization -- Fixed vs Tuned (out-of-sample)")
    print("=" * 72)
    print(f"1m bars / 5m rounds  : {res['bars_1m']} / {res['rounds_5m']}")
    print(f"train/test window    : {res['train_window']} / {res['test_window']} rounds  ({len(res['folds'])} folds)")
    print(f"breakeven win-rate   : {res['breakeven_winrate']:.1%}")
    print("-" * 72)
    print(f"{'':<22}{'trades':<10}{'accuracy':<12}{'EV/trade':<12}{'edge':<10}")
    print(f"{'baseline (fixed W)':<22}{b['oos_trades']:<10}{b['oos_accuracy']:<12.1%}"
          f"{b['oos_ev_per_trade']:<+12.4f}{b['oos_edge_vs_breakeven']:<+10.1%}")
    print(f"{'optimized (tuned W)':<22}{o['oos_trades']:<10}{o['oos_accuracy']:<12.1%}"
          f"{o['oos_ev_per_trade']:<+12.4f}{o['oos_edge_vs_breakeven']:<+10.1%}")
    print("-" * 72)
    print(f"OOS EV improvement   : {res['oos_ev_improvement']:+.4f} units/trade")
    print(f"VERDICT              : {res['verdict']}")
    print("-" * 72)
    print("per-fold optimized weights (impulse/rsi/macd/trend5/trend15):")
    for f in res["folds"]:
        w = f["opt_weights"]
        print(f"  fold {f['fold']:<2} thr={f['opt_threshold']:<5} "
              f"[{w['impulse_1m']}, {w['rsi_1m']}, {w['macd_1m']}, {w['trend_5m']}, {w['trend_15m']}]")
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-timeframe BTC 5m backtester")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="Load 1m OHLCV CSV")
    src.add_argument("--fetch", metavar="PROVIDER", choices=["binance", "cryptocompare"],
                     help="Fetch 1m OHLCV from a provider (needs network): binance | cryptocompare")
    src.add_argument("--fetch-binance", action="store_true", help="Alias for --fetch binance")
    src.add_argument("--synth", type=int, metavar="N", help="Generate N synthetic 1m bars (offline)")
    ap.add_argument("--days", type=float, default=7, help="Days of history for --fetch")
    ap.add_argument("--autocorr", type=float, default=0.0, help="Momentum for --synth (0=random walk)")
    ap.add_argument("--entry-threshold", type=float, default=0.20, help="Min confidence to take a trade (ignored in --walk-forward, which tunes it)")
    ap.add_argument("--entry-price", type=float, default=0.85, help="Assumed contract entry price (0.80-0.99)")
    ap.add_argument("--trade-log", metavar="PATH", help="Write a per-round CSV of every decision (taken/skipped, win/loss, features)")
    ap.add_argument("--walk-forward", action="store_true", help="Out-of-sample eval: tune the threshold on rolling train blocks, score on the next test block")
    ap.add_argument("--ablation", action="store_true", help="Leave-one-out feature ablation (out-of-sample); shows each indicator's EV contribution")
    ap.add_argument("--optimize-weights", action="store_true", help="Out-of-sample head-to-head: fixed default weights vs per-fold tuned weights")
    ap.add_argument("--wf-train", type=int, default=500, help="Train window in 5m rounds (walk-forward)")
    ap.add_argument("--wf-test", type=int, default=250, help="Test window in 5m rounds (walk-forward)")
    ap.add_argument("--wf-min-trades", type=int, default=15, help="Min in-sample trades required when tuning a fold's threshold")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    args = ap.parse_args()

    if args.csv:
        bars = load_csv(args.csv)
    elif args.fetch or args.fetch_binance:
        from btc_history import fetch_history
        provider = args.fetch or "binance"
        rows = fetch_history(provider, days=args.days)
        bars = [Bar(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]
    else:
        bars = synth_bars(args.synth, autocorr=args.autocorr)

    if args.optimize_weights:
        res = run_weight_optimization(
            bars,
            entry_price=args.entry_price,
            train=args.wf_train,
            test=args.wf_test,
            min_trades=args.wf_min_trades,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            _print_weight_opt_report(res)
        return 0

    if args.ablation:
        res = run_ablation(
            bars,
            entry_price=args.entry_price,
            train=args.wf_train,
            test=args.wf_test,
            min_trades=args.wf_min_trades,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            _print_ablation_report(res)
        return 0

    if args.walk_forward:
        res = run_walk_forward(
            bars,
            entry_price=args.entry_price,
            train=args.wf_train,
            test=args.wf_test,
            min_trades=args.wf_min_trades,
            trade_log=args.trade_log,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            _print_wf_report(res)
        return 0

    res = run_backtest(
        bars,
        entry_threshold=args.entry_threshold,
        entry_price=args.entry_price,
        trade_log=args.trade_log,
    )
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _print_report(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
