"""Volatility-contraction breakout: after a quiet spell (the range of the last few bars is
small compared with what the long ATR predicts), trade the first close beyond that range."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register

LONG_ATR = 50


def compression(bars: np.ndarray, k: int) -> np.ndarray:
    """Range of the k bars BEFORE each bar over ATR(50) x sqrt(k) (about 1.0 for a random walk)."""
    high = np.asarray(bars["high"], dtype=float)
    low = np.asarray(bars["low"], dtype=float)
    atr = indicators.bars_atr(bars, LONG_ATR)
    prev_atr = np.full(len(atr), np.nan)
    prev_atr[1:] = atr[:-1]
    span = indicators.rolling_max_prev(high, k) - indicators.rolling_min_prev(low, k)
    with np.errstate(invalid="ignore", divide="ignore"):
        return span / (prev_atr * np.sqrt(k))


@register
class VolContraction(Strategy):
    name = "vol_contraction"
    description = ("When the range of the last contraction_bars bars is below ratio_max x ATR(50) x sqrt(bars), buy "
                   "the first close above that range (sell below it).")
    param_space = (
        Param.int_range("contraction_bars", 6, 14, 4),
        Param.float_range("ratio_max", 0.6, 0.8, 0.1),
        Param.float_range("sl_mult", 1.0, 2.5, 0.5),
        Param.float_range("tp_mult", 1.5, 4.0, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return LONG_ATR * 3 + int(params["contraction_bars"])

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        high = np.asarray(bars["high"], dtype=float)
        low = np.asarray(bars["low"], dtype=float)
        close = np.asarray(bars["close"], dtype=float)
        k = int(params["contraction_bars"])
        upper = indicators.rolling_max_prev(high, k)
        lower = indicators.rolling_min_prev(low, k)
        with np.errstate(invalid="ignore"):
            quiet = compression(bars, k) < float(params["ratio_max"])
            buy = crosses_above(close, upper) & quiet
            sell = crosses_below(close, lower) & quiet
        return sides_from(buy, sell)
