"""Phase 1: one kernel everywhere. Golden replay, adapter parity and architecture rules."""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import numpy as np
import pytest

from mt5bot.adapters.exchange import SimCosts
from mt5bot.adapters.fake import FakeBroker
from mt5bot.adapters.mt5 import Mt5Adapter
from mt5bot.adapters.shadow import ShadowBroker
from mt5bot.adapters.sim_broker import SimBroker, SnapshotFeed
from mt5bot.config import Config, config_hash
from mt5bot.core import broker as br
from mt5bot.core.broker import OrderRequest
from mt5bot.eventlog import decision_log
from mt5bot.execution import project_orders
from mt5bot.ports import BrokerPort, CalendarPort, ClockPort, MarketDataPort, NotifierPort, StorePort
from mt5bot.sim.market import DEFAULT_SYMBOLS, SyntheticMarket
from mt5bot.sim.runner import run_simulation

ROOT = Path(__file__).resolve().parent.parent
BARS = 120
WARMUP = 1200
BAR_SECONDS = 300


def snapshot_feed() -> SnapshotFeed:
    """A fixed data snapshot, as research would have stored it."""
    market = SyntheticMarket(21, symbols=DEFAULT_SYMBOLS, bar_seconds=BAR_SECONDS, random_walk=False)
    return SnapshotFeed(market.history(WARMUP + BARS + 5), DEFAULT_SYMBOLS, BAR_SECONDS)


def replay(state: Path, feed: Any = None, costs: Optional[SimCosts] = None, team_mode: str = "baseline") -> Path:
    cfg = Config.from_dict({"state_root": str(state)})
    overrides = {"tiers.exploration_enabled": True}
    run_simulation(cfg, state, steps=BARS, speed=0, seed=9, warmup_bars=WARMUP, advisor="none", team_mode=team_mode,
                   symbols=DEFAULT_SYMBOLS, feed=feed or snapshot_feed(), costs=costs, config_overrides=overrides)
    return next(state.rglob("mt5bot.sqlite"))


def log_of(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    out = decision_log(conn)
    conn.close()
    return out


def test_golden_replay_is_byte_identical_across_runs(tmp_path: Path) -> None:
    first = log_of(replay(tmp_path / "a"))
    second = log_of(replay(tmp_path / "b"))
    assert first, "the replay produced no decisions, so it proved nothing"
    assert any('"decision.proposed"' in line for line in first)
    assert first == second


def test_replay_differs_when_the_data_differs(tmp_path: Path) -> None:
    market = SyntheticMarket(22, symbols=DEFAULT_SYMBOLS, bar_seconds=BAR_SECONDS)
    other = SnapshotFeed(market.history(WARMUP + BARS + 5), DEFAULT_SYMBOLS, BAR_SECONDS)
    assert log_of(replay(tmp_path / "a")) != log_of(replay(tmp_path / "b", other))


def test_every_order_stream_obeys_the_state_machine_after_a_full_run(tmp_path: Path) -> None:
    db = replay(tmp_path / "a", costs=SimCosts(reject_rate=0.2, requote_rate=0.2, partial_rate=0.2))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    built = project_orders(conn)      # raises on any illegal transition
    rows = {r["client_id"]: r["state"] for r in conn.execute("SELECT client_id, state FROM orders")}
    assert rows and {k: v["state"] for k, v in built.items()} == rows
    conn.close()


class MtShim:
    """The MetaTrader5 module's surface over a SimBroker: the REAL live adapter runs against it."""

    TIMEFRAMES = {"M5": 5, "M15": 15, "H1": 16385, "H4": 16388, "D1": 16408}

    def __init__(self, sim: SimBroker) -> None:
        self.sim = sim
        for name, value in br.CHECKED_CONSTANTS.items():
            setattr(self, name, value)
        for name, value in self.TIMEFRAMES.items():
            setattr(self, f"TIMEFRAME_{name}", value)

    def initialize(self, **kwargs: Any) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self) -> Any:
        return (1, "Success")

    def terminal_info(self) -> Any:
        return self.sim.terminal()

    def account_info(self) -> Any:
        return self.sim.account()

    def symbol_select(self, name: str, enable: bool) -> bool:
        return self.sim.select_symbol(name)

    def symbols_get(self, group: Optional[str] = None) -> Any:
        return self.sim.symbols(group)

    def symbol_info(self, name: str) -> Any:
        return self.sim.symbol_info(name)

    def symbol_info_tick(self, name: str) -> Any:
        return self.sim.tick(name)

    def copy_rates_from_pos(self, name: str, timeframe: int, start: int, count: int) -> Any:
        tf = next(k for k, v in self.TIMEFRAMES.items() if v == timeframe)
        return self.sim.rates(name, tf, start, count)

    def positions_get(self, symbol: Optional[str] = None) -> Any:
        return self.sim.positions(symbol)

    def orders_get(self) -> Any:
        return self.sim.pending_orders()

    def history_deals_get(self, *args: Any, position: Optional[int] = None) -> Any:
        return self.sim.deals_for_position(position) if position is not None else self.sim.deals_between(0, 0)

    def order_calc_profit(self, order_type: int, symbol: str, volume: float, a: float, b: float) -> Any:
        return self.sim.calc_profit("BUY" if order_type == 0 else "SELL", symbol, volume, a, b)

    def order_calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> Any:
        return self.sim.calc_margin("BUY" if order_type == 0 else "SELL", symbol, volume, price)

    @staticmethod
    def _request(wire: dict[str, Any]) -> OrderRequest:
        side = "BUY" if wire.get("type") == 0 else "SELL"
        if wire["action"] == br.TRADE_ACTION_SLTP:
            return OrderRequest("sltp", wire["symbol"], sl=wire["sl"], tp=wire["tp"], magic=wire["magic"],
                                comment=wire["comment"], position=wire["position"])
        kind = "open" if "sl" in wire else "close"
        return OrderRequest(kind, wire["symbol"], side, wire["volume"], wire["price"], wire.get("sl", 0.0),
                            wire.get("tp", 0.0), wire["deviation"], wire["magic"], wire["comment"],
                            wire.get("position"), filling=wire["type_filling"])

    def order_check(self, wire: dict[str, Any]) -> Any:
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, wire: dict[str, Any]) -> Any:
        reply = self.sim.send(self._request(wire))
        return SimpleNamespace(retcode=reply.retcode, order=reply.order, deal=reply.deal, volume=reply.volume,
                               price=reply.price, comment=reply.comment)


def run_parity(state: Path, through_live_adapter: bool) -> tuple[list[str], list[OrderRequest]]:
    """The same bars and seed, driven step by step, with or without the live adapter in between."""
    from mt5bot.bot import Bot
    from mt5bot.sim.runner import VirtualClock, simulation_config

    cfg = simulation_config(Config.from_dict({"state_root": str(state)}), state, "baseline")
    from mt5bot.config import apply_overrides

    cfg = apply_overrides(cfg, {"universe.candidates": tuple(s.name for s in DEFAULT_SYMBOLS), "universe.max_symbols": 3})
    sim = SimBroker(snapshot_feed(), WARMUP, SimCosts(), seed=4)
    clock = VirtualClock(sim.utc_now())
    (state / "x").mkdir(parents=True, exist_ok=True)
    wiring: dict[str, Any] = {"api": MtShim(sim)} if through_live_adapter else {"broker": sim, "market": sim}
    bot = Bot(cfg, wait=lambda s: None, env={}, monotonic=clock, utc_now=clock, config_path=str(state / "c.json"),
              spawn=lambda *a, **k: SimpleNamespace(poll=lambda: 0), globber=lambda p: [], no_claude=True,
              durable_files=False, auto_migrate=False, **wiring)
    bot.startup()
    for _ in range(BARS):
        sim.advance_bar()
        clock.t = sim.utc_now()
        bot.run_once()
    bot.funnel.flush()
    log = decision_log(bot.registry.conn)
    bot.shutdown()
    bot.close()
    return log, sim.sent


def test_parity_of_the_simulated_and_the_live_code_path(tmp_path: Path) -> None:
    sim_log, sim_sent = run_parity(tmp_path / "sim", False)
    live_log, live_sent = run_parity(tmp_path / "live", True)
    assert sim_sent, "no orders: the test would prove nothing"
    assert [(r.kind, r.symbol, r.side, r.volume, r.sl, r.tp) for r in sim_sent] == \
           [(r.kind, r.symbol, r.side, r.volume, r.sl, r.tp) for r in live_sent]
    assert sim_log == live_log


def test_shadow_broker_trades_live_quotes_with_simulated_fills() -> None:
    live = FakeBroker()
    shadow = ShadowBroker(live, live)
    assert isinstance(shadow, BrokerPort) and isinstance(shadow, MarketDataPort)
    tick = shadow.tick("EURUSD")
    reply = shadow.send(OrderRequest("open", "EURUSD", "BUY", 0.1, tick.ask, tick.bid - 0.001, tick.bid + 0.001, magic=1))
    assert reply.retcode == 10009 and len(shadow.positions()) == 1 and live.positions_book == []
    live.set_price("EURUSD", 1.0)
    shadow.on_tick("EURUSD")
    assert shadow.positions() == () and shadow.account().balance < 10_000


@pytest.mark.parametrize("adapter", [FakeBroker(), Mt5Adapter(SimpleNamespace())])
def test_adapters_satisfy_the_ports(adapter: Any) -> None:
    assert isinstance(adapter, BrokerPort) and isinstance(adapter, MarketDataPort)


def test_clock_store_calendar_and_notifier_ports(tmp_path: Path) -> None:
    from mt5bot.clock import SystemClock, VirtualClock
    from mt5bot.notify import Notifier
    from mt5bot.registry import Registry
    from mt5bot.risk import NewsCalendar

    assert isinstance(SystemClock(), ClockPort) and isinstance(VirtualClock(1.0), ClockPort)
    assert isinstance(NewsCalendar(), CalendarPort) and isinstance(Notifier(False, {}), NotifierPort)
    reg = Registry(tmp_path / "x.sqlite")
    assert isinstance(reg, StorePort)
    reg.close()


def test_constants_mismatch_refuses_to_start() -> None:
    api = SimpleNamespace(TRADE_ACTION_DEAL=99)
    assert Mt5Adapter(api).verify_constants() == ["TRADE_ACTION_DEAL=99 (expected 1)"]


def test_uncertain_answers_never_raise_out_of_the_adapter() -> None:
    def boom(request: Any) -> Any:
        raise OSError("terminal died")

    api = SimpleNamespace(order_send=boom, last_error=lambda: (-1, "x"))
    reply = Mt5Adapter(api).send(OrderRequest("open", "EURUSD", "BUY", 0.1))
    assert reply.retcode is None and "terminal died" in reply.error
    silent = SimpleNamespace(order_send=lambda r: None, last_error=lambda: (-10004, "No IPC connection"))
    assert Mt5Adapter(silent).send(OrderRequest("open", "EURUSD", "BUY", 0.1)).retcode is None


def test_none_from_history_is_empty_only_when_the_terminal_says_success() -> None:
    ok = SimpleNamespace(history_deals_get=lambda *a: None, last_error=lambda: (1, "Success"))
    bad = SimpleNamespace(history_deals_get=lambda *a: None, last_error=lambda: (-10004, "No IPC"))
    assert Mt5Adapter(ok).deals_between(0, 1) == ()
    assert Mt5Adapter(bad).deals_between(0, 1) is None


def test_config_hash_ignores_locations_but_not_decisions(tmp_path: Path) -> None:
    a = Config.from_dict({"state_root": "x"})
    assert config_hash(a) == config_hash(Config.from_dict({"state_root": "y"}))
    assert config_hash(a) != config_hash(Config.from_dict({"state_root": "x", "costs": {"k": 3.0}}))


def imports_of(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def test_metatrader5_is_imported_only_by_the_live_adapter() -> None:
    offenders = [str(p.relative_to(ROOT)) for pkg in ("mt5bot", "panel") for p in (ROOT / pkg).rglob("*.py")
                 if "MetaTrader5" in imports_of(p) and p.name != "mt5.py"]
    assert offenders == []


def test_the_core_is_pure() -> None:
    forbidden = {"MetaTrader5", "sqlite3", "socket", "http", "urllib", "subprocess", "threading", "time", "os", "logging"}
    for path in (ROOT / "mt5bot" / "core").glob("*.py"):
        assert not imports_of(path) & forbidden, path.name
        relative = [n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                    if isinstance(n, ast.ImportFrom) and n.level >= 2]
        assert not relative, f"{path.name} imports from outside the core"


def test_the_core_has_no_wall_clock_or_uuid() -> None:
    for path in (ROOT / "mt5bot" / "core").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "datetime.now" not in text and "time.time" not in text and "uuid" not in text, path.name


def test_the_kernel_does_not_read_the_wall_clock() -> None:
    text = (ROOT / "mt5bot" / "bot.py").read_text(encoding="utf-8")
    assert "datetime.now" not in text and "uuid" not in text
    assert ".mt5." not in text


def test_data_roundtrip_of_a_snapshot_feed() -> None:
    feed = snapshot_feed()
    history = feed.history(100)
    assert np.array_equal(history["EURUSD"]["close"], feed.bars["EURUSD"]["close"][:100])
    t, bars = feed.next_bars()
    assert t == int(feed.bars["EURUSD"]["time"][100]) and bars[0][3] == feed.bars["EURUSD"]["close"][100]
    assert json.dumps({"ok": True})
