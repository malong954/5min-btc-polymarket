#!/usr/bin/env python3
"""Parallel, fee-aware signal research for BTC 5-minute Up/Down markets.

The finder replays recorded trajectory snapshots and evaluates independent signal
families on the same immutable decision snapshot. It deliberately uses only
Polymarket's official resolution, delays fills to a later quote, limits fills to
recorded top-of-book size, and keeps whole UTC days in chronological
train/validation/holdout partitions.

This is a shadow research tool. It does not place orders.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


SIDES = ("UP", "DOWN")


@dataclass
class Dataset:
    samples: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    outcomes: dict[int, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    events_read: int = 0
    duplicate_samples: int = 0
    conflicting_labels: int = 0


@dataclass
class Config:
    decision_sec: float = 210.0
    min_decision_sec: float = 180.0
    latency_sec: float = 6.0
    max_fill_delay_sec: float = 15.0
    min_price: float = 0.05
    max_price: float = 0.85
    min_size: float = 5.0
    stake_usd: float = 10.0
    slippage: float = 0.01
    fee_rate: float = 0.07
    min_move_frac: float = 0.00016
    book_move_min: float = 0.01
    book_lookback_sec: float = 30.0
    book_favorite_min: float = 0.02
    pressure_min: float = 0.10
    train_frac: float = 0.60
    validation_frac: float = 0.20
    min_partition_trades: int = 30


@dataclass(frozen=True)
class Signal:
    side: str
    strength: float


@dataclass
class Context:
    round_start: int
    decision: dict[str, Any]
    history: list[dict[str, Any]]
    fill: dict[str, Any]
    fill_delay_sec: float


Arm = Callable[[Context, Config], Optional[Signal]]


def discover_btc_trajectory_files(data_dir: str) -> list[Path]:
    """Return BTC 5m trajectory archives, excluding suffixed alt-asset files."""
    paths = []
    for path in sorted(Path(data_dir).glob("trajectory*.jsonl.gz")):
        name = path.name.lower()
        if any(tag in name for tag in ("-eth", "-sol", "-xrp", "btc-15m")):
            continue
        paths.append(path)
    return paths


def _sample_richness(sample: dict[str, Any]) -> int:
    keys = ("up_ask", "dn_ask", "up_sz", "dn_sz", "up_bid", "dn_bid",
            "up_bid_sz", "dn_bid_sz", "ind_dir", "ind_conf")
    return sum(sample.get(key) is not None for key in keys)


def load_dataset(paths: list[Path]) -> Dataset:
    """Stream gzip JSONL archives and retain samples plus official labels only."""
    data = Dataset(files=[str(path) for path in paths])
    sample_index: dict[tuple[Any, ...], dict[str, Any]] = {}
    for path in paths:
        with gzip.open(path, "rt") as handle:
            for line in handle:
                data.events_read += 1
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                typ = event.get("type")
                round_start = event.get("round")
                if not isinstance(round_start, (int, float)):
                    continue
                round_start = int(round_start)
                if typ == "result_pm" and event.get("outcome") in SIDES:
                    prior = data.outcomes.get(round_start)
                    if prior is not None and prior != event["outcome"]:
                        data.conflicting_labels += 1
                    data.outcomes[round_start] = event["outcome"]
                elif typ == "sample":
                    sec_left = event.get("sec_left")
                    if not isinstance(sec_left, (int, float)):
                        continue
                    ts = event.get("ts")
                    identity = (round_start, ts if isinstance(ts, (int, float)) else None,
                                round(float(sec_left), 3))
                    prior = sample_index.get(identity)
                    if prior is not None:
                        data.duplicate_samples += 1
                        if _sample_richness(event) <= _sample_richness(prior):
                            continue
                        data.samples[round_start].remove(prior)
                    sample_index[identity] = event
                    data.samples.setdefault(round_start, []).append(event)
    for samples in data.samples.values():
        samples.sort(key=_sample_time)
    return data


def _sample_time(sample: dict[str, Any]) -> tuple[float, float]:
    ts = sample.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts), -float(sample.get("sec_left") or 0.0)
    return -float(sample.get("sec_left") or 0.0), 0.0


def _elapsed(earlier: dict[str, Any], later: dict[str, Any]) -> float:
    t0, t1 = earlier.get("ts"), later.get("ts")
    if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
        return float(t1) - float(t0)
    return float(earlier.get("sec_left") or 0.0) - float(later.get("sec_left") or 0.0)


def build_context(round_start: int, samples: list[dict[str, Any]], cfg: Config) -> Optional[Context]:
    """Freeze one decision snapshot and select the first sufficiently delayed quote."""
    decision_i = None
    for i, sample in enumerate(samples):
        sec_left = sample.get("sec_left")
        if not isinstance(sec_left, (int, float)):
            continue
        if cfg.min_decision_sec <= float(sec_left) <= cfg.decision_sec:
            decision_i = i
            break
    if decision_i is None:
        return None
    decision = samples[decision_i]
    if cfg.latency_sec <= 0:
        fill, delay = decision, 0.0
    else:
        fill = None
        delay = 0.0
        for later in samples[decision_i + 1:]:
            elapsed = _elapsed(decision, later)
            if elapsed >= cfg.latency_sec:
                fill, delay = later, elapsed
                break
        if fill is None or delay > cfg.max_fill_delay_sec:
            return None
    return Context(round_start, decision, samples[:decision_i], fill, delay)


def _number(sample: dict[str, Any], key: str) -> Optional[float]:
    value = sample.get(key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _opposite(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


def _spot_signal(ctx: Context, cfg: Config) -> Optional[Signal]:
    move = _number(ctx.decision, "move")
    spot = _number(ctx.decision, "spot")
    if move is None or spot is None or spot <= 0 or abs(move) < spot * cfg.min_move_frac:
        return None
    return Signal("UP" if move > 0 else "DOWN", abs(move) / spot)


def arm_spot_lead(ctx: Context, cfg: Config) -> Optional[Signal]:
    return _spot_signal(ctx, cfg)


def arm_spot_fade(ctx: Context, cfg: Config) -> Optional[Signal]:
    sig = _spot_signal(ctx, cfg)
    return Signal(_opposite(sig.side), sig.strength) if sig else None


def arm_indicator(ctx: Context, _cfg: Config) -> Optional[Signal]:
    side = ctx.decision.get("ind_dir")
    confidence = _number(ctx.decision, "ind_conf")
    if side not in SIDES or confidence is None:
        return None
    return Signal(side, confidence)


def _velocity_arm(field: str) -> Arm:
    def evaluate(ctx: Context, _cfg: Config) -> Optional[Signal]:
        velocity = _number(ctx.decision, field)
        if velocity is None or velocity == 0:
            return None
        return Signal("UP" if velocity > 0 else "DOWN", abs(velocity))
    return evaluate


def _book_probability(sample: dict[str, Any]) -> Optional[float]:
    up_ask, dn_ask = _number(sample, "up_ask"), _number(sample, "dn_ask")
    if up_ask is None or dn_ask is None:
        return None
    up_bid, dn_bid = _number(sample, "up_bid"), _number(sample, "dn_bid")
    up = (up_ask + up_bid) / 2.0 if up_bid is not None else up_ask
    down = (dn_ask + dn_bid) / 2.0 if dn_bid is not None else dn_ask
    total = up + down
    return up / total if total > 0 else None


def _book_favorite_signal(ctx: Context, cfg: Config) -> Optional[Signal]:
    probability = _book_probability(ctx.decision)
    if probability is None or abs(probability - 0.5) < cfg.book_favorite_min:
        return None
    return Signal("UP" if probability > 0.5 else "DOWN", abs(probability - 0.5) * 2.0)


def arm_book_favorite(ctx: Context, cfg: Config) -> Optional[Signal]:
    return _book_favorite_signal(ctx, cfg)


def arm_book_underdog(ctx: Context, cfg: Config) -> Optional[Signal]:
    sig = _book_favorite_signal(ctx, cfg)
    return Signal(_opposite(sig.side), sig.strength) if sig else None


def _prior_sample(ctx: Context, lookback_sec: float) -> Optional[dict[str, Any]]:
    eligible = [sample for sample in ctx.history if _elapsed(sample, ctx.decision) >= lookback_sec]
    return eligible[-1] if eligible else None


def _book_momentum_signal(ctx: Context, cfg: Config) -> Optional[Signal]:
    prior = _prior_sample(ctx, cfg.book_lookback_sec)
    now_p = _book_probability(ctx.decision)
    prior_p = _book_probability(prior) if prior is not None else None
    if now_p is None or prior_p is None or abs(now_p - prior_p) < cfg.book_move_min:
        return None
    return Signal("UP" if now_p > prior_p else "DOWN", abs(now_p - prior_p))


def arm_book_momentum(ctx: Context, cfg: Config) -> Optional[Signal]:
    return _book_momentum_signal(ctx, cfg)


def arm_book_reversal(ctx: Context, cfg: Config) -> Optional[Signal]:
    sig = _book_momentum_signal(ctx, cfg)
    return Signal(_opposite(sig.side), sig.strength) if sig else None


def arm_liquidity_pressure(ctx: Context, cfg: Config) -> Optional[Signal]:
    values = [_number(ctx.decision, key) for key in
              ("up_sz", "dn_sz", "up_bid_sz", "dn_bid_sz")]
    if any(value is None or value <= 0 for value in values):
        return None
    up_ask_sz, dn_ask_sz, up_bid_sz, dn_bid_sz = values
    bid_pressure = up_bid_sz / (up_bid_sz + dn_bid_sz)
    ask_scarcity = dn_ask_sz / (up_ask_sz + dn_ask_sz)
    pressure = (bid_pressure + ask_scarcity) / 2.0
    if abs(pressure - 0.5) < cfg.pressure_min:
        return None
    return Signal("UP" if pressure > 0.5 else "DOWN", abs(pressure - 0.5) * 2.0)


def arm_velocity_consensus(ctx: Context, _cfg: Config) -> Optional[Signal]:
    votes = []
    for field in ("vel_5s", "vel_15s", "vel_30s", "vel_60s"):
        velocity = _number(ctx.decision, field)
        if velocity is not None and velocity != 0:
            votes.append(1 if velocity > 0 else -1)
    if len(votes) < 3 or sum(votes) == 0:
        return None
    return Signal("UP" if sum(votes) > 0 else "DOWN", abs(sum(votes)) / len(votes))


def arm_spot_book_consensus(ctx: Context, cfg: Config) -> Optional[Signal]:
    spot, book = _spot_signal(ctx, cfg), _book_favorite_signal(ctx, cfg)
    if spot is None or book is None or spot.side != book.side:
        return None
    return Signal(spot.side, (spot.strength + book.strength) / 2.0)


def arm_spot_velocity_consensus(ctx: Context, cfg: Config) -> Optional[Signal]:
    spot, velocity = _spot_signal(ctx, cfg), arm_velocity_consensus(ctx, cfg)
    if spot is None or velocity is None or spot.side != velocity.side:
        return None
    return Signal(spot.side, (spot.strength + velocity.strength) / 2.0)


ARMS: dict[str, Arm] = {
    "spot_lead": arm_spot_lead,
    "spot_fade_control": arm_spot_fade,
    "indicator": arm_indicator,
    "velocity_5s": _velocity_arm("vel_5s"),
    "velocity_15s": _velocity_arm("vel_15s"),
    "velocity_30s": _velocity_arm("vel_30s"),
    "velocity_60s": _velocity_arm("vel_60s"),
    "velocity_consensus": arm_velocity_consensus,
    "book_favorite": arm_book_favorite,
    "book_underdog_control": arm_book_underdog,
    "book_momentum": arm_book_momentum,
    "book_reversal_control": arm_book_reversal,
    "liquidity_pressure": arm_liquidity_pressure,
    "spot_book_consensus": arm_spot_book_consensus,
    "spot_velocity_consensus": arm_spot_velocity_consensus,
}


def make_trade(arm_name: str, signal: Signal, ctx: Context, outcome: str,
               partition: str, cfg: Config) -> Optional[dict[str, Any]]:
    prefix = "up" if signal.side == "UP" else "dn"
    raw_price = _number(ctx.fill, f"{prefix}_ask")
    ask_size = _number(ctx.fill, f"{prefix}_sz")
    if raw_price is None or ask_size is None:
        return None
    price = min(0.99999, raw_price + cfg.slippage)
    if not (cfg.min_price <= price <= cfg.max_price) or ask_size < cfg.min_size:
        return None
    requested_shares = cfg.stake_usd / price
    shares = min(requested_shares, ask_size)
    if shares < cfg.min_size:
        return None
    won = signal.side == outcome
    fee_per_share = cfg.fee_rate * price * (1.0 - price)
    fee = shares * fee_per_share
    cost = shares * price
    pnl = shares * (1.0 if won else 0.0) - cost - fee
    per_share = (1.0 if won else 0.0) - price - fee_per_share
    full_depth_pnl = ask_size * per_share
    return {
        "arm": arm_name,
        "round": ctx.round_start,
        "partition": partition,
        "side": signal.side,
        "outcome": outcome,
        "win": won,
        "strength": signal.strength,
        "decision_sec_left": float(ctx.decision["sec_left"]),
        "fill_sec_left": float(ctx.fill["sec_left"]),
        "fill_delay_sec": ctx.fill_delay_sec,
        "raw_ask": raw_price,
        "price": price,
        "ask_size": ask_size,
        "shares": shares,
        "cost": cost,
        "fee": fee,
        "pnl": pnl,
        "pnl_per_share": per_share,
        "full_depth_pnl": full_depth_pnl,
    }


def partition_days(rounds: list[int], train_frac: float,
                   validation_frac: float) -> tuple[dict[int, str], dict[str, list[int]]]:
    days = sorted({round_start // 86400 for round_start in rounds})
    if len(days) < 3:
        raise ValueError("at least three UTC days with official labels are required")
    n_train = max(1, int(len(days) * train_frac))
    n_validation = max(1, int(len(days) * validation_frac))
    if n_train + n_validation >= len(days):
        n_validation = 1
        n_train = len(days) - 2
    train_days = days[:n_train]
    validation_days = days[n_train:n_train + n_validation]
    test_days = days[n_train + n_validation:]
    by_day = {day: "train" for day in train_days}
    by_day.update({day: "validation" for day in validation_days})
    by_day.update({day: "test" for day in test_days})
    return by_day, {"train": train_days, "validation": validation_days, "test": test_days}


def _day_cluster_stats(trades: list[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    by_day: dict[int, list[float]] = {}
    for trade in trades:
        by_day.setdefault(trade["round"] // 86400, []).append(trade["pnl_per_share"])
    means = [sum(values) / len(values) for values in by_day.values()]
    if len(means) < 2:
        return None, None
    mean = statistics.mean(means)
    se = statistics.stdev(means) / math.sqrt(len(means))
    if se <= 0:
        return (float("inf") if mean > 0 else float("-inf")), mean
    z = mean / se
    lower_95 = mean - 1.96 * se
    return z, lower_95


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    n = len(trades)
    wins = sum(1 for trade in trades if trade["win"])
    total_cost = sum(trade["cost"] + trade["fee"] for trade in trades)
    total_pnl = sum(trade["pnl"] for trade in trades)
    net_edge = sum(trade["pnl_per_share"] for trade in trades) / n
    days = sorted({trade["round"] // 86400 for trade in trades})
    calendar_days = days[-1] - days[0] + 1
    z, lower_95 = _day_cluster_stats(trades)
    p_one_sided = None
    if z is not None:
        p_one_sided = 0.5 * math.erfc(z / math.sqrt(2.0))
    roi = total_pnl / total_cost if total_cost else 0.0
    trades_per_day = n / calendar_days
    required_stake = 100.0 / (trades_per_day * roi) if roi > 0 and trades_per_day > 0 else None
    running = peak = max_drawdown = 0.0
    for trade in sorted(trades, key=lambda item: item["round"]):
        running += trade["pnl"]
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": wins / n,
        "avg_price": sum(trade["price"] for trade in trades) / n,
        "avg_fill_delay_sec": sum(trade["fill_delay_sec"] for trade in trades) / n,
        "net_edge_per_share": net_edge,
        "net_return_on_cost": roi,
        "pnl_usd": total_pnl,
        "pnl_usd_per_day": total_pnl / calendar_days,
        "top_level_capacity_pnl_per_day": sum(trade["full_depth_pnl"] for trade in trades) / calendar_days,
        "max_drawdown_usd": max_drawdown,
        "calendar_days": calendar_days,
        "trades_per_day": trades_per_day,
        "required_stake_per_trade_for_100_day": required_stake,
        "day_cluster_z": z,
        "lower_95_edge_per_share": lower_95,
        "p_one_sided": p_one_sided,
    }


def run_finder(data: Dataset, cfg: Config) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    labelled_rounds = sorted(set(data.samples).intersection(data.outcomes))
    by_day, split_days = partition_days(labelled_rounds, cfg.train_frac, cfg.validation_frac)
    trades: list[dict[str, Any]] = []
    contexts = 0
    for round_start in labelled_rounds:
        ctx = build_context(round_start, data.samples[round_start], cfg)
        if ctx is None:
            continue
        contexts += 1
        partition = by_day[round_start // 86400]
        for arm_name, arm in ARMS.items():
            signal = arm(ctx, cfg)
            if signal is None:
                continue
            trade = make_trade(arm_name, signal, ctx, data.outcomes[round_start], partition, cfg)
            if trade is not None:
                trades.append(trade)

    summaries: dict[str, dict[str, Any]] = {}
    tested_arms = 0
    for arm_name in ARMS:
        arm_trades = [trade for trade in trades if trade["arm"] == arm_name]
        summary = {part: summarize([trade for trade in arm_trades if trade["partition"] == part])
                   for part in ("train", "validation", "test")}
        summary["all"] = summarize(arm_trades)
        if summary["test"].get("trades", 0):
            tested_arms += 1
        summaries[arm_name] = summary

    for summary in summaries.values():
        test = summary["test"]
        p_value = test.get("p_one_sided")
        test["p_bonferroni"] = min(1.0, p_value * tested_arms) if p_value is not None else None
        edges = [summary[part].get("net_edge_per_share") for part in
                 ("train", "validation", "test")]
        counts = [summary[part].get("trades", 0) for part in
                  ("train", "validation", "test")]
        summary["stable_positive"] = (
            all(edge is not None and edge > 0 for edge in edges)
            and all(count >= cfg.min_partition_trades for count in counts)
        )
        summary["statistically_validated"] = (
            summary["stable_positive"]
            and test.get("lower_95_edge_per_share") is not None
            and test["lower_95_edge_per_share"] > 0
            and test.get("p_bonferroni") is not None
            and test["p_bonferroni"] < 0.05
        )

    eligible = []
    for arm_name, summary in summaries.items():
        train, validation = summary["train"], summary["validation"]
        if (train.get("trades", 0) >= cfg.min_partition_trades
                and validation.get("trades", 0) >= cfg.min_partition_trades
                and train.get("net_edge_per_share", 0) > 0
                and validation.get("net_edge_per_share", 0) > 0):
            discovery_score = min(train["net_edge_per_share"], validation["net_edge_per_share"])
            eligible.append((discovery_score, arm_name))
    eligible.sort(reverse=True)
    selected = eligible[0][1] if eligible else None
    if selected is None:
        verdict = "no_candidate_survived_train_and_validation"
    elif summaries[selected]["statistically_validated"]:
        verdict = "candidate_survived_holdout_and_multiple_testing"
    elif summaries[selected]["stable_positive"]:
        verdict = "positive_all_splits_but_not_statistically_proven"
    else:
        verdict = "selected_candidate_failed_holdout"

    report = {
        "method": {
            "official_labels_only": True,
            "one_decision_per_arm_per_round": True,
            "delayed_fill": True,
            "top_of_book_size_capped": True,
            "whole_utc_day_splits": True,
            "multiple_testing": f"Bonferroni across {tested_arms} holdout-tested arms",
        },
        "dataset": {
            "files": len(data.files),
            "events_read": data.events_read,
            "sample_rounds": len(data.samples),
            "official_rounds": len(data.outcomes),
            "labelled_sample_rounds": len(labelled_rounds),
            "evaluable_contexts": contexts,
            "duplicate_samples_removed": data.duplicate_samples,
            "conflicting_official_labels": data.conflicting_labels,
        },
        "config": vars(cfg),
        "split_days": split_days,
        "selected_on_train_validation": selected,
        "verdict": verdict,
        "summaries": summaries,
    }
    return report, trades


def _fmt(value: Any, kind: str = "number") -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value:+.2%}"
    if kind == "usd":
        return f"${value:+.2f}"
    return f"{value:.3f}"


def print_report(report: dict[str, Any]) -> None:
    data = report["dataset"]
    cfg = report["config"]
    print("=" * 104)
    print("BTC 5m Parallel Trend Finder (official labels, delayed fee-aware fills)")
    print("=" * 104)
    print(f"files={data['files']} events={data['events_read']:,} official rounds={data['official_rounds']:,} "
          f"evaluable={data['evaluable_contexts']:,}")
    print(f"decision={cfg['decision_sec']:.0f}s left | fill latency>={cfg['latency_sec']:.0f}s | "
          f"price={cfg['min_price']:.2f}-{cfg['max_price']:.2f} | slippage={cfg['slippage']:.2f} | "
          f"crypto taker fee rate={cfg['fee_rate']:.2f}")
    print("-" * 104)
    print(f"{'arm':<28}{'train n/edge':<19}{'valid n/edge':<19}{'holdout n/edge':<21}"
          f"{'holdout $/day':<16}{'status'}")
    for arm_name, summary in sorted(
            report["summaries"].items(),
            key=lambda item: item[1]["validation"].get("net_edge_per_share", -999), reverse=True):
        cells = []
        for part in ("train", "validation", "test"):
            metrics = summary[part]
            cells.append(f"{metrics.get('trades', 0):>4}/"
                         f"{_fmt(metrics.get('net_edge_per_share'), 'pct'):>8}")
        status = "VALIDATED" if summary["statistically_validated"] else (
            "positive 3/3" if summary["stable_positive"] else "not stable")
        print(f"{arm_name:<28}{cells[0]:<19}{cells[1]:<19}{cells[2]:<21}"
              f"{_fmt(summary['test'].get('pnl_usd_per_day'), 'usd'):<16}{status}")
    print("-" * 104)
    selected = report["selected_on_train_validation"]
    print(f"selected before holdout: {selected or 'none'}")
    print(f"verdict: {report['verdict']}")
    if selected:
        test = report["summaries"][selected]["test"]
        print(f"holdout: {test.get('trades', 0)} trades, win {_fmt(test.get('win_rate'), 'pct')}, "
              f"avg price {_fmt(test.get('avg_price'))}, net edge/share "
              f"{_fmt(test.get('net_edge_per_share'), 'pct')}, Bonferroni p="
              f"{_fmt(test.get('p_bonferroni'))}")
        print(f"$100/day linear stake estimate: "
              f"{_fmt(test.get('required_stake_per_trade_for_100_day'), 'usd')} per trade; "
              "not a guarantee and invalid above available depth")
    print("=" * 104)


def write_trade_log(path: str, trades: list[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(trades[0]) if trades else ["arm", "round", "partition"]
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Parallel BTC 5m trend research over trajectory archives")
    parser.add_argument("--data-dir", default="data/lab")
    parser.add_argument("--decision-sec", type=float, default=210.0)
    parser.add_argument("--min-decision-sec", type=float, default=180.0)
    parser.add_argument("--latency-sec", type=float, default=6.0)
    parser.add_argument("--max-fill-delay-sec", type=float, default=15.0)
    parser.add_argument("--min-price", type=float, default=0.05)
    parser.add_argument("--max-price", type=float, default=0.85)
    parser.add_argument("--min-size", type=float, default=5.0)
    parser.add_argument("--stake-usd", type=float, default=10.0)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--fee-rate", type=float, default=0.07,
                        help="Current Polymarket crypto taker rate in C*rate*p*(1-p)")
    parser.add_argument("--min-move-frac", type=float, default=0.00016)
    parser.add_argument("--trade-log")
    parser.add_argument("--output", help="Write full JSON report")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    paths = discover_btc_trajectory_files(args.data_dir)
    if not paths:
        parser.error(f"no BTC trajectory*.jsonl.gz files found under {args.data_dir}")
    cfg = Config(
        decision_sec=args.decision_sec,
        min_decision_sec=args.min_decision_sec,
        latency_sec=args.latency_sec,
        max_fill_delay_sec=args.max_fill_delay_sec,
        min_price=args.min_price,
        max_price=args.max_price,
        min_size=args.min_size,
        stake_usd=args.stake_usd,
        slippage=args.slippage,
        fee_rate=args.fee_rate,
        min_move_frac=args.min_move_frac,
    )
    report, trades = run_finder(load_dataset(paths), cfg)
    if args.trade_log:
        write_trade_log(args.trade_log, trades)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
