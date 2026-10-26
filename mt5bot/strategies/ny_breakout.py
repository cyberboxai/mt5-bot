"""New York opening-range breakout: the session-breakout logic on the New York open.

Hours are in broker SERVER time (bars carry server time). With the usual GMT+2/+3 broker
clock the New York cash open (13:30 UTC) falls at 15:30 or 16:30 server, so the grid spans
14 to 17; research and evidence pick what works on this broker's clock."""

from __future__ import annotations

from .base import Param
from .registry import register
from .session_breakout import SessionBreakout


@register
class NewYorkBreakout(SessionBreakout):
    name = "ny_breakout"
    description = ("Range of the first minutes after the New York open (server time), then trade the first close "
                   "outside it within the window.")
    param_space = (
        Param.int_range("start_hour", 14, 17, 1),
        Param.int_range("range_minutes", 30, 90, 30),
        Param.int_range("window_hours", 2, 4, 1),
        Param.float_range("sl_mult", 1.0, 3.0, 0.5),
        Param.float_range("tp_mult", 1.0, 4.0, 0.5),
    )
