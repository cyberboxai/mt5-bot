"""Test fixtures: a stub MetaTrader5 module and an in-memory fake terminal.

The stub is injected unconditionally, even on Windows with the real package installed,
so the test suite can never reach a real terminal or account.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import numpy as np
import pytest

# Values match the MQL5 / MetaTrader5 package constants.
MT5_CONSTANTS: dict[str, int] = {
    "TIMEFRAME_M1": 1,
    "TIMEFRAME_M5": 5,
    "TIMEFRAME_M15": 15,
    "TIMEFRAME_M30": 30,
    "TIMEFRAME_H1": 16385,
    "TIMEFRAME_H4": 16388,
    "ORDER_TYPE_BUY": 0,
    "ORDER_TYPE_SELL": 1,
    "POSITION_TYPE_BUY": 0,
    "POSITION_TYPE_SELL": 1,
    "TRADE_ACTION_DEAL": 1,
    "ORDER_TIME_GTC": 0,
    "ORDER_FILLING_FOK": 0,
    "ORDER_FILLING_IOC": 1,
    "ORDER_FILLING_RETURN": 2,
    "SYMBOL_TRADE_MODE_DISABLED": 0,
    "SYMBOL_TRADE_MODE_LONGONLY": 1,
    "SYMBOL_TRADE_MODE_SHORTONLY": 2,
    "SYMBOL_TRADE_MODE_CLOSEONLY": 3,
    "SYMBOL_TRADE_MODE_FULL": 4,
    "ACCOUNT_TRADE_MODE_DEMO": 0,
    "ACCOUNT_TRADE_MODE_CONTEST": 1,
    "ACCOUNT_TRADE_MODE_REAL": 2,
    "TRADE_RETCODE_REQUOTE": 10004,
    "TRADE_RETCODE_REJECT": 10006,
    "TRADE_RETCODE_PLACED": 10008,
    "TRADE_RETCODE_DONE": 10009,
    "TRADE_RETCODE_DONE_PARTIAL": 10010,
    "TRADE_RETCODE_INVALID_STOPS": 10016,
    "TRADE_RETCODE_NO_MONEY": 10019,
    "TRADE_RETCODE_PRICE_CHANGED": 10020,
    "TRADE_RETCODE_PRICE_OFF": 10021,
}


def _build_stub_module() -> types.ModuleType:
    stub = types.ModuleType("MetaTrader5")
    for name, value in MT5_CONSTANTS.items():
        setattr(stub, name, value)
    # Module-level functions behave like a machine with no terminal running.
    stub.initialize = lambda *a, **k: False
    stub.shutdown = lambda: None
    stub.last_error = lambda: (-10003, "IPC initialize failed, MetaTrader 5 x64 not found")
    return stub


sys.modules["MetaTrader5"] = _build_stub_module()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mt5_bot  # noqa: E402  (must be imported after the stub is installed)

RATES_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ]
)

BAR_SECONDS = 15 * 60
BASE_TIME = 1_760_000_400  # 2025-10-09 09:00:00 "server time": inside the default 7-20 window


def make_rates(closes: Any, start_time: int = BASE_TIME, half_range: float = 0.0005) -> np.ndarray:
    """Build an MT5-like rates array from close prices."""
    closes = np.asarray(closes, dtype=float)
    rates = np.zeros(closes.size, dtype=RATES_DTYPE)
    rates["time"] = start_time + np.arange(closes.size) * BAR_SECONDS
    rates["open"] = np.concatenate([[closes[0]], closes[:-1]])
    rates["high"] = np.maximum(rates["open"], closes) + half_range
    rates["low"] = np.minimum(rates["open"], closes) - half_range
    rates["close"] = closes
    rates["tick_volume"] = 100
    rates["spread"] = 10
    return rates


class FakeMT5:
    """In-memory MT5 terminal with one demo account and FX-like symbols."""

    def __init__(self) -> None:
        for name, value in MT5_CONSTANTS.items():
            setattr(self, name, value)
        self.connected = True
        self.init_ok = True
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True)
        self.account = SimpleNamespace(
            login=87654321,
            server="Broker-Demo",
            currency="USD",
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            trade_allowed=True,
            trade_expert=True,
            equity=10_000.0,
            balance=10_000.0,
        )
        self.symbols: dict[str, SimpleNamespace] = {}
        self.ticks: dict[str, SimpleNamespace] = {}
        self.rates: dict[str, np.ndarray] = {}
        self.positions: list[SimpleNamespace] = []
        self.init_calls: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.sends: list[dict[str, Any]] = []
        self.send_retcodes: list[int] = []
        self.check_retcode = 0
        self.rates_error: dict[str, Exception] = {}
        self._next_ticket = 1000
        self.add_symbol("EURUSD")

    def add_symbol(self, name: str, bid: float = 1.10000, spread_points: int = 10) -> None:
        self.symbols[name] = SimpleNamespace(
            name=name,
            point=0.00001,
            digits=5,
            trade_mode=self.SYMBOL_TRADE_MODE_FULL,
            trade_stops_level=0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            filling_mode=2,  # IOC
            contract_size=100_000,
        )
        self.rates[name] = make_rates(np.full(400, bid))
        self.set_tick(name, bid=bid, spread_points=spread_points)

    def set_tick(self, name: str, bid: float, spread_points: int = 10, time: Optional[int] = None) -> None:
        point = self.symbols[name].point
        if time is None:
            time = int(self.rates[name]["time"][-1]) + 60 if name in self.rates else BASE_TIME
        self.ticks[name] = SimpleNamespace(time=time, bid=bid, ask=round(bid + spread_points * point, 5))

    def advance_bar(self, name: str, close: Optional[float] = None) -> None:
        rates = self.rates[name]
        new = rates[-1:].copy()
        new["time"] += BAR_SECONDS
        if close is not None:
            new["close"] = close
        self.rates[name] = np.concatenate([rates, new])
        tick = self.ticks[name]
        tick.time = int(new["time"][0]) + 60

    def add_position(self, symbol: str, side: str, magic: int, volume: float = 0.1) -> SimpleNamespace:
        self._next_ticket += 1
        pos = SimpleNamespace(
            ticket=self._next_ticket,
            symbol=symbol,
            type=self.POSITION_TYPE_BUY if side == "BUY" else self.POSITION_TYPE_SELL,
            volume=volume,
            magic=magic,
        )
        self.positions.append(pos)
        return pos

    # ---- MetaTrader5 API surface used by the bot

    def initialize(self, **kwargs: Any) -> bool:
        self.init_calls.append(kwargs)
        return self.init_ok

    def shutdown(self) -> None:
        pass

    def last_error(self) -> tuple[int, str]:
        return (1, "Success")

    def terminal_info(self) -> Any:
        return self.terminal if self.connected else None

    def account_info(self) -> Any:
        return self.account

    def symbol_select(self, name: str, enable: bool) -> bool:
        return name in self.symbols

    def symbol_info(self, name: str) -> Any:
        return self.symbols.get(name)

    def symbol_info_tick(self, name: str) -> Any:
        return self.ticks.get(name)

    def copy_rates_from_pos(self, name: str, timeframe: int, start: int, count: int) -> Any:
        if name in self.rates_error:
            raise self.rates_error[name]
        rates = self.rates.get(name)
        return None if rates is None else rates[-count:]

    def positions_get(self, symbol: Optional[str] = None) -> tuple[Any, ...]:
        return tuple(p for p in self.positions if symbol is None or p.symbol == symbol)

    def order_calc_profit(self, order_type: int, symbol: str, volume: float, price_open: float,
                          price_close: float) -> float:
        size = self.symbols[symbol].contract_size
        direction = 1 if order_type == self.ORDER_TYPE_BUY else -1
        return (price_close - price_open) * direction * volume * size

    def order_check(self, request: dict[str, Any]) -> Any:
        self.checks.append(dict(request))
        return SimpleNamespace(retcode=self.check_retcode, comment="Done" if self.check_retcode == 0 else "Rejected")

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sends.append(dict(request))
        retcode = self.send_retcodes.pop(0) if self.send_retcodes else self.TRADE_RETCODE_DONE
        if retcode == self.TRADE_RETCODE_DONE:
            if "position" in request:
                self.positions = [p for p in self.positions if p.ticket != request["position"]]
            else:
                side = "BUY" if request["type"] == self.ORDER_TYPE_BUY else "SELL"
                self.add_position(request["symbol"], side, request["magic"], request["volume"])
        return SimpleNamespace(retcode=retcode, comment=f"retcode {retcode}", volume=request["volume"],
                               price=request["price"], order=1, deal=2)


@pytest.fixture
def fake_mt5() -> FakeMT5:
    return FakeMT5()


@pytest.fixture
def make_cfg(tmp_path: Path):
    """Config factory with file paths isolated in tmp_path."""

    def factory(**overrides: Any) -> mt5_bot.Config:
        values: dict[str, Any] = {
            "state_file": str(tmp_path / "state.json"),
            "kill_switch_file": str(tmp_path / "KILL"),
            "log_file": str(tmp_path / "bot.log"),
        }
        values.update(overrides)
        return mt5_bot.Config.from_dict(values)

    return factory


@pytest.fixture
def waits() -> list[float]:
    return []


@pytest.fixture
def make_bot(fake_mt5: FakeMT5, make_cfg, waits: list[float]):
    """Bot factory wired to the fake terminal, a recording wait() and an empty environment."""

    def factory(cfg: Optional[mt5_bot.Config] = None, strategy: Any = None, env: Optional[dict] = None,
                **cfg_overrides: Any) -> mt5_bot.Bot:
        cfg = cfg or make_cfg(**cfg_overrides)
        kwargs: dict[str, Any] = {"api": fake_mt5, "wait": waits.append, "env": env or {}}
        if strategy is not None:
            kwargs["strategy"] = strategy
        return mt5_bot.Bot(cfg, **kwargs)

    return factory
