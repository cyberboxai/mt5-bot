"""NoTrade: the first-class "stand aside" candidate. Zero expectancy by definition.

It is the champion whenever no other candidate passes every acceptance gate.
"""

from __future__ import annotations

import numpy as np

from .base import Params, Strategy

NO_TRADE = "notrade"


class NoTrade(Strategy):
    name = NO_TRADE
    description = "Never trades. Chosen when no candidate demonstrates an out-of-sample edge."
    param_space = ()

    def warmup_bars(self, params: Params) -> int:
        return 0

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        return np.zeros(len(bars), dtype=np.int8)


NOTRADE = NoTrade()
