"""RSI mean reversion in the direction of a long EMA trend filter."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register


@register
class RsiReversion(Strategy):
    name = "rsi_reversion"
    description = ("Buy when RSI climbs back above the oversold level while price is above the trend EMA; "
                   "sell on the mirror condition.")
    param_space = (
        Param.int_range("rsi_period", 7, 21, 7),
        Param.int_range("oversold", 20, 35, 5),
        Param.int_range("trend_ema", 100, 200, 50),
        Param.float_range("sl_mult", 1.0, 2.5, 0.5),
        Param.float_range("tp_mult", 1.0, 3.0, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return 3 * int(params["trend_ema"]) + 2

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        value = indicators.rsi(close, int(params["rsi_period"]))
        trend = indicators.ema(close, int(params["trend_ema"]))
        low = float(params["oversold"])
        high = 100.0 - low
        with np.errstate(invalid="ignore"):
            buy = crosses_above(value, low) & (close > trend)
            sell = crosses_below(value, high) & (close < trend)
        return sides_from(buy, sell)
