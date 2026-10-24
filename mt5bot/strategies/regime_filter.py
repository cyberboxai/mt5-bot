"""Volatility regime filter wrapper: only pass the inner strategy's signals when the ATR
percentile is inside a band. Registered for a few base strategies to keep the number of
candidates (and therefore the multiple-testing penalty) small.
"""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import ATR_PERIOD, Param, Params, Strategy
from .registry import register_instance, registered

LOOKBACK = 480  # five days of M15 bars
PREFIX = "vf_"


class VolatilityFilter(Strategy):
    def __init__(self, inner: Strategy) -> None:
        self.inner = inner
        self.name = f"volfilter_{inner.name}"
        self.description = f"{inner.name} traded only when ATR({ATR_PERIOD}) percentile over {LOOKBACK} bars is in [vf_low, vf_high]."
        self.param_space = inner.param_space + (
            Param.int_range(PREFIX + "low", 0, 40, 20),
            Param.int_range(PREFIX + "high", 60, 100, 20),
        )
        self.default_sl_mult = inner.default_sl_mult
        self.default_tp_mult = inner.default_tp_mult

    def _inner_params(self, params: Params) -> dict:
        return {k: v for k, v in params.items() if not k.startswith(PREFIX)}

    def is_valid(self, params: Params) -> bool:
        return params[PREFIX + "low"] < params[PREFIX + "high"] and self.inner.is_valid(self._inner_params(params))

    def warmup_bars(self, params: Params) -> int:
        return max(self.inner.warmup_bars(self._inner_params(params)), LOOKBACK + ATR_PERIOD + 1)

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        side = np.asarray(self.inner.compute(bars, self._inner_params(params)), dtype=np.int8).copy()
        vol = indicators.atr(bars["high"], bars["low"], bars["close"], ATR_PERIOD)
        pct = indicators.rolling_percentile_rank(vol, LOOKBACK)
        with np.errstate(invalid="ignore"):
            allowed = (pct >= params[PREFIX + "low"]) & (pct <= params[PREFIX + "high"])
        side[~allowed] = 0
        return side


def register_filtered(*inner_names: str) -> None:
    library = registered()
    for name in inner_names:
        register_instance(VolatilityFilter(library[name]))
