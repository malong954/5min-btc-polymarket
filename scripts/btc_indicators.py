#!/usr/bin/env python3
"""
Pure-stdlib technical indicators (no numpy/pandas) so the backtester runs on
any Python 3 install.

Every function takes a list of floats (usually closes) and returns a list of the
same length, with `None` filling the warm-up region where the indicator is not
yet defined. This makes them safe to index positionally against the source bars.
"""

from __future__ import annotations

import math
from typing import Optional


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average, seeded with the SMA of the first `period`."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period or period <= 0:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder's RSI."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period

    def rsi_from(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        ch = values[i] - values[i - 1]
        gain = max(ch, 0.0)
        loss = max(-ch, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = rsi_from(avg_gain, avg_loss)
    return out


def macd(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """Return (macd_line, signal_line, histogram)."""
    n = len(values)
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: list[Optional[float]] = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    # Signal EMA is computed over the defined region of macd_line.
    defined = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[Optional[float]] = [None] * n
    hist: list[Optional[float]] = [None] * n
    if len(defined) >= signal:
        seq = [v for _, v in defined]
        sig_seq = ema(seq, signal)
        for (idx, _), sv in zip(defined, sig_seq):
            if sv is not None:
                signal_line[idx] = sv
                hist[idx] = macd_line[idx] - sv
    return macd_line, signal_line, hist


def rolling_mean(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if period <= 0:
        return out
    run = 0.0
    for i in range(n):
        run += values[i]
        if i >= period:
            run -= values[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    var = sum((x - m) ** 2 for x in values) / (n - 1)
    return math.sqrt(var)


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation of two equal-length series; None if undefined."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
