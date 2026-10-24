"""EMA fast/slow crossover trend follower (the v1 strategy, now with bounded parameters)."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, sides_from
from .registry import register


@register
class EmaCross(Strategy):
    name = "ema_cross"
    description = "Buy when the fast EMA crosses above the slow EMA, sell on the opposite cross."
    param_space = (
        Param.int_range("fast", 5, 25, 5),
        Param.int_range("slow", 40, 120, 20),
        Param.float_range("sl_mult", 1.5, 3.0, 0.5),
        Param.float_range("tp_mult", 1.5, 4.5, 0.5),
    )

    def is_valid(self, params: Params) -> bool:
        return params["fast"] < params["slow"]

    def warmup_bars(self, params: Params) -> int:
        # EMA is SMA-seeded; after ~3 periods the seed no longer matters.
        return 3 * int(params["slow"]) + 2

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        diff = indicators.ema(close, int(params["fast"])) - indicators.ema(close, int(params["slow"]))
        # Tolerance absorbs float noise so a flat market never produces a "cross".
        tol = 1e-9 * np.maximum(np.abs(close), 1.0)
        buy = np.zeros(len(close), dtype=bool)
        sell = np.zeros(len(close), dtype=bool)
        with np.errstate(invalid="ignore"):
            buy[1:] = (diff[:-1] <= tol[1:]) & (diff[1:] > tol[1:])
            sell[1:] = (diff[:-1] >= -tol[1:]) & (diff[1:] < -tol[1:])
        return sides_from(buy, sell)
