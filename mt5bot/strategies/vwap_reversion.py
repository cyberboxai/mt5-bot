"""Session VWAP mean reversion: fade stretches away from the session's volume-weighted
average price (tick volume as the weight, reset at each server day), entering when price
turns back inside the band. Causal: the running VWAP at bar i uses bars of the same server
day up to and including i."""

from __future__ import annotations

import numpy as np

from .. import indicators
from .base import ATR_PERIOD, Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register

DAY = 86400


def session_vwap(bars: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(running VWAP, bars elapsed in the server day) for every bar."""
    times = np.asarray(bars["time"], dtype=np.int64)
    typical = (np.asarray(bars["high"], dtype=float) + np.asarray(bars["low"], dtype=float)
               + np.asarray(bars["close"], dtype=float)) / 3.0
    volume = np.maximum(np.asarray(bars["tick_volume"], dtype=float), 1.0)
    day = times // DAY
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    pv = typical * volume
    cum_pv = np.cumsum(pv)
    cum_v = np.cumsum(volume)
    seg = np.repeat(starts, np.diff(np.r_[starts, len(times)]))
    base_pv = np.where(seg > 0, cum_pv[seg - 1], 0.0)
    base_v = np.where(seg > 0, cum_v[seg - 1], 0.0)
    vwap = (cum_pv - base_pv) / (cum_v - base_v)
    elapsed = np.arange(len(times)) - seg + 1
    return vwap, elapsed


@register
class VwapReversion(Strategy):
    name = "vwap_reversion"
    description = ("Fade a stretch of more than band ATRs from the session VWAP: buy when the close crosses back above "
                   "VWAP - band x ATR, sell when it crosses back below VWAP + band x ATR.")
    param_space = (
        Param.float_range("band", 1.0, 2.5, 0.5),
        Param.int_range("min_bars", 6, 18, 6),
        Param.float_range("sl_mult", 1.0, 2.5, 0.5),
        Param.float_range("tp_mult", 1.0, 2.5, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return ATR_PERIOD * 3

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        vwap, elapsed = session_vwap(bars)
        atr = indicators.bars_atr(bars, ATR_PERIOD)
        band = float(params["band"]) * atr
        mature = elapsed >= int(params["min_bars"])
        with np.errstate(invalid="ignore"):
            buy = crosses_above(close, vwap - band) & mature
            sell = crosses_below(close, vwap + band) & mature
        return sides_from(buy, sell)
