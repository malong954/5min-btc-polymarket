#!/usr/bin/env python3
"""
Position sizing for the BTC 5m binary-contract strategy.

The economics (buy at price c, pays $1): staking $s buys s/c shares; a WIN
profits s*(1-c)/c, a LOSS forfeits -s. So EV per trade = s*(p-c)/c — a flat
stake s is a pure magnitude multiplier and CANNOT change the sign of EV. Only
p > c (win rate above the entry price) makes a trade +EV. Sizing therefore does
not rescue a losing edge; it only shapes how a *positive* edge compounds.

Modes:
  flat        : constant base_stake every trade.
  confidence  : base_stake scaled linearly by the model confidence in [0,1].
  kelly       : fractional Kelly on an estimated win probability p; stakes ZERO
                when p <= c (the mathematically correct "don't bet" case),
                capped and multiplied down to avoid full-Kelly variance.

Kelly needs a *probability*, and the model's confidence is NOT one — calibrate
confidence -> empirical win rate on training data first (Calibrator).
"""

from __future__ import annotations

from typing import Any, Optional


def kelly_fraction(p: float, entry_price: float) -> float:
    """Kelly fraction for a binary contract bought at `entry_price` paying $1.

    Net odds b = (1-c)/c (a $1 stake wins b). f* = (p*b - (1-p)) / b = p - (1-p)/b.
    Negative when p < c (the bet is -EV -> optimal size is zero/short)."""
    c = entry_price
    if not (0.0 < c < 1.0):
        return 0.0
    b = (1.0 - c) / c
    return (p * b - (1.0 - p)) / b


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Calibrator:
    """Maps model confidence -> empirical win probability using quantile buckets
    fit on historical trades. Falls back to the global win rate for unseen data."""

    def __init__(self, edges: list[float], winrates: list[float], global_wr: float):
        self.edges = edges          # bucket upper edges (ascending)
        self.winrates = winrates    # empirical win rate per bucket
        self.global_wr = global_wr

    @classmethod
    def fit(cls, rows: list[dict[str, Any]], n_buckets: int = 5) -> "Calibrator":
        pts = [(float(r["confidence"]), 1.0 if r["pred"] == r["actual"] else 0.0) for r in rows]
        if not pts:
            return cls([], [], 0.0)
        pts.sort(key=lambda t: t[0])
        n = len(pts)
        global_wr = sum(w for _, w in pts) / n
        n_buckets = max(1, min(n_buckets, n))
        edges: list[float] = []
        winrates: list[float] = []
        for b in range(n_buckets):
            lo = (b * n) // n_buckets
            hi = ((b + 1) * n) // n_buckets
            chunk = pts[lo:hi]
            if not chunk:
                continue
            edges.append(chunk[-1][0])
            winrates.append(sum(w for _, w in chunk) / len(chunk))
        return cls(edges, winrates, global_wr)

    def predict(self, confidence: float) -> float:
        if not self.edges:
            return self.global_wr
        for edge, wr in zip(self.edges, self.winrates):
            if confidence <= edge:
                return wr
        return self.winrates[-1]


def stake_for(
    mode: str,
    *,
    bankroll: float,
    base_stake: float,
    confidence: float,
    entry_price: float,
    p_est: Optional[float] = None,
    kelly_cap: float = 0.25,
    kelly_mult: float = 0.5,
    max_stake_frac: float = 1.0,
    margin: float = 0.03,
    pct: float = 0.10,
) -> float:
    """Return the dollar stake for one trade under `mode`. Never exceeds the
    bankroll (or max_stake_frac of it).

    `percent` mode stakes `pct` of the CURRENT balance (passed as `bankroll`), so
    the stake grows automatically as the account grows and shrinks in a drawdown.

    `margin` (kelly only): require p_est >= entry_price + margin, not merely
    p_est > entry_price. A win rate that only *barely* clears breakeven is almost
    always estimation noise, and over-betting a mis-estimated edge is the classic
    way a winning system turns into a losing one — so we demand a cushion.
    """
    cap_usd = bankroll * max_stake_frac
    if bankroll <= 0:
        return 0.0
    if mode == "flat":
        return min(base_stake, cap_usd)
    if mode == "percent":
        # Fraction of the current balance -> auto-scales with the account.
        return min(bankroll * clamp(pct, 0.0, 1.0), cap_usd)
    if mode == "confidence":
        # NOTE: scaling by RAW confidence enlarges -EV bets — only safe once
        # confidence has been calibrated to a probability. Prefer kelly mode.
        return min(base_stake * clamp(confidence, 0.0, 1.0), cap_usd)
    if mode == "kelly":
        p = p_est if p_est is not None else confidence
        if p < entry_price + margin:
            return 0.0  # below breakeven + safety cushion -> don't bet
        f = kelly_fraction(p, entry_price)
        if f <= 0.0:
            return 0.0
        f = min(f * kelly_mult, kelly_cap)
        return min(bankroll * f, cap_usd)
    raise ValueError(f"unknown sizing mode {mode!r}")


def simulate_account(
    trades: list[dict[str, Any]],
    mode: str,
    *,
    bankroll: float = 100.0,
    base_stake: float = 10.0,
    entry_price: float = 0.85,
    kelly_cap: float = 0.25,
    kelly_mult: float = 0.5,
    max_stake_frac: float = 1.0,
    margin: float = 0.03,
    ruin_floor: float = 1.0,
) -> dict[str, Any]:
    """Walk a list of trades (each {confidence, win, p_est?}) through a sizing
    mode and report the account trajectory. Kelly compounds on the live balance;
    flat/confidence use a fixed base stake."""
    bal = bankroll
    peak = bankroll
    max_dd = 0.0
    n_staked = 0
    total_staked = 0.0
    ruined = False
    for tr in trades:
        p_est = tr.get("p_est")
        stake = stake_for(
            mode, bankroll=bal, base_stake=base_stake, confidence=float(tr["confidence"]),
            entry_price=entry_price, p_est=p_est, kelly_cap=kelly_cap, kelly_mult=kelly_mult,
            max_stake_frac=max_stake_frac, margin=margin,
        )
        if stake <= 0.0:
            continue
        n_staked += 1
        total_staked += stake
        if tr["win"]:
            bal += stake * (1.0 - entry_price) / entry_price
        else:
            bal -= stake
        peak = max(peak, bal)
        max_dd = min(max_dd, bal - peak)
        if bal < ruin_floor:
            ruined = True
            break
    return {
        "mode": mode,
        "final_balance": round(bal, 2),
        "return_pct": round((bal / bankroll - 1.0) * 100.0, 2),
        "pnl": round(bal - bankroll, 2),
        "n_staked": n_staked,
        "avg_stake": round(total_staked / n_staked, 2) if n_staked else 0.0,
        "max_drawdown": round(max_dd, 2),
        "ruined": ruined,
    }
