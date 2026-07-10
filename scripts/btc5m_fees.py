#!/usr/bin/env python3
"""Fee model for BTC 5m Up/Down reporting (Phase 1, ODDS_IMPROVEMENT_PLAN.md).

Reporting-only: nothing in the live trading path imports this module.

Polymarket charges a taker fee on short-duration crypto markets approximated by

    fee_per_share(p) = COEF * (p * (1 - p)) ** 2

with COEF believed to be 0.125. The coefficient is UNCALIBRATED until verified
against real fills (compare on-chain settlement vs reported order amounts), so
every number derived here is labeled an estimate. Override via --fee-coef or
the BTC5M_FEE_COEF environment variable.

It is also unverified whether the runner's cashflow numbers (makingAmount /
takingAmount) are already net of fees; if they are, est_net_pnl double-counts
the fee. Calibration against live fills resolves this. Until then reports show
gross cashflow PnL and the fee estimate separately.
"""
import os
from typing import Any, Optional

DEFAULT_FEE_COEF = 0.125
FEE_MODEL_STATUS = "estimated_not_calibrated"


def fee_coef_from_env(default: float = DEFAULT_FEE_COEF) -> float:
    try:
        return float(os.environ.get("BTC5M_FEE_COEF", default))
    except (TypeError, ValueError):
        return default


def taker_fee_per_share(price: float, coef: float = DEFAULT_FEE_COEF) -> float:
    """Estimated taker fee in USDC per share at a given fill price."""
    if not isinstance(price, (int, float)) or not 0.0 < price < 1.0:
        return 0.0
    return coef * (price * (1.0 - price)) ** 2


def exit_price_from_closed(closed: dict[str, Any]) -> Optional[float]:
    """Effective exit price = USDC received / shares sold, when both known."""
    try:
        usdc = float(closed.get("close_usdc") or 0)
        shares = float(closed.get("close_shares") or 0)
    except (TypeError, ValueError):
        return None
    if usdc > 0 and shares > 0:
        return usdc / shares
    return None


def estimate_run_fees(report: dict[str, Any], coef: float = DEFAULT_FEE_COEF) -> dict[str, Any]:
    """Per-run fee estimate from a runner tail-JSON report.

    Both legs are modeled as taker: entries fill as matched market-style
    orders, and every close path (FAK, GTC at best-bid minus 1 tick, forced
    GTC) prices through the book. Missing data yields zeros/None rather than
    errors so old or partial logs keep parsing.
    """
    opened = report.get("opened") or {}
    closed = report.get("closed") or {}

    entry_price = opened.get("entry_price")
    shares = opened.get("shares")
    exit_price = exit_price_from_closed(closed)

    fee_entry = 0.0
    if isinstance(entry_price, (int, float)) and isinstance(shares, (int, float)) and shares > 0:
        fee_entry = shares * taker_fee_per_share(float(entry_price), coef)

    fee_exit = 0.0
    try:
        exit_shares = float(closed.get("close_shares") or 0)
    except (TypeError, ValueError):
        exit_shares = 0.0
    if exit_price is not None and exit_shares > 0:
        fee_exit = exit_shares * taker_fee_per_share(exit_price, coef)

    gross = report.get("realized_cashflow_pnl_usdc")
    net = None
    if isinstance(gross, (int, float)):
        net = round(float(gross) - fee_entry - fee_exit, 6)

    return {
        "entry_price": entry_price,
        "exit_price": round(exit_price, 6) if exit_price is not None else None,
        "est_fee_entry_usdc": round(fee_entry, 6),
        "est_fee_exit_usdc": round(fee_exit, 6),
        "est_fees_total_usdc": round(fee_entry + fee_exit, 6),
        "gross_cashflow_pnl_usdc": gross,
        "est_net_pnl_usdc": net,
    }


def fee_model_meta(coef: float) -> dict[str, Any]:
    return {
        "status": FEE_MODEL_STATUS,
        "formula": "fee_per_share = coef * (p*(1-p))**2, both legs modeled as taker",
        "coef": coef,
        "note": "Uncalibrated estimate; verify coefficient and whether cashflow amounts are already net of fees against >=20 real fills before trusting est_net_pnl.",
    }
