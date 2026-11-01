"""Structure-based stops: the stop sits beyond the recent swing instead of a fixed ATR multiple.

``StructureStop`` wraps a strategy without changing when it trades. It only changes the volatility
figure that sizes the trade, so SL and TP keep the arithmetic every other part of the system uses
(``atr * sl_mult``). The distance is kept between 1 and 3 ATR, so a stop can never be absurdly tight
(spread dominates) or wide (the minimum lot becomes unaffordable). Everything is computed from closed
bars and never looks ahead.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .. import indicators
from .base import ATR_PERIOD, Params, SignalArrays, Strategy
from .registry import register_instance, registered

SWING_BARS = 10
MIN_ATR = 1.0
MAX_ATR = 3.0
PREFIX = "structstop_"


def swing_distance(high: np.ndarray, low: np.ndarray, close: np.ndarray, atr: np.ndarray) -> np.ndarray:
    """Distance from each close to the farther of the last ``SWING_BARS`` swing extremes, in price,
    clipped to [MIN_ATR, MAX_ATR] ATR. NaN while there are too few bars."""
    n = close.size
    out = np.full(n, np.nan)
    if n < SWING_BARS:
        return out
    windows_h = np.lib.stride_tricks.sliding_window_view(high, SWING_BARS).max(axis=1)
    windows_l = np.lib.stride_tricks.sliding_window_view(low, SWING_BARS).min(axis=1)
    c = close[SWING_BARS - 1:]
    raw = np.maximum(windows_h - c, c - windows_l)
    a = atr[SWING_BARS - 1:]
    with np.errstate(invalid="ignore"):
        out[SWING_BARS - 1:] = np.clip(raw, MIN_ATR * a, MAX_ATR * a)
    return out


def structure_distance(bars: Optional[np.ndarray], atr: Optional[float] = None) -> Optional[float]:
    """Current structure stop distance in price, for the feasibility map."""
    if bars is None or len(bars) < SWING_BARS + ATR_PERIOD:
        return None
    series = indicators.atr(bars["high"], bars["low"], bars["close"], ATR_PERIOD)
    value = swing_distance(np.asarray(bars["high"], dtype=float), np.asarray(bars["low"], dtype=float),
                           np.asarray(bars["close"], dtype=float), np.asarray(series, dtype=float))[-1]
    return float(value) if np.isfinite(value) and value > 0 else None


class StructureStop(Strategy):
    def __init__(self, inner: Strategy) -> None:
        self.inner = inner
        self.name = f"{PREFIX}{inner.name}"
        self.description = f"{inner.name} with the stop beyond the swing of the last {SWING_BARS} bars."
        self.param_space = inner.param_space
        self.default_sl_mult = inner.default_sl_mult
        self.default_tp_mult = inner.default_tp_mult

    def is_valid(self, params: Params) -> bool:
        return self.inner.is_valid(params)

    def warmup_bars(self, params: Params) -> int:
        return max(self.inner.warmup_bars(params), SWING_BARS + ATR_PERIOD)

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        return self.inner.compute(bars, params)

    def signals(self, bars: np.ndarray, params: Params) -> SignalArrays:
        base = self.inner.signals(bars, params)
        distance = swing_distance(np.asarray(bars["high"], dtype=float), np.asarray(bars["low"], dtype=float),
                                  np.asarray(bars["close"], dtype=float), np.asarray(base.atr, dtype=float))
        side = np.where(np.isfinite(distance), base.side, 0).astype(np.int8)
        return SignalArrays(side=side, atr=distance / base.sl_mult, sl_mult=base.sl_mult, tp_mult=base.tp_mult)


def register_structure(*inner_names: str) -> None:
    library = registered()
    for name in inner_names:
        register_instance(StructureStop(library[name]))
