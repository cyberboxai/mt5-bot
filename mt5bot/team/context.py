"""Immutable snapshots handed to agents. Built by the bot from MT5 data; agents never see
the terminal. Account figures are RELATIVE (percentages) so nothing identifying or
absolute can leak from an agent into a prompt or the panel."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import numpy as np


def read_only(array: np.ndarray) -> np.ndarray:
    view = array.view()
    view.flags.writeable = False
    return view


@dataclass(frozen=True)
class PositionView:
    symbol: str
    side: str
    volume: float
    entry: float
    sl: float
    tp: float
    ticket: int
    risk_pct: float          # money at stake to SL as % of equity
    unrealized_r: float      # current P&L in R
    bars_open: int
    currency_base: str = ""
    currency_profit: str = ""
    param_set_id: Optional[int] = None


@dataclass(frozen=True)
class AccountView:
    is_real: bool
    risk_pct: float
    equity_change_today_pct: float
    daily_loss_used_pct_of_limit: float
    drawdown_pct: float
    drawdown_limit_pct: float
    open_risk_pct: float
    max_open_risk_pct: float
    halted: Optional[str]
    positions: tuple[PositionView, ...] = ()


@dataclass(frozen=True)
class MarketView:
    symbol: str
    timeframe: str
    bar_seconds: int
    bar_time: int
    closed: np.ndarray
    bid: float
    ask: float
    point: float
    digits: int
    spread_points: float
    tick_time: int
    utc_now: float
    server_offset: int
    currency_base: str = ""
    currency_profit: str = ""
    tick_age_seconds: float = 0.0

    @property
    def server_now(self) -> float:
        return self.utc_now + self.server_offset


@dataclass(frozen=True)
class Proposal:
    """The v2 champion's entry intent: the ONLY thing that can originate a trade."""

    side: str
    strategy: str
    param_set_id: Optional[int]
    description: str
    atr: float
    sl_mult: float
    tp_mult: float
    oos_expectancy_r: Optional[float] = None
    win_rate: Optional[float] = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionContext:
    decision_id: str
    market: MarketView
    account: AccountView
    proposal: Optional[Proposal]
    others: Mapping[str, np.ndarray] = field(default_factory=dict)  # closes of the other symbols
    views: Mapping[str, Any] = field(default_factory=dict)            # context-stage outputs by agent id
    track_records: Mapping[str, Any] = field(default_factory=dict)
    purpose: str = "entry"  # entry | management | observe

    def with_views(self, views: Mapping[str, Any]) -> "DecisionContext":
        merged = dict(self.views)
        merged.update(views)
        return dataclasses.replace(self, views=merged)

    def view(self, agent_id: str, default: Any = None) -> Any:
        return self.views.get(agent_id, default)
