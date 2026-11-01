"""Phase 3: TCA, the calibrated cost model, the cost-to-edge gate and schedule hygiene."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mt5bot import limits
from mt5bot.arms import EntryIntent
from mt5bot.core import costs as c
from mt5bot.core.orders import OrderState
from mt5bot.evidence import Posterior
from mt5bot.execution import ExecReport
from mt5bot.registry import Registry
from mt5bot.strategies.base import Signal
from mt5bot.tca import TcaStore

MON_NOON = 1_767_571_200 + 12 * 3600   # Monday 2026-01-05 12:00 (server time)


@given(st.floats(0.0, 1.0, allow_nan=False), st.floats(1.0, 1000.0), st.floats(0.01, 500.0), st.floats(1.0, 5.0))
def test_gate_is_monotone_in_edge_and_cost(edge: float, loss: float, cost: float, k: float) -> None:
    if c.passes_gate(edge, loss, cost, k):
        assert c.passes_gate(edge + 0.01, loss, cost, k)
        assert c.passes_gate(edge, loss, cost * 0.9, k)


@given(st.floats(-1.0, 0.0), st.floats(1.0, 1000.0), st.floats(0.01, 500.0))
def test_no_proven_edge_never_passes(edge: float, loss: float, cost: float) -> None:
    assert not c.passes_gate(edge, loss, cost, 1.0)


def test_cost_includes_spread_both_way_slippage_commission_and_swap() -> None:
    assert c.all_in_cost_per_lot(10, 2, 0.00001, 100_000, 7.0, 3.0, 2.0) == pytest.approx(14 + 7 + 6)


def test_shrinkage_moves_from_prior_to_data() -> None:
    prior = c.CostPrior(10.0, 2.0)
    obs = c.FillObservation("X", "london", 9, 20.0, 6.0, 90.0)
    assert c.calibrate_cell([], prior).source == "prior"
    few, many = c.calibrate_cell([obs] * 2, prior), c.calibrate_cell([obs] * 2000, prior)
    assert 10.0 < few.spread_points < many.spread_points < 20.0
    assert many.slippage_points == pytest.approx(6.0, abs=0.05) and many.source == "fills"


def test_reject_rate_is_measured() -> None:
    prior = c.CostPrior(10.0, 2.0)
    fills = [c.FillObservation("X", "london", 9, 10.0, 1.0, 90.0)] * 300
    rejects = [c.FillObservation("X", "london", 9, 10.0, 0.0, 0.0, True)] * 100
    assert c.calibrate_cell(fills + rejects, prior).reject_rate == pytest.approx(0.25, abs=0.02)


@pytest.mark.parametrize("minutes,blocked", [(23 * 60 + 49, False), (23 * 60 + 50, True), (5, True), (19, True),
                                             (20, False), (12 * 60, False)])
def test_rollover_guard_wraps_midnight(minutes: int, blocked: bool) -> None:
    assert c.in_rollover(MON_NOON - 12 * 3600 + minutes * 60, c.ScheduleRules()) is blocked


def test_pre_weekend_guard() -> None:
    friday = MON_NOON - 12 * 3600 + 4 * 86400
    rules = c.ScheduleRules()
    assert not c.pre_weekend(friday + 20.9 * 3600, rules) and c.pre_weekend(friday + 21.0 * 3600, rules)
    assert c.pre_weekend(friday + 86400 + 3600, rules)
    assert c.schedule_block(friday + 22 * 3600, rules) == "PRE_WEEKEND"


def test_learned_rollover_can_only_widen() -> None:
    day = MON_NOON - 12 * 3600
    times, spreads = [], []
    for d in range(10):
        for m in range(0, 1440, 10):
            times.append(day + d * 86400 + m * 60)
            spreads.append(60.0 if m < 40 or m >= 1400 else 10.0)
    learned = c.learn_rollover(times, spreads, c.ScheduleRules())
    assert learned.rollover_end_minute == 40 and learned.rollover_start_minute == 1400
    calm = c.learn_rollover(times, [10.0] * len(times), c.ScheduleRules())
    assert calm == c.ScheduleRules()
    assert c.learn_rollover([], [], c.ScheduleRules()) == c.ScheduleRules()


def _report(state: OrderState, fill: float = 1.10012, cid: str = "a" * 12) -> ExecReport:
    return ExecReport(cid, state, state == OrderState.FILLED, "ORDER_OK", "x", 0.1, fill, 1, 1, 2, None, 1, 120.0, 1, 0,
                      0.0001)


META = {"side": "BUY", "point": 0.00001, "decision_price": 1.1001, "sl": 1.0981, "volume": 0.1, "loss_per_lot": 200.0,
        "timeframe": "M15", "arm_id": 3, "tier": "T2", "spread_decision": 10.0}


def test_tca_records_slippage_in_points_and_r(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "x.sqlite", clock=lambda: float(MON_NOON))
    store = TcaStore(reg, lambda: float(MON_NOON))
    store.record("EURUSD", META, _report(OrderState.FILLED), 12)
    row = reg.conn.execute("SELECT * FROM tca").fetchone()
    assert row["slippage_points"] == pytest.approx(2.0)
    assert row["slippage_r"] == pytest.approx(2.0 / 200.0)
    assert row["spread_submit"] == pytest.approx(10.0) and row["latency_ms"] == 120.0 and row["requotes"] == 1
    assert row["session"] == "overlap" and row["hour"] == 12
    reg.close()


def test_rejected_orders_are_part_of_the_record_and_the_model(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "x.sqlite", clock=lambda: float(MON_NOON))
    store = TcaStore(reg, lambda: float(MON_NOON))
    for i in range(30):
        store.record("EURUSD", META, _report(OrderState.FILLED, cid=f"f{i}"), 12)
    for i in range(10):
        store.record("EURUSD", META, _report(OrderState.REJECTED, cid=f"r{i}"), 12)
    assert store.calibrate(2.0) == 1
    cell = store.cell("EURUSD", float(MON_NOON), 12)
    assert cell is not None and cell.n == 30 and 0.05 < cell.reject_rate < 0.25
    assert 1.5 < cell.slippage_points < 2.5
    assert store.sim_lookup()("EURUSD", 12)[0] == pytest.approx(cell.slippage_points)
    assert store.report()["orders"] == 40
    reg.close()


def _post(lo: float) -> Posterior:
    return Posterior(lo + 1.645 * 0.05, 0.05, 0.9, lo, 0.3, 0.1, 0.0, 0.05, 80, 0, 80.0, 0.2, 1.0)


def intent(tier: str, lo: float, exploration: bool = False) -> EntryIntent:
    return EntryIntent(Signal("BUY", 0.0010, 2.0, 3.0), None, "s", "s", arm_id=1, tier=tier, timeframe="M15",
                       exploration=exploration, posterior=_post(lo), risk_pct=0.2, bar_time=1_760_000_400)


def test_gate_blocks_arm_without_edge_over_cost_and_reports_why(make_bot) -> None:
    from conftest import FakeMT5  # noqa: F401

    bot = make_bot()
    bot.startup()
    out = bot._try_open("EURUSD", intent("T2", 0.001), SimpleNamespace(equity=10_000.0, leverage=100, margin=0.0),
                        preview=True)
    assert not out.risk_allowed and out.code == "COST_TO_EDGE" and "cost-to-edge" in out.reason


def test_gate_passes_a_strong_arm(make_bot) -> None:
    bot = make_bot()
    bot.startup()
    out = bot._try_open("EURUSD", intent("T2", 0.5), SimpleNamespace(equity=10_000.0, leverage=100, margin=0.0),
                        preview=True)
    assert out.risk_allowed and out.reason == "ok"


def test_exploration_exemption_is_demo_only_by_default(make_bot) -> None:
    bot = make_bot()
    bot.startup()
    acc = SimpleNamespace(equity=10_000.0, leverage=100, margin=0.0)
    assert bot._try_open("EURUSD", intent("T1", -0.1, True), acc, preview=True).code != "COST_TO_EDGE"
    bot.is_real = True
    assert bot._try_open("EURUSD", intent("T1", -0.1, True), acc, preview=True).code == "COST_TO_EDGE"


def test_the_block_keeps_its_reason_through_the_team_path() -> None:
    """Regression: blocks seen through the team's preview were recorded as OTHER."""
    from mt5bot.funnel import describe, risk_reason_code

    assert risk_reason_code("risk layer: cost-to-edge 0.10 < 2.00: lower edge") == "COST_TO_EDGE"
    assert risk_reason_code("risk layer: projected margin level 90% < 150%") == "MARGIN_GUARD"
    assert risk_reason_code("risk layer: EUR risk 3.00% would exceed 2.00%") == "CURRENCY_CAP"
    assert risk_reason_code(f"risk layer: {describe('ROLLOVER_GUARD')}") == "ROLLOVER_GUARD"
    assert risk_reason_code(f"risk layer: {describe('PRE_WEEKEND')}") == "PRE_WEEKEND"


def test_a_lower_edge_quantile_is_tunable_within_bounds(make_bot, make_cfg) -> None:
    assert make_cfg(costs={"edge_z": 0.67}).costs.edge_z == 0.67
    with pytest.raises(ValueError):
        make_cfg(costs={"edge_z": 0.1})


def test_k_is_bounded_and_the_gate_config_validates(make_cfg) -> None:
    with pytest.raises(ValueError):
        make_cfg(costs={"k": 0.5})
    with pytest.raises(ValueError):
        make_cfg(costs={"rollover_start": "25:00"})
    assert make_cfg().costs.k == 2.0


def test_order_outside_the_rollover_guard_blocks_in_the_pipeline(make_bot, fake_mt5: Any) -> None:
    bot = make_bot()
    bot.startup()
    fake_mt5.ticks["EURUSD"].time = MON_NOON - 12 * 3600 + 300     # 00:05 server
    out = bot._try_open("EURUSD", replace(intent("T2", 0.5)), SimpleNamespace(equity=10_000.0, leverage=100, margin=0.0),
                        preview=True)
    assert out.code == "ROLLOVER_GUARD"
    assert limits.MAX_RISK_PER_TRADE_PCT == 2.0
