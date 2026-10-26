"""Bollinger Bands: mean reversion back inside the bands, or squeeze breakout."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register

SQUEEZE_LOOKBACK = 120
SQUEEZE_PERCENTILE = 20.0


@register
class BollingerBands(Strategy):
    name = "bollinger"
    description = ("mode=reversion: buy when the close re-enters above the lower band, sell when it re-enters "
                   "below the upper band. mode=squeeze: after a low band-width percentile, trade the breakout.")
    param_space = (
        Param.int_range("period", 20, 40, 10),
        Param.float_range("width", 1.5, 2.5, 0.5),
        Param.choice("mode", "reversion", "squeeze"),
        Param.float_range("sl_mult", 1.0, 3.0, 0.5),
        Param.float_range("tp_mult", 1.0, 4.0, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return int(params["period"]) + SQUEEZE_LOOKBACK + 2

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        period = int(params["period"])
        mid = indicators.sma(close, period)
        dev = indicators.rolling_std(close, period) * float(params["width"])
        upper, lower = mid + dev, mid - dev
        with np.errstate(invalid="ignore", divide="ignore"):
            if params["mode"] == "reversion":
                return sides_from(crosses_above(close, lower), crosses_below(close, upper))
            bandwidth = np.where(mid > 0, (upper - lower) / mid, np.nan)
            # The band-width percentile does not depend on the width multiple: cache by period only.
            pct = indicators.cached(bars, f"bbw_pct{period}_{SQUEEZE_LOOKBACK}",
                                    lambda: indicators.rolling_percentile_rank(bandwidth, SQUEEZE_LOOKBACK))
            squeezed_before = np.zeros(len(close), dtype=bool)
            squeezed_before[1:] = pct[:-1] <= SQUEEZE_PERCENTILE
            buy = crosses_above(close, upper) & squeezed_before
            sell = crosses_below(close, lower) & squeezed_before
        return sides_from(buy, sell)
