"""Broker vocabulary of the domain: numeric enums and value objects.

The numbers are the documented MQL5 enum values, repeated here so the core never imports
the ``MetaTrader5`` package. ``adapters.mt5.Mt5Adapter.verify_constants`` compares them with
the real package at start-up and refuses to trade if a future build changes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Protocol

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6
TRADE_ACTION_REMOVE = 8
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_FULL = 4
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_OUT_BY = 3
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2
DEAL_TYPE_CREDIT = 3
DEAL_TYPE_CHARGE = 4
DEAL_TYPE_CORRECTION = 5
DEAL_TYPE_BONUS = 6

# Bit flags of symbol_info.filling_mode (MQL5 SYMBOL_FILLING_*).
FILLING_FLAG_FOK = 1
FILLING_FLAG_IOC = 2

# Every constant the adapter checks against the installed MetaTrader5 package.
CHECKED_CONSTANTS: dict[str, int] = {
    "ORDER_TYPE_BUY": ORDER_TYPE_BUY, "ORDER_TYPE_SELL": ORDER_TYPE_SELL, "POSITION_TYPE_BUY": POSITION_TYPE_BUY,
    "POSITION_TYPE_SELL": POSITION_TYPE_SELL, "TRADE_ACTION_DEAL": TRADE_ACTION_DEAL,
    "TRADE_ACTION_SLTP": TRADE_ACTION_SLTP, "TRADE_ACTION_REMOVE": TRADE_ACTION_REMOVE,
    "ORDER_TIME_GTC": ORDER_TIME_GTC, "ORDER_FILLING_FOK": ORDER_FILLING_FOK, "ORDER_FILLING_IOC": ORDER_FILLING_IOC,
    "ORDER_FILLING_RETURN": ORDER_FILLING_RETURN, "SYMBOL_TRADE_MODE_FULL": SYMBOL_TRADE_MODE_FULL,
    "ACCOUNT_TRADE_MODE_DEMO": ACCOUNT_TRADE_MODE_DEMO, "ACCOUNT_TRADE_MODE_CONTEST": ACCOUNT_TRADE_MODE_CONTEST,
    "ACCOUNT_TRADE_MODE_REAL": ACCOUNT_TRADE_MODE_REAL,
    "ACCOUNT_MARGIN_MODE_RETAIL_NETTING": ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
    "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING": ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    "DEAL_ENTRY_IN": DEAL_ENTRY_IN, "DEAL_ENTRY_OUT": DEAL_ENTRY_OUT, "DEAL_ENTRY_OUT_BY": DEAL_ENTRY_OUT_BY,
    "DEAL_TYPE_BUY": DEAL_TYPE_BUY, "DEAL_TYPE_SELL": DEAL_TYPE_SELL, "DEAL_TYPE_BALANCE": DEAL_TYPE_BALANCE,
}

Side = Literal["BUY", "SELL"]
RequestKind = Literal["open", "close", "sltp", "remove"]


def order_type_of(side: str) -> int:
    return ORDER_TYPE_BUY if side == "BUY" else ORDER_TYPE_SELL


def side_of_position(position_type: int) -> Side:
    return "BUY" if position_type == POSITION_TYPE_BUY else "SELL"


def select_filling(filling_flags: int) -> int:
    """A filling type the symbol supports: IOC, then FOK, else RETURN."""
    if filling_flags & FILLING_FLAG_IOC:
        return ORDER_FILLING_IOC
    if filling_flags & FILLING_FLAG_FOK:
        return ORDER_FILLING_FOK
    return ORDER_FILLING_RETURN


class TickLike(Protocol):
    @property
    def time(self) -> int: ...
    @property
    def bid(self) -> float: ...
    @property
    def ask(self) -> float: ...


class PositionLike(Protocol):
    @property
    def ticket(self) -> int: ...
    @property
    def symbol(self) -> str: ...
    @property
    def type(self) -> int: ...
    @property
    def volume(self) -> float: ...
    @property
    def magic(self) -> int: ...
    @property
    def price_open(self) -> float: ...
    @property
    def sl(self) -> float: ...
    @property
    def tp(self) -> float: ...


@dataclass(frozen=True)
class OrderRequest:
    """A broker-neutral trade request. Adapters translate it; nothing else builds wire formats."""

    kind: RequestKind
    symbol: str = ""
    side: Optional[Side] = None
    volume: float = 0.0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 0
    magic: int = 0
    comment: str = ""
    position: Optional[int] = None
    order: Optional[int] = None
    filling: int = ORDER_FILLING_RETURN


@dataclass(frozen=True)
class BrokerReply:
    """What the broker said. ``retcode`` None means no answer at all (not "not executed")."""

    retcode: Optional[int]
    order: int = 0
    deal: int = 0
    volume: float = 0.0
    price: float = 0.0
    comment: str = ""
    error: str = ""
