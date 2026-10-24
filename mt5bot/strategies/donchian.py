"""Donchian channel breakout with ATR stops."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register


@register
class DonchianBreakout(Strategy):
    name = "donchian"
    description = "Buy when the close breaks above the highest high of the previous N bars; sell below the lowest low."
    param_space = (
        Param.int_range("channel", 20, 100, 10),
        Param.float_range("sl_mult", 1.5, 3.0, 0.5),
        Param.float_range("tp_mult", 2.0, 5.0, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return int(params["channel"]) + 20

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        n = int(params["channel"])
        close = np.asarray(bars["close"], dtype=float)
        upper = indicators.rolling_max_prev(bars["high"], n)
        lower = indicators.rolling_min_prev(bars["low"], n)
        # Only the first close beyond the channel counts, so a stale breakout never re-enters.
        return sides_from(crosses_above(close, upper), crosses_below(close, lower))
