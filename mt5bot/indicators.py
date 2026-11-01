"""Causal indicators on numpy arrays. Value ``i`` only ever uses inputs ``0..i``.

Recursive indicators (EMA, Wilder ATR/RSI) run as plain Python loops over lists,
which is faster than indexing numpy scalars and still linear in the input size.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Optional

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .core import incremental as inc

_CACHE: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_CACHE_SIZE = 64


def _fingerprint(bars: np.ndarray) -> tuple:
    """Identity of a bars array: buffer, length, first/last time and close. Many arms evaluate
    the SAME closed-bar array on one bar, so shared indicators are computed once."""
    n = len(bars)
    if n == 0:
        return (0,)
    return (bars.__array_interface__["data"][0], n, int(bars["time"][0]), int(bars["time"][-1]),
            float(bars["close"][0]), float(bars["close"][-1]), float(bars["high"][-1]), float(bars["low"][-1]))


def cached(bars: np.ndarray, name: str, compute: Callable[[], np.ndarray]) -> np.ndarray:
    """Memoise an indicator of ``bars`` (results are read-only views; callers must not mutate)."""
    key = (_fingerprint(bars), name)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    value = np.asarray(compute())
    value.flags.writeable = False
    _CACHE[key] = value
    if len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return value


# ---------------------------------------------------------------- append-only series
#
# The live loop keeps each symbol and timeframe in a ``SeriesBuffer``: rows are only ever appended,
# so the indicators of a view of the buffer can extend their last result by one value, in O(1),
# instead of recomputing hundreds of bars. Any other input takes the batch path below, and research
# always does. tests/test_incremental.py proves both paths give the same numbers.


class _Extension:
    __slots__ = ("n", "out", "step")

    def __init__(self, capacity: int, step: Any) -> None:
        self.n = 0
        self.out = np.full(capacity, np.nan)
        self.step = step


class _Root:
    __slots__ = ("array", "extensions", "children")

    def __init__(self, array: np.ndarray) -> None:
        self.array = array
        self.extensions: dict[tuple[int, str], _Extension] = {}
        self.children: list[int] = []


_ROOTS: dict[int, _Root] = {}
_LOCK = threading.RLock()   # the team's agents read the same series from other threads


class SeriesBuffer:
    """Append-only bars with a stable memory location. ``view()`` is what strategies receive."""

    def __init__(self, rows: np.ndarray, capacity: int) -> None:
        self.array = np.zeros(max(capacity, len(rows) + 1), dtype=rows.dtype)
        self.array[:len(rows)] = rows
        self.n = len(rows)
        with _LOCK:
            _ROOTS[id(self.array)] = _Root(self.array)

    def __len__(self) -> int:
        return self.n

    @property
    def free(self) -> int:
        return len(self.array) - self.n

    def append(self, rows: np.ndarray) -> None:
        end = self.n + len(rows)
        self.array[self.n:end] = rows
        self.n = end

    def view(self) -> np.ndarray:
        return self.array[:self.n]

    def release(self) -> None:
        with _LOCK:
            _forget(id(self.array))


def _forget(root_id: int) -> None:
    root = _ROOTS.pop(root_id, None)
    if root is not None:
        for child in root.children:
            _forget(child)


def _root_of(x: np.ndarray) -> Optional[tuple[_Root, int]]:
    """The registered root of ``x`` and its field offset, only if ``x`` starts at row 0."""
    base = x.base
    if not isinstance(base, np.ndarray):
        return None
    root = _ROOTS.get(id(base))
    if root is None or x.ndim != 1 or x.strides[0] != base.strides[0]:
        return None
    offset = x.__array_interface__["data"][0] - base.__array_interface__["data"][0]
    return (root, offset) if 0 <= offset < base.strides[0] else None


def _extend(found: tuple[_Root, int], name: str, n: int, make: Callable[[], Any], feed: Callable[[Any, int], float]) -> np.ndarray:
    root, offset = found
    with _LOCK:
        key = (offset, name)
        ext = root.extensions.get(key)
        if ext is None or n < ext.n:
            ext = root.extensions[key] = _Extension(len(root.array), make())
            registered = _Root(ext.out)
            _ROOTS[id(ext.out)] = registered
            root.children.append(id(ext.out))
        out, step = ext.out, ext.step
        for i in range(ext.n, n):
            out[i] = feed(step, i)
        ext.n = max(ext.n, n)
        view = out[:n]
        view.flags.writeable = False
        return view


def _one_input(x: np.ndarray, name: str, make: Callable[[], Any]) -> Optional[np.ndarray]:
    found = _root_of(x)
    if found is None:
        return None
    return _extend(found, name, x.size, make, lambda step, i: step.update(float(x[i])))


def _bars_input(bars: np.ndarray, name: str, make: Callable[[], Any], fields: tuple[str, ...]) -> Optional[np.ndarray]:
    found = _root_of(bars)
    if found is None:
        return None
    columns = [bars[f] for f in fields]
    return _extend(found, name, len(bars), make, lambda step, i: step.update(*(float(c[i]) for c in columns)))


def bars_atr(bars: np.ndarray, period: int) -> np.ndarray:
    fast = _bars_input(bars, f"atr{period}", lambda: inc.WilderAtr(period), ("high", "low", "close"))
    if fast is not None:
        return fast
    return cached(bars, f"atr{period}", lambda: atr(bars["high"], bars["low"], bars["close"], period))


def bars_adx(bars: np.ndarray, period: int = 14) -> np.ndarray:
    fast = _bars_input(bars, f"adx{period}", lambda: inc.WilderAdx(period), ("high", "low", "close"))
    if fast is not None:
        return fast
    return cached(bars, f"adx{period}", lambda: adx(np.asarray(bars["high"], dtype=float),
                                                    np.asarray(bars["low"], dtype=float),
                                                    np.asarray(bars["close"], dtype=float), period))


def bars_rolling_median(bars: np.ndarray, field: str, window: int, batch: Callable[[], np.ndarray]) -> np.ndarray:
    fast = _bars_input(bars, f"median_{field}_{window}", lambda: inc.RollingMedian(window), (field,))
    return fast if fast is not None else batch()


def ema(values: Any, period: int) -> np.ndarray:
    """Exponential moving average seeded with the SMA of the first ``period`` values.

    Entries before index ``period - 1`` are NaN.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    x = np.asarray(values, dtype=float)
    fast = _one_input(x, f"ema{period}", lambda: inc.Ema(period))
    if fast is not None:
        return fast
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
    fast = _one_input(x, f"sma{period}", lambda: inc.Sma(period))
    if fast is not None:
        return fast
    out = np.full(x.shape, np.nan)
    if x.size < period:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


def rolling_std(values: Any, period: int) -> np.ndarray:
    """Population standard deviation over the last ``period`` values (inclusive)."""
    x = np.asarray(values, dtype=float)
    fast = _one_input(x, f"std{period}", lambda: inc.RollingStd(period))
    if fast is not None:
        return fast
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
    fast = _one_input(c, f"rsi{period}", lambda: inc.WilderRsi(period))
    if fast is not None:
        return fast
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
    fast = _one_input(x, f"maxprev{period}", lambda: inc.RollingMaxPrev(period))
    if fast is not None:
        return fast
    out = np.full(x.shape, np.nan)
    if x.size <= period:
        return out
    out[period:] = sliding_window_view(x[:-1], period).max(axis=1)
    return out


def rolling_min_prev(values: Any, period: int) -> np.ndarray:
    """Min of the ``period`` values BEFORE index i (excludes i). NaN until defined."""
    x = np.asarray(values, dtype=float)
    fast = _one_input(x, f"minprev{period}", lambda: inc.RollingMinPrev(period))
    if fast is not None:
        return fast
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
    fast = _one_input(x, f"pctrank{lookback}", lambda: inc.PercentileRank(lookback))
    if fast is not None:
        return fast
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



def efficiency_ratio(close: Any, period: int) -> np.ndarray:
    """Kaufman efficiency ratio: |net change| / sum of |changes| over ``period`` bars (0..1)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    c = np.asarray(close, dtype=float)
    fast = _one_input(c, f"er{period}", lambda: inc.EfficiencyRatio(period))
    if fast is not None:
        return fast
    out = np.full(c.shape, np.nan)
    if c.size <= period:
        return out
    change = np.abs(c[period:] - c[:-period])
    steps = np.abs(np.diff(c))
    csum = np.concatenate([[0.0], np.cumsum(steps)])
    path = csum[period:] - csum[:-period]
    with np.errstate(invalid="ignore", divide="ignore"):
        out[period:] = np.where(path > 0, change / path, 0.0)
    return out


def adx(high: Any, low: Any, close: Any, period: int = 14) -> np.ndarray:
    """Wilder's Average Directional Index (0..100). First defined value at index 2 * period - 1."""
    if period < 1:
        raise ValueError("period must be >= 1")
    h = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    n = h.size
    out = np.full(n, np.nan)
    if n < 2 * period:      # an index's value must not depend on how many bars come after it
        return out
    up = h[1:] - h[:-1]
    down = lo[:-1] - lo[1:]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(h, lo, c)[1:]

    def wilder(x: np.ndarray) -> np.ndarray:
        res = np.full(x.size, np.nan)
        prev = float(np.sum(x[:period]))
        res[period - 1] = prev
        for i in range(period, x.size):
            prev = prev - prev / period + float(x[i])
            res[i] = prev
        return res

    atr_s, plus_s, minus_s = wilder(tr), wilder(plus_dm), wilder(minus_dm)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = 100.0 * plus_s / atr_s
        minus_di = 100.0 * minus_s / atr_s
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    dx = np.where(np.isfinite(dx), dx, 0.0)
    adx_vals = np.full(dx.size, np.nan)
    first = 2 * period - 2
    if dx.size > first:
        prev = float(np.mean(dx[period - 1:first + 1]))
        adx_vals[first] = prev
        for i in range(first + 1, dx.size):
            prev = (prev * (period - 1) + dx[i]) / period
            adx_vals[i] = prev
    out[1:] = adx_vals
    return out
