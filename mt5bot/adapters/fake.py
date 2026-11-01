"""``FakeBroker``: a static market for tests. Same matching engine and chaos as the simulator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional, Sequence

import numpy as np

from ..core import broker as b
from .exchange import SimCosts, SimExchange


class FakeBroker(SimExchange):
    def __init__(self, bid: float = 1.1, spread_points: int = 10, costs: SimCosts = SimCosts(slippage_points=0.0),
                 balance: float = 10_000.0, hedging: bool = True, symbols: Sequence[str] = ("EURUSD",)) -> None:
        super().__init__(costs, balance, 1, hedging=hedging)
        self.bid = {s: bid for s in symbols}
        self.spread = spread_points
        self.now = 1_760_000_400
        self.rates_by_symbol: dict[str, Any] = {}

    def set_price(self, symbol: str, bid: float) -> None:
        self.bid[symbol] = bid

    def quote(self, symbol: str) -> Any:
        return self.tick(symbol)

    def server_time(self) -> int:
        return self.now

    def tick(self, symbol: str) -> Any:
        if symbol not in self.bid:
            return None
        bid = self.bid[symbol]
        return SimpleNamespace(time=self.now, bid=bid, ask=round(bid + self.spread * 0.00001, 5))

    def symbol_info(self, symbol: str) -> Any:
        if symbol not in self.bid:
            return None
        return SimpleNamespace(name=symbol, point=0.00001, digits=5, trade_mode=b.SYMBOL_TRADE_MODE_FULL,
                               trade_stops_level=0, trade_freeze_level=0, volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, filling_mode=2, trade_contract_size=100_000.0, trade_tick_value=1.0,
                               trade_tick_size=0.00001, swap_long=0.0, swap_short=0.0, swap_mode=1,
                               swap_rollover3days=3, currency_base=symbol[:3], currency_profit=symbol[3:6])

    def rates(self, symbol: str, timeframe: str, start: int, count: int) -> Any:
        rates = self.rates_by_symbol.get(symbol)
        if rates is None:
            return None
        end = len(rates) - start
        return np.asarray(rates[max(0, end - count):end])

    def select_symbol(self, symbol: str) -> bool:
        return symbol in self.bid

    def symbols(self, group: Optional[str] = None) -> Sequence[Any]:
        return tuple(self.symbol_info(s) for s in self.bid)
