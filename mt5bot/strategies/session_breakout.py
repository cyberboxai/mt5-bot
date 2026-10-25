"""Opening range breakout on server-time sessions (one trade signal per day at most)."""

from __future__ import annotations

import numpy as np

from .base import Param, Params, Strategy
from .registry import register

DAY = 86400


@register
class SessionBreakout(Strategy):
    name = "session_breakout"
    description = ("Build the high/low range of the first minutes after the session start (server time), "
                   "then trade the first close outside it before the session window ends.")
    param_space = (
        Param.int_range("start_hour", 7, 12, 1),
        Param.int_range("range_minutes", 30, 120, 30),
        Param.int_range("window_hours", 3, 7, 2),
        Param.float_range("sl_mult", 1.0, 3.0, 0.5),
        Param.float_range("tp_mult", 1.0, 4.0, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return 20

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        times = np.asarray(bars["time"], dtype=np.int64)
        n = len(times)
        side = np.zeros(n, dtype=np.int8)
        if n < 2:
            return side
        day = (times // DAY).tolist()
        sec = (times % DAY).tolist()
        high = np.asarray(bars["high"], dtype=float).tolist()
        low = np.asarray(bars["low"], dtype=float).tolist()
        close = np.asarray(bars["close"], dtype=float).tolist()
        range_start = int(params["start_hour"]) * 3600
        range_end = range_start + int(params["range_minutes"]) * 60
        window_end = range_end + int(params["window_hours"]) * 3600

        current_day = None
        r_high = r_low = None
        done = False
        for i in range(n):
            if day[i] != current_day:
                current_day, r_high, r_low, done = day[i], None, None, False
            s = sec[i]
            if range_start <= s < range_end:
                r_high = high[i] if r_high is None else max(r_high, high[i])
                r_low = low[i] if r_low is None else min(r_low, low[i])
                continue
            if done or r_high is None or not (range_end <= s < window_end):
                continue
            if close[i] > r_high:
                side[i], done = 1, True
            elif close[i] < r_low:
                side[i], done = -1, True
        return side
