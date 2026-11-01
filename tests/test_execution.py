"""Chaos tests of the execution engine. The invariants: one decision never becomes two orders,
an unknown outcome is never treated as "not executed", and no position stays unprotected."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mt5bot.adapters.exchange import SimCosts
from mt5bot.adapters.fake import FakeBroker
from mt5bot.core import orders as oc
from mt5bot.core.breaker import CLOSED, HALF_OPEN, OPEN, BreakerSettings, BrokerBreaker
from mt5bot.core.broker import BrokerReply, OrderRequest
from mt5bot.core.orders import OrderState, Outcome
from mt5bot.eventlog import EventLog
from mt5bot.execution import ExecutionEngine, ExecutionSettings, OrderJournal, project_orders
from mt5bot.registry import Registry


class Clock:
    """Wall clock and monotonic clock are separate: only the wall clock can jump."""

    def __init__(self) -> None:
        self.t = 1_760_000_000.0
        self.mono = 0.0

    def now(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.mono += seconds


class Rig:
    """An engine on a real SQLite journal and the shared matching engine."""

    def __init__(self, path: Path, broker: Any = None, **settings_kw: Any) -> None:
        self.path = path
        self.clock = Clock()
        self.broker = broker or FakeBroker()
        self.settings = ExecutionSettings(**settings_kw)
        self.notes: list[str] = []
        self.boot()

    def boot(self) -> None:
        self.registry = Registry(self.path, clock=self.clock.now)
        self.events = EventLog(self.registry, self.clock.now, lambda: {"code": "test"})
        self.journal = OrderJournal(self.registry, self.events, self.clock.now)
        self.engine = ExecutionEngine(self.broker, self.journal, self.registry, 777, self.settings, self.clock.now,
                                      self.clock.monotonic, self.clock.sleep,
                                      lambda text, key=None: self.notes.append(text))

    def restart(self) -> None:
        self.registry.close()
        self.boot()

    def request(self, cid: str, side: str = "BUY", volume: float = 0.1) -> Any:
        tick = self.broker.tick("EURUSD")
        price = tick.ask if side == "BUY" else tick.bid
        sign = 1 if side == "BUY" else -1
        return OrderRequest("open", "EURUSD", side, volume, price, round(price - sign * 0.002, 5),
                            round(price + sign * 0.003, 5), 10, 777, oc.order_comment(cid), filling=1)

    def send(self, cid: str = "a" * 12, volume: float = 0.1) -> Any:
        return self.engine.open(cid, lambda t: self.request(cid, volume=volume), self.broker.tick("EURUSD"),
                                lambda: self.broker.tick("EURUSD"), lambda r: None, {"symbol": "EURUSD"}, "d1")

    def opens(self) -> list[OrderRequest]:
        return [r for r in self.broker.sent if r.kind == "open"]

    def close_request(self, position: Any) -> Any:
        def build(tick: Any) -> OrderRequest:
            return OrderRequest("close", "EURUSD", "SELL", float(position.volume), tick.bid, magic=777, comment="c",
                                position=int(position.ticket))
        return build


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path / "x.sqlite")


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize("code", [10008, 10009])
def test_both_success_codes_are_success(code: int) -> None:
    assert oc.classify(code) in (Outcome.PLACED, Outcome.FILLED)


@pytest.mark.parametrize("code", [None, 10011, 10012, 10028, 10031, 10039, 12345])
def test_no_proof_means_uncertain_never_failed(code: Any) -> None:
    assert oc.classify(code) == Outcome.UNCERTAIN


@pytest.mark.parametrize("code", [10014, 10016, 10017, 10018, 10019])
def test_permanent_rejections(code: int) -> None:
    assert oc.classify(code) == Outcome.PERMANENT


def test_every_official_code_is_classified_exactly_once() -> None:
    groups = [oc.TRANSIENT_RETCODES, oc.UNCERTAIN_RETCODES, oc.PERMANENT_RETCODES,
              frozenset({10008, 10009, 10010, 10025, 10036})]
    assert sum(len(g) for g in groups) == len(frozenset().union(*groups))
    assert frozenset().union(*groups) == frozenset(oc.RETCODE_NAMES)
    assert len(oc.RETCODE_NAMES) == 41


def test_uncertain_codes_cannot_be_configured_as_retryable() -> None:
    with pytest.raises(ValueError):
        oc.validate_retryable([10012])
    assert oc.validate_retryable([10004]) == frozenset({10004})


# ------------------------------------------------------------------- state machine


@given(st.lists(st.sampled_from(list(OrderState)), max_size=12))
def test_state_machine_is_closed_and_terminals_absorb(path: list[OrderState]) -> None:
    current = OrderState.INTENT
    for target in path:
        if oc.can_transition(current, target):
            current = oc.advance(current, target)
        else:
            with pytest.raises(oc.IllegalTransition):
                oc.advance(current, target)
    if current in oc.TERMINAL:
        assert not oc.TRANSITIONS[current]


def test_uncertain_can_never_go_back_to_submitted() -> None:
    assert not oc.can_transition(OrderState.UNCERTAIN, OrderState.SUBMITTED)
    assert not oc.can_transition(OrderState.ACKED, OrderState.SUBMITTED)


@given(st.lists(st.text(max_size=8), min_size=1, max_size=6))
def test_client_id_is_deterministic_and_fits_the_comment(parts: list[str]) -> None:
    cid = oc.client_id(*parts)
    assert cid == oc.client_id(*parts) and oc.parse_comment(oc.order_comment(cid)) == cid
    assert len(oc.order_comment(cid)) <= 31


# ------------------------------------------------------------------------ happy path


def test_fill_is_written_ahead_and_protected(rig: Rig) -> None:
    report = rig.send()
    assert report.ok and report.state == OrderState.FILLED and len(rig.opens()) == 1
    kinds = [e.kind for e in rig.events.stream("a" * 12)]
    assert kinds[:3] == ["order.intent", "order.submitted", "order.filled"]


def test_projection_rebuilds_from_the_ledger(rig: Rig) -> None:
    rig.send()
    rig.broker.chaos.append("timeout_no_fill")
    rig.send("b" * 12)
    built = project_orders(rig.registry.conn)
    for cid in ("a" * 12, "b" * 12):
        assert built[cid]["state"] == rig.journal.get(cid).state.value


def test_ledger_is_immutable(rig: Rig) -> None:
    rig.send()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        rig.registry.conn.execute("UPDATE ledger SET kind = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        rig.registry.conn.execute("DELETE FROM ledger")


# --------------------------------------------------------------------------- chaos


@pytest.mark.parametrize("script", ["timeout_after_fill", "none_after_fill", "exception_after_fill"])
def test_answer_lost_after_the_broker_filled_does_not_duplicate(rig: Rig, script: str) -> None:
    rig.broker.chaos.append(script)
    report = rig.send()
    assert report.ok and report.state == OrderState.FILLED
    assert len(rig.opens()) == 1 and len(rig.broker.positions()) == 1


def test_late_confirmation_is_found_by_polling(rig: Rig) -> None:
    rig.broker.chaos.append(("late_fill", 3))
    report = rig.send()
    assert report.ok and len(rig.opens()) == 1 and len(rig.broker.positions()) == 1


def test_very_late_fill_after_the_polls_is_found_by_reconciliation(rig: Rig) -> None:
    rig.broker.chaos.append(("late_fill", 50))
    report = rig.send()
    assert not report.ok and report.code == "ORDER_UNCERTAIN" and report.state == OrderState.UNCERTAIN
    seen: list[Any] = []
    for _ in range(50):
        rig.engine.reconcile(seen.append)
    assert len(seen) == 1 and rig.journal.get("a" * 12).state == OrderState.FILLED
    assert len(rig.opens()) == 1


def test_uncertain_blocks_new_orders_on_the_symbol(rig: Rig) -> None:
    rig.broker.chaos.append("timeout_no_fill")
    assert rig.send().code == "ORDER_UNCERTAIN"
    second = rig.send("b" * 12)
    assert second.code == "ORDER_UNCERTAIN" and len(rig.opens()) == 1


def test_timeout_without_fill_resolves_only_after_the_grace_period(rig: Rig) -> None:
    rig.broker.chaos.append("timeout_no_fill")
    rig.send()
    rig.engine.reconcile()
    assert rig.journal.get("a" * 12).state == OrderState.UNCERTAIN
    rig.clock.sleep(rig.settings.uncertain_grace_seconds + 1)
    rig.engine.reconcile()
    assert rig.journal.get("a" * 12).state == OrderState.CANCELLED
    assert len(rig.opens()) == 1          # proved dead, and an entry signal is never re-sent
    assert rig.send("b" * 12).ok          # the symbol is free again


def test_unanswered_queries_keep_the_order_uncertain_however_long_it_takes(rig: Rig) -> None:
    rig.broker.chaos.append(("blind", 1000))
    report = rig.send()
    assert report.state == OrderState.UNCERTAIN
    rig.clock.sleep(10 * rig.settings.uncertain_grace_seconds)
    rig.engine.reconcile()
    assert rig.journal.get("a" * 12).state == OrderState.UNCERTAIN  # "unknown" is never read as "absent"


def test_same_decision_after_a_restart_is_blocked(rig: Rig) -> None:
    assert rig.send().ok
    rig.restart()
    again = rig.send()
    assert again.code == "DUPLICATE_BLOCKED" and len(rig.opens()) == 1
    assert rig.engine.duplicates_blocked == 1


def test_terminal_killed_between_write_ahead_and_send(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "x.sqlite")

    class Dies(Exception):
        pass

    def die(request: OrderRequest) -> BrokerReply:
        raise Dies

    original = rig.broker.send
    rig.broker.send = die  # type: ignore[method-assign]
    with pytest.raises(Dies):
        rig.send()
    rig.broker.send = original  # type: ignore[method-assign]
    assert rig.journal.get("a" * 12).state == OrderState.SUBMITTED
    rig.restart()
    assert rig.engine.reconcile_startup() == {"cancelled": 0, "uncertain": 1}
    assert rig.journal.get("a" * 12).state == OrderState.UNCERTAIN
    assert rig.send("b" * 12).code == "ORDER_UNCERTAIN"


def test_killed_after_intent_only_is_cancelled_as_never_sent(rig: Rig) -> None:
    rec = rig.journal.create(oc_record("c" * 12))
    assert rec.state == OrderState.INTENT
    rig.restart()
    assert rig.engine.reconcile_startup() == {"cancelled": 1, "uncertain": 0}


def oc_record(cid: str) -> Any:
    from mt5bot.execution import OrderRecord

    return OrderRecord(cid, "open", OrderState.INTENT, "EURUSD", "BUY", 0.1)


def test_order_filled_during_the_crash_is_recovered_after_restart(rig: Rig) -> None:
    rig.broker.chaos.append("none_after_fill")
    original_locate = rig.engine.locate
    rig.engine.locate = lambda rec: __import__("mt5bot.execution").execution.Found("unknown")  # type: ignore
    rig.send()
    rig.engine.locate = original_locate
    rig.restart()
    seen: list[Any] = []
    rig.engine.reconcile(seen.append)
    assert len(seen) == 1 and seen[0].state == OrderState.FILLED and len(rig.opens()) == 1


def test_partial_fill_records_the_real_volume(rig: Rig) -> None:
    rig.broker.chaos.append(("partial", 0.5))
    report = rig.send(volume=0.2)
    assert report.ok and report.state == OrderState.PARTIALLY_FILLED
    assert report.filled_volume == pytest.approx(0.1) and len(rig.opens()) == 1


def test_placed_10008_waits_for_the_fill(rig: Rig) -> None:
    rig.broker.chaos.append("placed")
    report = rig.send()
    assert report.ok and report.state == OrderState.FILLED and report.position_ticket


def test_transient_retry_reconciles_first_and_uses_backoff(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "x.sqlite", max_attempts=3)
    rig.broker.chaos.extend([("reject", 10004), ("reject", 10020)])
    start = rig.clock.t
    report = rig.send()
    assert report.ok and report.requotes == 2 and len(rig.opens()) == 3
    assert rig.clock.t - start >= 1.5   # 0.5 + 1.0


def test_requote_that_actually_filled_is_not_sent_again(rig: Rig) -> None:
    """Defence in depth: even a "definite" rejection is checked before a resubmission."""
    rig.broker.chaos.append(("reject", 10004))
    cid = "a" * 12
    original = rig.engine.locate
    calls: list[int] = []

    def locate(rec: Any) -> Any:
        if not calls:
            calls.append(1)
            rig.broker._fill_open(rig.request(cid), 0.1)
        return original(rec)

    rig.engine.locate = locate  # type: ignore[method-assign]
    report = rig.send(cid)
    assert report.ok and len(rig.opens()) == 1 and len(rig.broker.positions()) == 1


@pytest.mark.parametrize("code", [10019, 10016, 10014, 10017, 10018])
def test_permanent_rejection_is_never_retried_and_is_not_an_outage(rig: Rig, code: int) -> None:
    for i in range(10):
        rig.broker.chaos.append(("reject", code))
        report = rig.send(f"{i:012x}")
        assert report.state == OrderState.REJECTED
    assert len(rig.opens()) == 10 and rig.engine.breaker.state == CLOSED


def test_broker_ignoring_stops_gets_them_attached(rig: Rig) -> None:
    rig.broker.chaos.append("ignore_stops")
    report = rig.send()
    pos = rig.broker.positions()[0]
    assert pos.sl == 0.0
    rec = rig.journal.get(report.client_id)
    req = report.request
    result = rig.engine.verify_protection(rec, pos.ticket, req.sl, req.tp, 0.00001, lambda p, r: False)
    assert result == "attached" and pos.sl == req.sl and pos.tp == req.tp
    assert rig.notes and rig.engine.unprotected_events == 1


def test_stops_that_cannot_be_attached_close_the_position(rig: Rig) -> None:
    rig.broker.chaos.append("ignore_stops")
    report = rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.extend(["reject_stops"] * 3)
    closed: list[Any] = []

    def close(position: Any, reason: str) -> bool:
        closed.append(reason)
        rig.broker._close(position, rig.broker.tick("EURUSD").bid)
        return True

    rec = rig.journal.get(report.client_id)
    result = rig.engine.verify_protection(rec, pos.ticket, report.request.sl, report.request.tp, 0.00001, close)
    assert result == "closed" and closed and not rig.broker.positions()


def test_unprotected_window_is_bounded_in_seconds(rig: Rig) -> None:
    rig.broker.chaos.append("ignore_stops")
    report = rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.extend(["reject_stops"] * 3)
    start = rig.clock.t
    rec = rig.journal.get(report.client_id)
    rig.engine.verify_protection(rec, pos.ticket, report.request.sl, report.request.tp, 0.00001, lambda p, r: True)
    assert rig.clock.t - start <= 5.0


def test_close_after_timeout_is_not_repeated(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.append("timeout_after_fill")
    report = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                              lambda: rig.broker.tick("EURUSD"), "test")
    assert report.ok and not rig.broker.positions()
    assert len([r for r in rig.broker.sent if r.kind == "close"]) == 1


def test_close_is_allowed_while_the_breaker_is_open(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    rig.engine.breaker.state = OPEN
    rig.engine.breaker.opened_at = rig.clock.t
    report = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                              lambda: rig.broker.tick("EURUSD"), "test")
    assert report.ok and not rig.broker.positions()


def test_netting_repeat_close_cannot_open_a_reverse_position(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "x.sqlite", broker=FakeBroker(hedging=False))
    rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.append(("reject", 10004))

    def build(tick: Any) -> OrderRequest:
        return OrderRequest("close", "EURUSD", "SELL", 0.1, tick.bid, magic=777, comment="c")

    # A requote that in truth closed the position: the re-read stops the second send.
    original = rig.broker._send_close

    def sneaky(request: OrderRequest) -> BrokerReply:
        original(request)
        return BrokerReply(10004, comment="requote")

    rig.broker._send_close = sneaky  # type: ignore[method-assign]
    rig.broker.chaos.clear()
    report = rig.engine.close(pos, 0.1, build, rig.broker.tick("EURUSD"), lambda: rig.broker.tick("EURUSD"), "t")
    assert report.ok and rig.broker.positions() == ()
    assert len([r for r in rig.broker.sent if r.kind == "close"]) == 1


def test_disk_full_stops_the_order_before_it_is_sent(rig: Rig) -> None:
    def full(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database or disk is full")

    rig.journal.create = full  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        rig.send()
    assert rig.opens() == []     # no write-ahead record, no order


def test_database_locked_stops_the_order_before_it_is_sent(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "x.sqlite")
    other = sqlite3.connect(tmp_path / "x.sqlite", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    rig.registry.raw.execute("PRAGMA busy_timeout=50")
    with pytest.raises(sqlite3.OperationalError):
        rig.send()
    other.execute("ROLLBACK")
    assert rig.opens() == []


def test_clock_jump_does_not_resolve_an_order_whose_fill_exists(rig: Rig) -> None:
    rig.broker.chaos.append(("late_fill", 12))
    rig.send()
    rig.clock.t += 10 * 86400       # the PC clock jumps: elapsed "time" alone proves nothing
    rig.engine.reconcile()
    assert rig.journal.get("a" * 12).state == OrderState.UNCERTAIN
    for _ in range(12):
        rig.engine.reconcile()
    assert rig.journal.get("a" * 12).state == OrderState.FILLED
    assert len(rig.opens()) == 1


# ------------------------------------------------------------------------- breaker


def test_breaker_opens_after_infrastructure_failures_and_persists(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "x.sqlite", breaker=BreakerSettings(2, 60.0, 1), uncertain_grace_seconds=10)
    for i in range(2):
        rig.broker.chaos.append("timeout_no_fill")
        rig.send(f"{i:012x}")
        rig.engine.reconcile()
        rig.clock.sleep(11)
        rig.engine.reconcile()
    assert rig.engine.breaker.state == OPEN
    assert rig.send("c" * 12).code == "BROKER_BREAKER_OPEN"
    rig.restart()
    assert rig.engine.breaker.state == OPEN
    assert rig.send("d" * 12).code == "BROKER_BREAKER_OPEN"
    rig.clock.sleep(61)
    assert rig.send("e" * 12).ok and rig.engine.breaker.state == CLOSED


def test_half_open_allows_limited_probes() -> None:
    s = BreakerSettings(1, 10.0, 1)
    b = BrokerBreaker()
    assert b.on_failure(0.0, s) and not b.allow(5.0, s)
    assert b.allow(11.0, s) and b.state == HALF_OPEN and not b.allow(11.0, s)
    assert b.on_failure(12.0, s) and b.state == OPEN


def test_corrupt_breaker_state_fails_closed() -> None:
    assert BrokerBreaker.from_json("{not json").state == OPEN
    assert BrokerBreaker.from_json(json.dumps({"state": "?"})).state == OPEN


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.sampled_from(["ok", "timeout_after_fill", "none_after_fill", "timeout_no_fill", "partial",
                                 "ignore_stops", "requote", "reject", "placed"]), min_size=1, max_size=8))
def test_no_chaos_sequence_ever_duplicates_a_decision(tmp_path_factory: Any, script: list[str]) -> None:
    rig = Rig(tmp_path_factory.mktemp("c") / "x.sqlite")
    table = {"partial": ("partial", 0.5), "requote": ("reject", 10004), "reject": ("reject", 10019)}
    for i, step in enumerate(script):
        if step != "ok":
            rig.broker.chaos.append(table.get(step, step))
        cid = f"{i:012x}"
        rig.send(cid)
        rig.send(cid)                      # the same decision again must never reach the broker
        rig.engine.reconcile()
        rig.clock.sleep(rig.settings.uncertain_grace_seconds + 1)
        rig.engine.reconcile()
        # One entry per decision is the invariant, however the broker misbehaved.
        same = [r for r in rig.opens() if r.comment == oc.order_comment(cid)]
        assert len(same) <= rig.settings.max_attempts
        filled = [p for p in rig.broker.positions_book if p.comment == oc.order_comment(cid)]
        assert len(filled) <= 1
        for p in list(rig.broker.positions_book):
            rig.broker._close(p, rig.broker.tick("EURUSD").bid)
    rig.registry.close()
    assert SimCosts().commission_per_lot_side > 0


# -------------------------------------------------- what reconciliation can still find


def test_a_fill_already_stopped_out_is_found_through_its_deals(rig: Rig) -> None:
    """The position is gone by the time we look: the deal is the only proof it ever existed."""
    rig.broker.chaos.append("none_after_fill")
    original = rig.engine.locate
    rig.engine.locate = lambda rec: rig.engine.__class__.locate(rig.engine, rec)  # type: ignore[method-assign]
    send = rig.broker.send

    def filled_then_stopped(request: OrderRequest) -> BrokerReply:
        reply = send(request)
        for p in list(rig.broker.positions_book):
            rig.broker._close(p, p.sl)
        return reply

    rig.broker.send = filled_then_stopped  # type: ignore[method-assign]
    report = rig.send()
    assert report.ok and report.state == OrderState.FILLED and rig.broker.positions() == ()
    assert original is not None


def test_a_pending_order_keeps_the_outcome_open(rig: Rig) -> None:
    from types import SimpleNamespace

    rig.broker.chaos.append("timeout_no_fill")
    cid = "a" * 12
    rig.broker.orders_book.append(SimpleNamespace(ticket=55, magic=777, comment=oc.order_comment(cid)))
    assert rig.send(cid).state == OrderState.UNCERTAIN
    rig.clock.sleep(10 * rig.settings.uncertain_grace_seconds)
    rig.engine.reconcile()
    assert rig.journal.get(cid).state == OrderState.UNCERTAIN    # a live order is not "nothing at the broker"


def test_a_retry_without_a_fresh_price_is_rejected_not_guessed(rig: Rig) -> None:
    rig.broker.chaos.append(("reject", 10004))
    cid = "a" * 12
    report = rig.engine.open(cid, lambda t: rig.request(cid), rig.broker.tick("EURUSD"), lambda: None, lambda r: None,
                             {"symbol": "EURUSD"}, "d")
    assert report.state == OrderState.REJECTED and len(rig.opens()) == 1


def test_a_retry_that_fails_the_risk_layer_never_reaches_the_broker(rig: Rig) -> None:
    rig.broker.chaos.append(("reject", 10004))
    cid = "a" * 12
    calls: list[int] = []

    def validate(request: OrderRequest) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise ValueError("SL distance differs from the risk plan")

    report = rig.engine.open(cid, lambda t: rig.request(cid), rig.broker.tick("EURUSD"),
                             lambda: rig.broker.tick("EURUSD"), validate, {"symbol": "EURUSD"}, "d")
    assert report.state == OrderState.REJECTED and len(rig.opens()) == 1 and "risk plan" in report.reason


def test_position_ticket_comes_from_the_broker_not_from_the_order(rig: Rig) -> None:
    send = rig.broker.send

    def other_order(request: OrderRequest) -> BrokerReply:
        reply = send(request)
        for p in rig.broker.positions_book:
            p.comment = "rewritten by the broker"
        return BrokerReply(reply.retcode, 999_999, reply.deal, reply.volume, reply.price, reply.comment)

    rig.broker.send = other_order  # type: ignore[method-assign]
    report = rig.send()
    assert report.position_ticket == rig.broker.positions()[0].ticket != report.ticket


def test_stops_the_broker_moved_are_accepted_not_fought(rig: Rig) -> None:
    report = rig.send()
    pos = rig.broker.positions()[0]
    pos.sl -= 0.0005                            # moved by the broker, still protective
    rec = rig.journal.get(report.client_id)
    assert rig.engine.verify_protection(rec, pos.ticket, report.request.sl, report.request.tp, 0.00001,
                                        lambda p, r: False) == "ok"
    assert len([r for r in rig.broker.sent if r.kind == "sltp"]) == 0


def test_a_position_closed_at_once_needs_no_protection(rig: Rig) -> None:
    report = rig.send()
    pos = rig.broker.positions()[0]
    rig.broker._close(pos, pos.sl)
    rec = rig.journal.get(report.client_id)
    assert rig.engine.verify_protection(rec, pos.ticket, 1.0, 2.0, 0.00001, lambda p, r: False) == "ok"


def test_stops_attached_on_the_last_attempt_still_count(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "x.sqlite", protect_attempts=1)
    rig.broker.chaos.append("ignore_stops")
    report = rig.send()
    pos = rig.broker.positions()[0]
    rec = rig.journal.get(report.client_id)
    result = rig.engine.verify_protection(rec, pos.ticket, report.request.sl, report.request.tp, 0.00001,
                                          lambda p, r: False)
    assert result == "attached" and pos.sl and rig.engine.unprotected_events == 1


def test_an_uncertain_stop_change_is_settled_by_reading_the_position(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    send = rig.broker.send

    def lost_answer(request: OrderRequest) -> BrokerReply:
        send(request)
        return BrokerReply(None, error="connection lost")

    rig.broker.send = lost_answer  # type: ignore[method-assign]
    assert rig.engine.modify_stops(pos, 1.09, 1.12, "t") is True
    rig.broker.send = lambda request: BrokerReply(None, error="connection lost")  # type: ignore[method-assign]
    assert rig.engine.modify_stops(pos, 1.08, 1.13, "t") is False


def test_a_permanent_stop_rejection_is_reported_as_failed(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.append("reject_stops")
    assert rig.engine.modify_stops(pos, 1.09, 1.12, "t") is False


def test_an_unresolved_close_blocks_a_second_close_until_it_settles(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.append("timeout_no_fill")
    first = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                             lambda: rig.broker.tick("EURUSD"), "t")
    assert first.code == "ORDER_UNCERTAIN"
    second = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                              lambda: rig.broker.tick("EURUSD"), "t")
    assert second.code == "ORDER_UNCERTAIN"
    assert len([r for r in rig.broker.sent if r.kind == "close"]) == 1
    rig.clock.sleep(rig.settings.uncertain_grace_seconds + 1)
    third = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                             lambda: rig.broker.tick("EURUSD"), "t")
    assert third.ok and rig.broker.positions() == ()       # proved it did not close, so closing again is safe


def test_a_close_that_landed_after_all_is_recognised(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.append("timeout_no_fill")
    rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"), lambda: rig.broker.tick("EURUSD"), "t")
    rig.broker._close(pos, rig.broker.tick("EURUSD").bid)        # the server did act
    again = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                             lambda: rig.broker.tick("EURUSD"), "t")
    assert again.ok and again.reason == "closed (reconciled)"


def test_close_placed_is_only_success_once_the_position_is_gone(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    send_close = rig.broker._send_close

    def placed(request: OrderRequest) -> BrokerReply:
        reply = send_close(request)
        return BrokerReply(10008, reply.order, 0, 0.0, 0.0, "placed")

    rig.broker._send_close = placed  # type: ignore[method-assign]
    report = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                              lambda: rig.broker.tick("EURUSD"), "t")
    assert report.ok and report.state == OrderState.FILLED


def test_closing_a_position_the_broker_no_longer_has_is_success(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    rig.broker._close(pos, rig.broker.tick("EURUSD").bid)
    report = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                              lambda: rig.broker.tick("EURUSD"), "t")
    assert report.ok and report.state == OrderState.FILLED


def test_a_close_with_a_clock_jump_is_not_proved_failed(rig: Rig) -> None:
    rig.send()
    pos = rig.broker.positions()[0]
    rig.broker.chaos.append("timeout_no_fill")
    report = rig.engine.close(pos, 0.1, rig.close_request(pos), rig.broker.tick("EURUSD"),
                              lambda: rig.broker.tick("EURUSD"), "t")
    rig.clock.t += 10 * 86400
    rig.engine.reconcile()
    assert rig.journal.get(report.client_id).state == OrderState.UNCERTAIN


def test_remove_pending(rig: Rig) -> None:
    from types import SimpleNamespace

    rig.broker.orders_book.append(SimpleNamespace(ticket=5, magic=777, comment="x"))
    assert rig.engine.remove_pending(SimpleNamespace(ticket=5))
    assert rig.broker.orders_book == []
