"""``ShadowBroker``: the whole kernel on LIVE quotes with SIMULATED fills.

Nothing reaches the broker: this is the safe way to run the real decision path on a live feed
(``--shadow``). Stops are filled from the same matching engine as the simulator, on the ticks it
sees. Its fills are what the reality gap monitor compares real fills against.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from ..core import broker as b
from ..core.broker import BrokerReply, OrderRequest
from ..ports import MarketDataPort
from .exchange import SimCosts, SimExchange


class ShadowBroker(SimExchange):
    def __init__(self, live: MarketDataPort, live_broker: Any, costs: SimCosts = SimCosts(), seed: int = 1) -> None:
        acc = live_broker.account()
        super().__init__(costs, float(acc.equity) if acc is not None else 10_000.0, seed,
                         int(getattr(acc, "leverage", 100) or 100), int(getattr(acc, "login", 0) or 0),
                         f"{getattr(acc, 'server', 'shadow')}-shadow")
        self.live = live
        self.live_broker = live_broker
        self.account_state.trade_mode = getattr(acc, "trade_mode", b.ACCOUNT_TRADE_MODE_DEMO)
        self.account_state.currency = getattr(acc, "currency", "USD")

    def quote(self, symbol: str) -> Any:
        return self.live.tick(symbol)

    def server_time(self) -> int:
        for p in self.positions_book:
            tick = self.live.tick(p.symbol)
            if tick is not None:
                return int(tick.time)
        return 0

    def _spec(self, symbol: str) -> Any:
        return self.live.symbol_info(symbol)

    def contract(self, symbol: str) -> float:
        info = self._spec(symbol)
        return float(getattr(info, "trade_contract_size", 100_000.0) or 100_000.0)

    def point(self, symbol: str) -> float:
        info = self._spec(symbol)
        return float(getattr(info, "point", 0.00001) or 0.00001)

    def digits(self, symbol: str) -> int:
        info = self._spec(symbol)
        return int(getattr(info, "digits", 5) or 5)

    def on_tick(self, symbol: str) -> None:
        """Fill the stops the current quote has crossed (a tick is a zero-range bar)."""
        tick = self.live.tick(symbol)
        if tick is None:
            return
        self.fill_stops(symbol, float(tick.bid), float(tick.bid), float(tick.bid), float(tick.ask) - float(tick.bid))
        self.enforce_stop_out()

    # MarketData passes straight through; account questions go to the simulated book.

    def terminal(self) -> Any:
        return self.live_broker.terminal()

    def last_error(self) -> Any:
        return self.live_broker.last_error()

    def connect(self, **kwargs: Any) -> bool:
        return bool(self.live_broker.connect(**kwargs))

    def disconnect(self) -> None:
        self.live_broker.disconnect()

    def account(self) -> Any:
        self.mark_to_market()
        return self.account_state

    def check(self, request: OrderRequest) -> Optional[BrokerReply]:
        return self.live_broker.check(request)

    def symbol_info(self, symbol: str) -> Any:
        return self.live.symbol_info(symbol)

    def tick(self, symbol: str) -> Any:
        return self.live.tick(symbol)

    def rates(self, symbol: str, timeframe: str, start: int, count: int) -> Any:
        return self.live.rates(symbol, timeframe, start, count)

    def select_symbol(self, symbol: str) -> bool:
        return self.live.select_symbol(symbol)

    def symbols(self, group: Optional[str] = None) -> Sequence[Any]:
        return self.live.symbols(group)
