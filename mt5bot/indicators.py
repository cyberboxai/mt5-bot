"""Causal indicators on numpy arrays. Value ``i`` only ever uses inputs ``0..i``.

Recursive indicators (EMA, Wilder ATR/RSI) run as plain Python loops over lists,
which is faster than indexing numpy scalars and still linear in the input size.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def ema(values: Any, period: int) -> np.ndarray:
    """Exponential moving average seeded with the SMA of the first ``period`` values.

    Entries before index ``period - 1`` are NaN.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    if x.size < period:
        return out
    alpha = 2.0 / (period + 1)
    keep = 1.0 - alpha
    data = x.tolist()
    prev = float(np.mean(x[:period]))
    res = [prev]
    for v in data[period:]:
        prev = alpha * v + keep * prev
        res.append(prev)
    out[period - 1:] = res
    return out


def sma(values: Any, period: int) -> np.ndarray:
    if period < 1:
        raise ValueError("period must be >= 1")
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    if x.size < period:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


def rolling_std(values: Any, period: int) -> np.ndarray:
    """Population standard deviation over the last ``period`` values (inclusive)."""
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    if period < 2 or x.size < period:
        return out
    windows = sliding_window_view(x, period)
    out[period - 1:] = windows.std(axis=1)
    return out


def true_range(high: Any, low: Any, close: Any) -> np.ndarray:
    """True range; index 0 has no previous close and is NaN."""
    h = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    if not h.shape == lo.shape == c.shape:
        raise ValueError("high, low and close must have the same length")
    tr = np.full(h.shape, np.nan)
    if h.size < 2:
        return tr
    prev_close = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - prev_close), np.abs(lo[1:] - prev_close)])
    return tr


def atr(high: Any, low: Any, close: Any, period: int) -> np.ndarray:
    """Average True Range with Wilder smoothing.

    True range needs the previous close, so the first defined ATR is at index
    ``period`` (mean of the first ``period`` true ranges). Earlier entries are NaN.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    tr = true_range(high, low, close)
    out = np.full(tr.shape, np.nan)
    if tr.size < period + 1:
        return out
    prev = float(np.mean(tr[1:period + 1]))
    res = [prev]
    for v in tr[period + 1:].tolist():
        prev = (prev * (period - 1) + v) / period
        res.append(prev)
    out[period:] = res
    return out


def rsi(close: Any, period: int) -> np.ndarray:
    """Wilder RSI. First defined value at index ``period``."""
    if period < 1:
        raise ValueError("period must be >= 1")
    c = np.asarray(close, dtype=float)
    out = np.full(c.shape, np.nan)
    if c.size < period + 1:
        return out
    diff = np.diff(c)
    gains = np.clip(diff, 0, None).tolist()
    losses = np.clip(-diff, 0, None).tolist()
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    res = []

    def value(g: float, lo: float) -> float:
        if lo == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / lo)

    res.append(value(avg_g, avg_l))
    for g, lo in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + lo) / period
        res.append(value(avg_g, avg_l))
    out[period:] = res
    return out


def rolling_max_prev(values: Any, period: int) -> np.ndarray:
    """Max of the ``period`` values BEFORE index i (excludes i). NaN until defined."""
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    if x.size <= period:
        return out
    out[period:] = sliding_window_view(x[:-1], period).max(axis=1)
    return out


def rolling_min_prev(values: Any, period: int) -> np.ndarray:
    """Min of the ``period`` values BEFORE index i (excludes i). NaN until defined."""
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    if x.size <= period:
        return out
    out[period:] = sliding_window_view(x[:-1], period).min(axis=1)
    return out


def rolling_percentile_rank(values: Any, lookback: int, chunk: int = 4096) -> np.ndarray:
    """Percentile (0..100) of value i within the window of the last ``lookback`` values (inclusive).

    NaN inputs propagate as NaN. Processed in chunks to bound memory.
    """
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    if lookback < 2 or x.size < lookback:
        return out
    windows = sliding_window_view(x, lookback)
    for start in range(0, windows.shape[0], chunk):
        w = windows[start:start + chunk]
        current = w[:, -1:]
        valid = ~np.isnan(w).any(axis=1)
        rank = (w <= current).sum(axis=1) / lookback * 100.0
        rank[~valid] = np.nan
        out[lookback - 1 + start: lookback - 1 + start + w.shape[0]] = rank
    return out

