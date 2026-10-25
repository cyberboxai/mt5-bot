"""Test fixtures: a stub MetaTrader5 module, an in-memory fake terminal and synthetic data.

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
    "TRADE_ACTION_SLTP": 6,
    "TRADE_ACTION_REMOVE": 8,
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
    "ACCOUNT_MARGIN_MODE_RETAIL_NETTING": 0,
    "ACCOUNT_MARGIN_MODE_EXCHANGE": 1,
    "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING": 2,
    "DEAL_ENTRY_IN": 0,
    "DEAL_ENTRY_OUT": 1,
    "DEAL_ENTRY_OUT_BY": 3,
    "DEAL_TYPE_BUY": 0,
    "DEAL_TYPE_SELL": 1,
    "DEAL_TYPE_BALANCE": 2,
    "TRADE_RETCODE_REQUOTE": 10004,
    "TRADE_RETCODE_REJECT": 10006,
    "TRADE_RETCODE_PLACED": 10008,
    "TRADE_RETCODE_DONE": 10009,
    "TRADE_RETCODE_DONE_PARTIAL": 10010,
    "TRADE_RETCODE_INVALID_STOPS": 10016,
    "TRADE_RETCODE_MARKET_CLOSED": 10018,
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

from mt5bot.backtest import CostModel  # noqa: E402  (imported after the stub is installed)
from mt5bot.config import Config  # noqa: E402
from mt5bot.risk import SymbolSpec  # noqa: E402
from mt5bot.registry import Registry  # noqa: E402

Registry.durable = False  # see Registry.durable; test_autonomy covers the WAL mode

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
BASE_TIME = 1_760_000_400  # 2025-10-09 09:00:00 "server time": a Thursday inside the 7-20 window


def make_rates(closes: Any, start_time: int = BASE_TIME, half_range: float = 0.0005,
               bar_seconds: int = BAR_SECONDS, spread: int = 10) -> np.ndarray:
    """Build an MT5-like rates array from close prices."""
    closes = np.asarray(closes, dtype=float)
    rates = np.zeros(closes.size, dtype=RATES_DTYPE)
    rates["time"] = start_time + np.arange(closes.size) * bar_seconds
    rates["open"] = np.concatenate([[closes[0]], closes[:-1]])
    rates["high"] = np.maximum(rates["open"], closes) + half_range
    rates["low"] = np.minimum(rates["open"], closes) - half_range
    rates["close"] = closes
    rates["tick_volume"] = 100
    rates["spread"] = spread
    return rates


def synthetic_bars(n: int, seed: int, bar_seconds: int = 3600, vol: float = 0.0012, trend: float = 0.0,
                   regime_bars: int = 150, start_time: int = 1_699_920_000) -> np.ndarray:
    """Random walk (trend=0) or random walk plus persistent trend regimes (a genuine edge).

    ``trend`` is the drift per bar in units of the per-bar volatility, with a random sign
    that persists for geometric-length regimes of mean ``regime_bars`` bars.
    """
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, vol, n)
    drift = np.zeros(n)
    i = 0
    while i < n and trend:
        length = int(rng.geometric(1 / regime_bars))
        drift[i:i + length] = rng.choice([-1, 1]) * trend * vol
        i += length
    close = 1.1 * np.exp(np.cumsum(eps + drift))
    bars = np.zeros(n, dtype=RATES_DTYPE)
    bars["time"] = start_time + np.arange(n) * bar_seconds
    bars["open"] = np.concatenate([[close[0]], close[:-1]])
    wiggle = np.abs(rng.normal(0, vol * 0.5, n)) * close
    bars["high"] = np.maximum(bars["open"], close) + wiggle
    bars["low"] = np.minimum(bars["open"], close) - wiggle
    bars["close"] = close
    bars["tick_volume"] = 100
    bars["spread"] = 10
    return bars


EURUSD_SPEC = SymbolSpec("EURUSD", 0.00001, 5, 1.0, 0.00001, 100_000, 0.01, 100.0, 0.01)
TEST_COSTS = CostModel(commission_per_lot=7.0, slippage_points=2.0, max_spread_points=30.0, trading_hours=(0, 24))


class FakeMT5:
    """In-memory MT5 terminal with one demo hedging account and FX-like symbols."""

    def __init__(self) -> None:
        for name, value in MT5_CONSTANTS.items():
            setattr(self, name, value)
        self.connected = True
        self.init_ok = True
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True, path=r"C:\MT5")
        self.account = SimpleNamespace(
            login=87654321,
            server="Broker-Demo",
            currency="USD",
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
            leverage=100,
            trade_allowed=True,
            trade_expert=True,
            equity=10_000.0,
            balance=10_000.0,
        )
        self.symbols: dict[str, SimpleNamespace] = {}
        self.ticks: dict[str, SimpleNamespace] = {}
        self.rates: dict[str, np.ndarray] = {}
        self.positions: list[SimpleNamespace] = []
        self.orders: list[SimpleNamespace] = []
        self.deals: list[SimpleNamespace] = []
        self.init_calls: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.sends: list[dict[str, Any]] = []
        self.send_retcodes: list[int] = []
        self.check_retcode = 0
        self.fill_slippage = 0.0
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
            trade_freeze_level=0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            filling_mode=2,  # IOC
            trade_contract_size=100_000,
            trade_tick_value=1.0,
            trade_tick_size=0.00001,
            swap_long=0.0,
            swap_short=0.0,
            swap_mode=1,
            swap_rollover3days=3,
            currency_base=name[:3],
            currency_profit=name[3:6],
        )
        self.rates[name] = make_rates(np.full(1200, bid))
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

    def add_position(self, symbol: str, side: str, magic: int, volume: float = 0.1, price_open: float = 1.1,
                     sl: float = 0.0, tp: float = 0.0) -> SimpleNamespace:
        self._next_ticket += 1
        pos = SimpleNamespace(
            ticket=self._next_ticket,
            symbol=symbol,
            type=self.POSITION_TYPE_BUY if side == "BUY" else self.POSITION_TYPE_SELL,
            volume=volume,
            magic=magic,
            price_open=price_open,
            sl=sl,
            tp=tp,
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

    def symbols_get(self, group: Optional[str] = None) -> tuple[Any, ...]:
        needle = (group or "").strip("*").upper()
        return tuple(s for s in self.symbols.values() if needle in s.name.upper())

    def symbol_info_tick(self, name: str) -> Any:
        return self.ticks.get(name)

    def copy_rates_from_pos(self, name: str, timeframe: int, start: int, count: int) -> Any:
        if name in self.rates_error:
            raise self.rates_error[name]
        rates = self.rates.get(name)
        if rates is None:
            return None
        end = len(rates) - start
        return rates[max(0, end - count):end]

    def positions_get(self, symbol: Optional[str] = None) -> tuple[Any, ...]:
        return tuple(p for p in self.positions if symbol is None or p.symbol == symbol)

    def orders_get(self) -> tuple[Any, ...]:
        return tuple(self.orders)

    def history_deals_get(self, *args: Any, position: Optional[int] = None) -> tuple[Any, ...]:
        return tuple(d for d in self.deals if position is None or getattr(d, "position_id", None) == position)

    def order_calc_profit(self, order_type: int, symbol: str, volume: float, price_open: float,
                          price_close: float) -> float:
        size = self.symbols[symbol].trade_contract_size
        direction = 1 if order_type == self.ORDER_TYPE_BUY else -1
        return (price_close - price_open) * direction * volume * size

    def order_calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float:
        return volume * self.symbols[symbol].trade_contract_size * price / self.account.leverage

    def order_check(self, request: dict[str, Any]) -> Any:
        self.checks.append(dict(request))
        return SimpleNamespace(retcode=self.check_retcode, comment="Done" if self.check_retcode == 0 else "Rejected")

    def order_send(self, request: dict[str, Any]) -> Any:
        self.sends.append(dict(request))
        retcode = self.send_retcodes.pop(0) if self.send_retcodes else self.TRADE_RETCODE_DONE
        price = request.get("price", 0.0)
        if retcode == self.TRADE_RETCODE_DONE:
            action = request.get("action")
            if action == self.TRADE_ACTION_SLTP:
                for p in self.positions:
                    if p.ticket == request["position"]:
                        p.sl, p.tp = request["sl"], request["tp"]
            elif action == self.TRADE_ACTION_REMOVE:
                self.orders = [o for o in self.orders if o.ticket != request["order"]]
            elif "position" in request:
                self.positions = [p for p in self.positions if p.ticket != request["position"]]
            else:
                side = "BUY" if request["type"] == self.ORDER_TYPE_BUY else "SELL"
                existing = [p for p in self.positions if p.symbol == request["symbol"]]
                netting = self.account.margin_mode != self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
                opposite = [p for p in existing if (p.type == self.POSITION_TYPE_BUY) != (side == "BUY")]
                if netting and opposite:
                    self.positions = [p for p in self.positions if p not in opposite]
                else:
                    price = price + (self.fill_slippage if side == "BUY" else -self.fill_slippage)
                    pos = self.add_position(request["symbol"], side, request["magic"], request["volume"],
                                            price_open=price, sl=request.get("sl", 0.0), tp=request.get("tp", 0.0))
                    return SimpleNamespace(retcode=retcode, comment="done", volume=request["volume"], price=price,
                                           order=pos.ticket, deal=pos.ticket + 50_000)
        return SimpleNamespace(retcode=retcode, comment=f"retcode {retcode}", volume=request.get("volume", 0.0),
                               price=price, order=1, deal=2)


class FixedSource:
    """Signal source that always proposes the same side (replaces the adaptive engine)."""

    def __init__(self, side: Optional[str] = "BUY", atr: float = 0.0010, exit_on_opposite: bool = True) -> None:
        self.side = side
        self.atr = atr
        self.exit_on_opposite = exit_on_opposite
        self.seen: list[np.ndarray] = []

    def on_bar(self, symbol: str, closed: np.ndarray, spec: Any, costs: Any, equity: float, risk_pct: float):
        from mt5bot.engine import EntryIntent
        from mt5bot.strategies.base import Signal

        self.seen.append(closed.copy())
        if self.side is None:
            return None
        return EntryIntent(Signal(self.side, self.atr, 2.0, 3.0), None, "fixed", f"fixed {self.side}")

    def exit_signal(self, symbol: str, param_set_id: Any, side: str, closed: np.ndarray) -> bool:
        return self.exit_on_opposite and self.side is not None and side != self.side


@pytest.fixture
def fake_mt5() -> FakeMT5:
    return FakeMT5()


@pytest.fixture
def make_cfg(tmp_path: Path):
    """Config factory with file paths isolated in tmp_path."""

    def factory(**overrides: Any) -> Config:
        values: dict[str, Any] = {
            "state_root": str(tmp_path / "state"),
            "reports_dir": str(tmp_path / "reports"),
            "health_file": str(tmp_path / "health.json"),
            "kill_switch_file": str(tmp_path / "KILL"),
            "log_file": str(tmp_path / "bot.log"),
            "trading_hours_server": [0, 24],
        }
        values.update(overrides)
        return Config.from_dict(values)

    return factory


@pytest.fixture
def waits() -> list[float]:
    return []


@pytest.fixture
def make_bot(fake_mt5: FakeMT5, make_cfg, waits: list[float], tmp_path: Path):
    """Bot factory wired to the fake terminal, a recording wait() and an empty environment."""
    from mt5bot.bot import Bot

    created: list[Bot] = []

    def factory(cfg: Optional[Config] = None, source: Any = "default", env: Optional[dict] = None,
                **kwargs: Any) -> Bot:
        bot_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in ("confirm_real_money", "monotonic", "utc_now",
                                                                       "spawn", "globber", "terminal_killer")}
        cfg = cfg or make_cfg(**kwargs)
        signal_source = FixedSource() if source == "default" else source
        bot = Bot(cfg, api=fake_mt5, wait=waits.append, env=env or {}, signal_source=signal_source,
                  config_path=str(tmp_path / "config.json"), globber=lambda pattern: [], **bot_kwargs)
        created.append(bot)
        return bot

    yield factory
    for bot in created:
        bot.close()
