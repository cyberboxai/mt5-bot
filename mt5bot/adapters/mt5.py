"""``Mt5Adapter``: the only module that touches the MetaTrader5 API.

It implements ``BrokerPort`` and ``MarketDataPort`` on any object with the MetaTrader5
module's interface, and translates between that API and the domain's value objects. An
exception or a ``None`` from ``order_send`` becomes a reply with no retcode: the domain treats
that as UNCERTAIN, never as "not executed".
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from ..core.broker import (CHECKED_CONSTANTS, ORDER_TIME_GTC, TRADE_ACTION_DEAL, TRADE_ACTION_REMOVE, TRADE_ACTION_SLTP,
                           BrokerReply, OrderRequest, order_type_of)

log = logging.getLogger("mt5_bot")


def load_terminal_api() -> Any:
    """The real MetaTrader5 module. Raises ImportError on machines without the package."""
    import MetaTrader5  # type: ignore[import-not-found]  # the one import of the package in the project

    return MetaTrader5


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class Mt5Adapter:
    def __init__(self, api: Any, info_ttl: float = 0.0, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.api = api
        self.info_ttl = info_ttl     # seconds a symbol_info answer may be reused (0: never)
        self._monotonic = monotonic
        self._info: dict[str, tuple[float, Any]] = {}

    def verify_constants(self) -> list[str]:
        """Names whose value differs between this package build and the domain constants."""
        wrong = []
        for name, value in CHECKED_CONSTANTS.items():
            actual = getattr(self.api, name, value)
            if actual != value:
                wrong.append(f"{name}={actual} (expected {value})")
        return wrong

    # ------------------------------------------------------------------ connection

    def connect(self, **kwargs: Any) -> bool:
        return bool(self.api.initialize(**kwargs))

    def disconnect(self) -> None:
        self.api.shutdown()

    def last_error(self) -> Any:
        try:
            return self.api.last_error()
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            return "unknown"

    def terminal(self) -> Any:
        return self.api.terminal_info()

    def account(self) -> Any:
        return self.api.account_info()

    # ----------------------------------------------------------------- market data

    def symbol_info(self, symbol: str) -> Any:
        if self.info_ttl <= 0:
            return self.api.symbol_info(symbol)
        now = self._monotonic()
        cached = self._info.get(symbol)
        if cached is not None and now < cached[0]:
            return cached[1]
        info = self.api.symbol_info(symbol)
        if info is not None:
            self._info[symbol] = (now + self.info_ttl, info)
        return info

    def tick(self, symbol: str) -> Any:
        return self.api.symbol_info_tick(symbol)

    def rates(self, symbol: str, timeframe: str, start: int, count: int) -> Any:
        return self.api.copy_rates_from_pos(symbol, getattr(self.api, f"TIMEFRAME_{timeframe}"), start, count)

    def select_symbol(self, symbol: str) -> bool:
        return bool(self.api.symbol_select(symbol, True))

    def symbols(self, group: Optional[str] = None) -> Sequence[Any]:
        getter = getattr(self.api, "symbols_get", None)
        if getter is None:
            return ()
        try:
            return getter(group=group) or ()
        except TypeError:
            return getter() or ()

    # ----------------------------------------------------------------------- state

    def positions(self, symbol: Optional[str] = None) -> Optional[Sequence[Any]]:
        return self.api.positions_get(symbol=symbol) if symbol else self.api.positions_get()

    def pending_orders(self) -> Optional[Sequence[Any]]:
        getter = getattr(self.api, "orders_get", None)
        return getter() if getter is not None else ()

    def _empty_or_error(self, result: Any) -> Optional[Sequence[Any]]:
        """``None`` means an error, except that some terminal builds also return it for "nothing
        found" while last_error() says success (code 1). Anything else stays unknown."""
        if result is not None:
            return result
        error = self.last_error()
        return () if isinstance(error, (tuple, list)) and error and error[0] == 1 else None

    def deals_between(self, date_from: float, date_to: float) -> Optional[Sequence[Any]]:
        getter = getattr(self.api, "history_deals_get", None)
        if getter is None:
            return None
        try:
            return self._empty_or_error(getter(_utc(date_from), _utc(date_to)))
        except Exception:  # noqa: BLE001 - "unknown", which callers treat as unknown
            log.debug("history_deals_get failed", exc_info=True)
            return None

    def deals_for_position(self, position: int) -> Optional[Sequence[Any]]:
        getter = getattr(self.api, "history_deals_get", None)
        if getter is None:
            return None
        try:
            return getter(position=int(position)) or ()
        except Exception:  # noqa: BLE001
            log.debug("history_deals_get(position=%s) failed", position, exc_info=True)
            return None

    def history_orders_between(self, date_from: float, date_to: float) -> Optional[Sequence[Any]]:
        getter = getattr(self.api, "history_orders_get", None)
        if getter is None:
            return None
        try:
            return self._empty_or_error(getter(_utc(date_from), _utc(date_to)))
        except Exception:  # noqa: BLE001
            log.debug("history_orders_get failed", exc_info=True)
            return None

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float,
                    price_close: float) -> Optional[float]:
        return self.api.order_calc_profit(order_type_of(side), symbol, volume, price_open, price_close)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> Optional[float]:
        calc = getattr(self.api, "order_calc_margin", None)
        return None if calc is None else calc(order_type_of(side), symbol, volume, price)

    # --------------------------------------------------------------------- trading

    def wire_request(self, request: OrderRequest) -> dict[str, Any]:
        """The dict ``order_send`` expects."""
        if request.kind == "sltp":
            return {"action": TRADE_ACTION_SLTP, "symbol": request.symbol, "position": request.position,
                    "sl": request.sl, "tp": request.tp, "magic": request.magic, "comment": request.comment}
        if request.kind == "remove":
            return {"action": TRADE_ACTION_REMOVE, "order": request.order, "magic": request.magic,
                    "comment": request.comment}
        wire: dict[str, Any] = {
            "action": TRADE_ACTION_DEAL, "symbol": request.symbol, "volume": request.volume,
            "type": order_type_of(request.side or "BUY"), "price": request.price, "deviation": request.deviation,
            "magic": request.magic, "comment": request.comment, "type_time": ORDER_TIME_GTC,
            "type_filling": request.filling,
        }
        if request.kind == "open":
            wire["sl"], wire["tp"] = request.sl, request.tp
        if request.position is not None:
            wire["position"] = request.position
        return wire

    @staticmethod
    def _reply(result: Any) -> BrokerReply:
        return BrokerReply(int(result.retcode), int(getattr(result, "order", 0) or 0),
                           int(getattr(result, "deal", 0) or 0), float(getattr(result, "volume", 0.0) or 0.0),
                           float(getattr(result, "price", 0.0) or 0.0), str(getattr(result, "comment", "") or ""))

    def check(self, request: OrderRequest) -> Optional[BrokerReply]:
        result = self.api.order_check(self.wire_request(request))
        return None if result is None else self._reply(result)

    def send(self, request: OrderRequest) -> BrokerReply:
        try:
            result = self.api.order_send(self.wire_request(request))
        except Exception as exc:  # noqa: BLE001 - the request may have reached the server: uncertain
            log.exception("order_send raised")
            return BrokerReply(None, error=f"{type(exc).__name__}: {exc}")
        if result is None:
            return BrokerReply(None, error=str(self.last_error()))
        return self._reply(result)
