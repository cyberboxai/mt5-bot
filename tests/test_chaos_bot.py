"""Chaos through the whole kernel: the Bot, its journal and the shared matching engine."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mt5bot.adapters.exchange import SimCosts
from mt5bot.adapters.mt5 import Mt5Adapter
from mt5bot.adapters.sim_broker import SimBroker
from mt5bot.bot import Bot
from mt5bot.clock import VirtualClock
from mt5bot.config import Config, apply_overrides
from mt5bot.core.orders import OrderState
from mt5bot.execution import project_orders
from mt5bot.sim.market import DEFAULT_SYMBOLS, SyntheticMarket
from mt5bot.sim.runner import simulation_config

WARMUP = 1200


class Rig:
    def __init__(self, state: Path, costs: SimCosts = SimCosts()) -> None:
        self.state = state
        self.sim = SimBroker(SyntheticMarket(31, symbols=DEFAULT_SYMBOLS, bar_seconds=300), WARMUP, costs, seed=3)
        self.clock = VirtualClock(self.sim.utc_now())
        self.boot()

    def boot(self) -> None:
        cfg = simulation_config(Config.from_dict({"state_root": str(self.state)}), self.state, "baseline")
        cfg = apply_overrides(cfg, {"universe.candidates": tuple(s.name for s in DEFAULT_SYMBOLS), "universe.max_symbols": 3})
        (self.state / "x").mkdir(parents=True, exist_ok=True)
        self.bot = Bot(cfg, broker=self.sim, market=self.sim, wait=self.clock.sleep, env={}, monotonic=self.clock,
                       utc_now=self.clock, config_path=str(self.state / "c.json"), globber=lambda p: [], no_claude=True,
                       spawn=lambda *a, **k: type("P", (), {"poll": lambda s: 0})(), durable_files=False, auto_migrate=False)
        self.bot.startup()

    def bars(self, n: int, script: Any = ()) -> None:
        pending = list(script)
        for _ in range(n):
            if pending:
                self.sim.chaos.append(pending.pop(0))
            self.sim.advance_bar()
            self.clock.t = self.sim.utc_now()
            self.bot.run_once()

    def opens(self) -> list[Any]:
        return [r for r in self.sim.sent if r.kind == "open"]

    def close(self) -> None:
        self.bot.close()


def test_chaos_never_breaks_the_one_position_per_symbol_rule(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    script = ["timeout_after_fill", "none_after_fill", ("late_fill", 3), "ignore_stops", ("partial", 0.5),
              "timeout_no_fill", ("reject", 10004), ("reject", 10019), "placed"] * 6
    for _ in range(25):
        rig.bars(8, script[:8])
        script = script[8:]
        symbols = [p.symbol for p in rig.sim.positions_book if p.magic == rig.bot.cfg.magic]
        assert len(symbols) == len(set(symbols)), symbols
        for p in rig.sim.positions_book:
            assert p.sl and p.tp, "a position stayed unprotected across a bar"
    assert rig.opens(), "no orders: the test would prove nothing"
    project_orders(rig.bot.registry.conn)        # every stream obeys the state machine
    rig.close()


def test_unresolved_orders_settle_and_their_fills_reach_the_journal(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bars(60, ["timeout_after_fill"] * 60)
    conn = rig.bot.registry.conn
    assert not rig.bot.engine.journal.unresolved()
    for row in conn.execute("SELECT client_id, position_ticket FROM orders WHERE state = 'FILLED' AND kind = 'open'"):
        assert rig.bot.registry.trade_by_ticket(int(row["position_ticket"])) is not None
    rig.close()


def test_a_restart_with_open_positions_resumes_without_a_second_order(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bars(40)
    before = len(rig.opens())
    open_now = len(rig.sim.positions_book)
    rig.close()
    rig.boot()
    assert len(rig.sim.positions_book) == open_now
    assert len(rig.opens()) == before                     # restart alone sends nothing
    rig.bars(5)
    symbols = [p.symbol for p in rig.sim.positions_book]
    assert len(symbols) == len(set(symbols))
    rig.close()


def test_database_trouble_mid_order_is_recovered_by_reconciliation(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bars(30)
    original = rig.bot.engine.journal.transition
    failed: list[int] = []

    def flaky(rec: Any, state: Any, **changes: Any) -> Any:
        if state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED) and not failed:
            failed.append(1)
            raise sqlite3.OperationalError("database is locked")
        return original(rec, state, **changes)

    rig.bot.engine.journal.transition = flaky  # type: ignore[method-assign]
    rig.bars(60)
    rig.bot.engine.journal.transition = original  # type: ignore[method-assign]
    rig.bars(3)
    assert failed, "no fill happened: the test would prove nothing"
    assert not rig.bot.engine.journal.unresolved()
    symbols = [p.symbol for p in rig.sim.positions_book]
    assert len(symbols) == len(set(symbols))
    rig.close()


def test_corrupted_risk_state_refuses_to_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    state_dir = rig.bot.state_dir
    rig.close()
    (state_dir / "risk_state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        rig.boot()


def test_a_clock_jump_changes_no_order_decision(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bars(30)
    before = len(rig.opens())
    rig.clock.t += 5 * 86400       # the PC clock jumps; the broker's bars have not moved
    rig.bot.run_once()
    assert len(rig.opens()) == before
    rig.close()


def test_a_terminal_that_stops_answering_never_counts_as_flat(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bars(30)
    rig.sim._blind_reads = 6
    rig.bars(1)
    symbols = [p.symbol for p in rig.sim.positions_book]
    assert len(symbols) == len(set(symbols))
    rig.close()


def test_the_live_adapter_handles_a_partial_fill_like_the_simulator(fake_mt5: Any, make_bot: Any) -> None:
    """10010 through the real adapter: the journal records what was filled, not what was asked."""
    bot = make_bot()
    bot.startup()
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    fake_mt5.send_retcodes = [10010]
    original = fake_mt5.order_send

    def partial(request: dict[str, Any]) -> Any:
        result = original(request)
        pos = fake_mt5.add_position(request["symbol"], "BUY", request["magic"], round(request["volume"] / 2, 2),
                                    request["price"], request["sl"], request["tp"], request["comment"])
        result.volume, result.order, result.deal = pos.volume, pos.ticket, pos.ticket + 1
        return result

    fake_mt5.order_send = partial  # type: ignore[method-assign]
    bot.run_once()
    [row] = bot.registry.trades(mode="live", status="open")
    assert row.volume == fake_mt5.positions[0].volume
    assert isinstance(bot.broker, Mt5Adapter)
    [order] = bot.engine.journal.in_state(OrderState.PARTIALLY_FILLED)
    assert order.filled_volume == row.volume < order.volume


def test_netting_journal_uses_the_position_ticket_not_the_order(fake_mt5: Any, make_bot: Any) -> None:
    """Regression: the journal stored the order ticket, which only equals the position on hedging."""
    fake_mt5.account.margin_mode = fake_mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING
    bot = make_bot()
    bot.startup()
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    original = fake_mt5.order_send

    def different_ticket(request: dict[str, Any]) -> Any:
        result = original(request)
        result.order = 987654
        return result

    fake_mt5.order_send = different_ticket  # type: ignore[method-assign]
    bot.run_once()
    [row] = bot.registry.trades(mode="live", status="open")
    assert row.ticket == fake_mt5.positions[0].ticket != 987654


def test_rejections_from_a_lossy_broker_leave_a_measured_trail(tmp_path: Path) -> None:
    rig = Rig(tmp_path, SimCosts(reject_rate=0.3, requote_rate=0.3))
    rig.bars(120)
    conn = rig.bot.registry.conn
    rows = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM tca GROUP BY status")}
    assert sum(rows.values()) >= 3 and rows == rows
    rig.close()
