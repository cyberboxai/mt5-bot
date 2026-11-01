"""Session dynamics: breakout of the quiet Asian range when London opens."""

from __future__ import annotations

import numpy as np

from .base import Param, Params, Strategy, sides_from
from .registry import register

DAY = 86400


@register
class AsianRangeBreakout(Strategy):
    name = "asian_range"
    description = "Trade the first close outside the Asian session range during the London morning."
    param_space = (
        Param.int_range("range_start_hour", 0, 2, 1),
        Param.int_range("range_end_hour", 6, 9, 1),
        Param.int_range("trade_hours", 2, 5, 1),
        Param.float_range("sl_mult", 1.0, 2.5, 0.5),
        Param.float_range("tp_mult", 1.5, 4.0, 0.5),
    )
    default_sl_mult = 1.5
    default_tp_mult = 2.5

    def warmup_bars(self, params: Params) -> int:
        return 120

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        times = np.asarray(bars["time"], dtype=np.int64)
        highs, lows = np.asarray(bars["high"], dtype=float), np.asarray(bars["low"], dtype=float)
        close = np.asarray(bars["close"], dtype=float)
        days = times // DAY
        seconds = times % DAY
        start, end = int(params["range_start_hour"]) * 3600, int(params["range_end_hour"]) * 3600
        stop = end + int(params["trade_hours"]) * 3600
        buy = np.zeros(close.shape, dtype=bool)
        sell = np.zeros(close.shape, dtype=bool)
        for day in np.unique(days):
            today = np.flatnonzero(days == day)
            section = seconds[today]
            ranged = today[(section >= start) & (section < end)]
            active = today[(section >= end) & (section < stop)]
            if ranged.size < 3 or active.size == 0:
                continue
            top, bottom = float(highs[ranged].max()), float(lows[ranged].min())
            for i in active:
                if close[i] > top:
                    buy[i] = True
                    break
                if close[i] < bottom:
                    sell[i] = True
                    break
        return sides_from(buy, sell)
