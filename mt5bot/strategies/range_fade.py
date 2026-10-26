"""Range fade: when the market is not trending (low ADX), fade rejections of the channel
edges: a bar that pierces the prior N-bar low but closes back above it is a buy (mirror for
the high)."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, sides_from
from .registry import register


@register
class RangeFade(Strategy):
    name = "range_fade"
    description = ("With ADX below adx_max, buy a bar that trades below the prior channel low but closes back above "
                   "it; sell the mirror at the channel high.")
    param_space = (
        Param.int_range("channel", 20, 50, 10),
        Param.int_range("adx_max", 18, 26, 4),
        Param.float_range("sl_mult", 1.0, 2.0, 0.5),
        Param.float_range("tp_mult", 1.0, 2.5, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return max(int(params["channel"]), 28) + 30

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        high = np.asarray(bars["high"], dtype=float)
        low = np.asarray(bars["low"], dtype=float)
        close = np.asarray(bars["close"], dtype=float)
        n = int(params["channel"])
        upper = indicators.rolling_max_prev(high, n)
        lower = indicators.rolling_min_prev(low, n)
        trendless = indicators.cached(bars, "adx14", lambda: indicators.adx(high, low, close, 14)) < float(params["adx_max"])
        with np.errstate(invalid="ignore"):
            buy = (low < lower) & (close > lower) & trendless
            sell = (high > upper) & (close < upper) & trendless
        return sides_from(buy, sell)
