"""Agent framework: event bus ordering and persistence, supervision (crash / slow agents),
opinion validation and TTL, and the deterministic simulation feed."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mt5bot.config import Config
from mt5bot.officedb import OfficeStore
from mt5bot.sim.market import SyntheticMarket
from mt5bot.sim.terminal import SimulatedTerminal
from mt5bot.team.agent import Agent, AgentState, Opinion, TeamServices
from mt5bot.team.events import AgentSaid, Alert, Event, EventBus, GuardianAlert
from mt5bot.team.scheduler import AgentSupervisor


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------- event bus


def test_bus_orders_and_persists_events_from_many_threads(tmp_path: Path) -> None:
    store = OfficeStore(tmp_path / "office.sqlite")
    bus = EventBus(store)
    seen: list[tuple[str, int]] = []
    bus.subscribe("agent.*", lambda e: seen.append((e.agent, e.seq)))

    def worker(n: int) -> None:
        for i in range(200):
            bus.publish(AgentSaid(f"{n}-{i}"), agent=f"a{n}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bus.flush(10)
    rows = store.events_after(0, limit=5000)
    assert len(rows) == 600
    ids = [r["id"] for r in rows]
    texts = [r["payload"]["text"] for r in rows]
    assert ids == sorted(ids)
    # Database order == publish (sequence) order, and per-thread order is preserved.
    for n in range(3):
        mine = [t for t in texts if t.startswith(f"{n}-")]
        assert mine == [f"{n}-{i}" for i in range(200)]
    # Subscribers run in the publishing thread, outside the bus lock: every event is delivered
    # exactly once and each thread's events arrive in order (interleaving across threads may vary).
    assert len(seen) == 600 and len({seq for _a, seq in seen}) == 600
    for n in range(3):
        mine = [seq for agent, seq in seen if agent == f"a{n}"]
        assert len(mine) == 200 and mine == sorted(mine)
    bus.close()
    store.close()


def test_bus_topic_patterns_and_isolated_subscribers() -> None:
    bus = EventBus()
    got: dict[str, list[str]] = {"exact": [], "prefix": [], "all": []}
    bus.subscribe("guardian.alert", lambda e: got["exact"].append(e.topic))
    bus.subscribe("guardian.*", lambda e: got["prefix"].append(e.topic))
    bus.subscribe("*", lambda e: got["all"].append(e.topic))
    bus.subscribe("*", lambda e: 1 / 0)  # a broken subscriber must not break publishing
    event = bus.publish(GuardianAlert("sl", "missing", "freeze"), agent="guardian")
    bus.publish(Alert("x"))
    assert event.severity == "warning" and event.payload["invariant"] == "sl"
    assert got == {"exact": ["guardian.alert"], "prefix": ["guardian.alert"], "all": ["guardian.alert",
                                                                                     "system.alert"]}
    assert bus.subscriber_errors == 2


class SlowStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.gate = threading.Event()

    def insert_events(self, rows: list[dict]) -> list[int]:
        if threading.current_thread().name == "event-writer":
            self.gate.wait(5)  # the background writer is stuck (e.g. a locked database)
        self.rows.extend(rows)
        return list(range(len(rows)))


def test_bus_queue_is_bounded_and_never_loses_warnings() -> None:
    store = SlowStore()
    bus = EventBus(store, queue_size=5, batch_size=1)
    for i in range(50):
        bus.publish(AgentSaid(f"chatter {i}"))
    for i in range(3):
        bus.publish(Alert(f"important {i}"))
    assert bus.dropped > 0
    store.gate.set()
    assert bus.flush(5)
    texts = [r["payload"].get("text") for r in store.rows]
    assert all(f"important {i}" in texts for i in range(3))
    bus.close()


# ------------------------------------------------------------------- supervision


class GoodAgent(Agent):
    id = "trend_analyst"

    def analyze(self, ctx: Any) -> str:
        return "ok"


class CrashAgent(Agent):
    id = "breakout_analyst"
    resets = 0

    def analyze(self, ctx: Any) -> str:
        raise RuntimeError("boom")

    def reset(self) -> None:
        CrashAgent.resets += 1


class SlowAgent(Agent):
    id = "mean_reversion_analyst"

    def __init__(self, services: TeamServices) -> None:
        super().__init__(services)
        self.release = threading.Event()

    def analyze(self, ctx: Any) -> str:
        self.release.wait(5)
        return "late"


class CrashGuardian(Agent):
    id = "guardian"

    def analyze(self, ctx: Any) -> str:
        raise RuntimeError("guardian bug")


def supervisor(mono: Mono, bus: EventBus) -> tuple[AgentSupervisor, TeamServices]:
    services = TeamServices(bus=bus, cfg=Config())
    sup = AgentSupervisor(bus, timeout_ms=100, max_failures=3, window_seconds=600, backoff_seconds=60, monotonic=mono)
    return sup, services


def test_crashing_agent_is_isolated_benched_and_restarted() -> None:
    mono, bus = Mono(), EventBus()
    sup, services = supervisor(mono, bus)
    good = sup.register(GoodAgent(services))
    crash = sup.register(CrashAgent(services))
    for i in range(3):
        out = sup.call_many(["trend_analyst", "breakout_analyst"], "analyze", None)
        assert out["trend_analyst"].ok and out["trend_analyst"].value == "ok"
        assert not out["breakout_analyst"].ok and "boom" in out["breakout_analyst"].error
    assert crash.state == AgentState.RESTING and sup.benched_for_errors("breakout_analyst")
    out = sup.call_many(["trend_analyst", "breakout_analyst"], "analyze", None)
    assert out["breakout_analyst"].skipped and out["trend_analyst"].ok
    assert good.state == AgentState.IDLE
    mono.t += 61
    sup.call("breakout_analyst", "analyze", None)
    assert CrashAgent.resets == 1
    topics = [e.topic for e in bus.recent]
    assert "agent.benched" in topics and "agent.restored" in topics
    # Benched again: the backoff doubles.
    for _ in range(2):
        sup.call("breakout_analyst", "analyze", None)
    assert sup.health["breakout_analyst"].benched_until == pytest.approx(mono.t + 120)
    sup.close()


def test_slow_agent_cannot_block_the_loop() -> None:
    mono, bus = Mono(), EventBus()
    sup, services = supervisor(mono, bus)
    sup.register(GoodAgent(services))
    slow = sup.register(SlowAgent(services))
    started = time.monotonic()
    out = sup.call_many(["trend_analyst", "mean_reversion_analyst"], "analyze", None)
    assert time.monotonic() - started < 1.0
    assert out["trend_analyst"].ok and out["mean_reversion_analyst"].slow
    out = sup.call_many(["mean_reversion_analyst"], "analyze", None)
    assert out["mean_reversion_analyst"].busy  # not called again while the stuck call runs
    slow.release.set()
    time.sleep(0.05)
    assert sup.call("mean_reversion_analyst", "analyze", None).ok
    sup.close()


def test_never_benched_agents_show_error_and_stay_callable() -> None:
    mono, bus = Mono(), EventBus()
    sup, services = supervisor(mono, bus)
    guardian = sup.register(CrashGuardian(services))
    for _ in range(10):
        assert not sup.call("guardian", "analyze", None).ok
    assert guardian.state == AgentState.ERROR and not sup.benched_for_errors("guardian")
    sup.close()


# ---------------------------------------------------------------------- opinions


def test_opinion_validation_and_ttl() -> None:
    op = Opinion("trend_analyst", "EURUSD", "M15", "long", 0.6, timestamp=100.0, ttl=900.0)
    assert not op.expired(999.0) and op.expired(1001.0) and op.directional() == 1
    for bad in ({"stance": "buy"}, {"confidence": 1.2}, {"confidence": float("nan")}, {"size_hint": -0.1},
                {"confidence": True}):
        args = {"agent": "x", "symbol": "EURUSD", "timeframe": "M15", "stance": "long", "confidence": 0.5}
        args.update(bad)
        with pytest.raises(ValueError):
            Opinion(**args)


def test_event_envelope_row() -> None:
    event = Event("x", 1.0, seq=3, agent="a", payload={"k": 1})
    assert event.row()["payload"] == {"k": 1}


# ----------------------------------------------------------------- simulation feed


def test_synthetic_market_is_deterministic_and_skips_weekends() -> None:
    a, b = SyntheticMarket(11), SyntheticMarket(11)
    ha, hb = a.history(800), b.history(800)
    for name in ha:
        np.testing.assert_array_equal(ha[name], hb[name])
    times = ha["EURUSD"]["time"]
    weekdays = ((times // 86400) + 3) % 7
    assert (weekdays < 5).all()
    assert np.all(ha["EURUSD"]["high"] >= np.maximum(ha["EURUSD"]["open"], ha["EURUSD"]["close"]))
    assert not np.array_equal(SyntheticMarket(12).history(50)["EURUSD"], ha["EURUSD"][:50])


def test_simulated_terminal_fills_server_side_stops() -> None:
    term = SimulatedTerminal(SyntheticMarket(5), warmup_bars=300)
    tick = term.symbol_info_tick("EURUSD")
    res = term.order_send({"action": term.TRADE_ACTION_DEAL, "symbol": "EURUSD", "volume": 0.1,
                           "type": term.ORDER_TYPE_BUY, "price": tick.ask, "sl": tick.bid - 0.0003,
                           "tp": tick.bid + 0.0003, "magic": 1})
    assert res.retcode == term.TRADE_RETCODE_DONE and len(term.positions) == 1
    for _ in range(200):
        term.advance_bar()
        if not term.positions:
            break
    assert not term.positions
    exits = [d for d in term.deals if d.entry == term.DEAL_ENTRY_OUT]
    assert len(exits) == 1 and exits[0].position_id == res.order
    assert term.account.equity == term.account.balance
