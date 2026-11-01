"""Time-series momentum: go with the sign of the return over a long lookback."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import ATR_PERIOD, Param, Params, Strategy, sides_from
from .registry import register


@register
class TimeSeriesMomentum(Strategy):
    name = "ts_momentum"
    description = "Trend following: enter when the N-bar return, scaled by volatility, crosses a threshold."
    param_space = (
        Param.int_range("lookback", 20, 120, 20),
        Param.float_range("threshold", 1.0, 3.0, 0.5),
        Param.float_range("sl_mult", 2.0, 4.0, 0.5),
        Param.float_range("tp_mult", 3.0, 8.0, 1.0),
    )
    default_sl_mult = 3.0
    default_tp_mult = 5.0

    def warmup_bars(self, params: Params) -> int:
        return int(params["lookback"]) + ATR_PERIOD + 2

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        n = int(params["lookback"])
        close = np.asarray(bars["close"], dtype=float)
        vol = indicators.bars_atr(bars, ATR_PERIOD)
        move = np.full(close.shape, np.nan)
        move[n:] = close[n:] - close[:-n]
        with np.errstate(divide="ignore", invalid="ignore"):
            score = move / (vol * np.sqrt(n))
        level = float(params["threshold"])
        above, below = score > level, score < -level
        # Only the first bar past the threshold counts: a trend is entered once, not on every bar.
        buy = np.zeros(close.shape, dtype=bool)
        sell = np.zeros(close.shape, dtype=bool)
        buy[1:] = above[1:] & ~above[:-1]
        sell[1:] = below[1:] & ~below[:-1]
        return sides_from(buy, sell)
