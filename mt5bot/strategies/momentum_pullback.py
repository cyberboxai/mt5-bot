"""Momentum pullback: in an established trend, enter when price resumes after a dip to the
fast EMA. Fires far more often than a moving-average crossover (every pullback, not only
trend changes), which suits M5/M15 arms."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register


@register
class MomentumPullback(Strategy):
    name = "momentum_pullback"
    description = ("Trend from the slow EMA and its slope; buy when the close crosses back above the fast EMA in an "
                   "uptrend (sell on the mirror), i.e. after a pullback, not at the trend change.")
    param_space = (
        Param.int_range("fast", 10, 30, 10),
        Param.int_range("slow", 50, 150, 50),
        Param.int_range("slope_bars", 5, 15, 5),
        Param.float_range("sl_mult", 1.0, 2.5, 0.5),
        Param.float_range("tp_mult", 1.5, 3.5, 0.5),
    )

    def is_valid(self, params: Params) -> bool:
        return params["fast"] < params["slow"]

    def warmup_bars(self, params: Params) -> int:
        return 3 * int(params["slow"]) + int(params["slope_bars"]) + 2

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        fast = indicators.ema(close, int(params["fast"]))
        slow = indicators.ema(close, int(params["slow"]))
        k = int(params["slope_bars"])
        rising = np.zeros(len(close), dtype=bool)
        falling = np.zeros(len(close), dtype=bool)
        with np.errstate(invalid="ignore"):
            rising[k:] = slow[k:] > slow[:-k]
            falling[k:] = slow[k:] < slow[:-k]
            up = rising & (close > slow) & (fast > slow)
            down = falling & (close < slow) & (fast < slow)
            return sides_from(crosses_above(close, fast) & up, crosses_below(close, fast) & down)
