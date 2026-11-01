"""Any balance: the sweep from far below the floor to far beyond the ceiling, granularity-aware
sizing, trade-count budgets, modes, cash flows and the owner's root-cause regression."""

from __future__ import annotations

import ast
import itertools
import math
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from conftest import FakeMT5, make_rates
from mt5bot import feasibility as fz
from mt5bot import limits
from mt5bot.arms import EntryIntent
from mt5bot.capital import anti_martingale_money
from mt5bot.config import Config
from mt5bot.profiles import PROFILES, effective_limits
from mt5bot.risk import EntryContext, RiskManager, SymbolSpec, TradePlan, lots_for_risk, plan_trade
from mt5bot.strategies.base import Signal
from mt5bot.tiers import exploration_room, server_day_start

PROJECT = Path(__file__).resolve().parent.parent

# Fake specs with realistic minimums, steps, maximums, contract sizes and tick values.
SPECS = {
    "EURUSD": (SymbolSpec("EURUSD", 0.00001, 5, 1.0, 0.00001, 100_000, 0.01, 100.0, 0.01), 1.16, 0.0008, 3.0),
    "GBPUSD": (SymbolSpec("GBPUSD", 0.00001, 5, 1.0, 0.00001, 100_000, 0.01, 50.0, 0.01), 1.33, 0.0010, 4.0),
    "USDJPY": (SymbolSpec("USDJPY", 0.001, 3, 0.67, 0.001, 100_000, 0.01, 100.0, 0.01), 150.0, 0.12, 3.0),
    "BTCUSD": (SymbolSpec("BTCUSD", 0.01, 2, 0.01, 0.01, 1, 0.01, 10.0, 0.01), 65_000.0, 120.0, 0.0),
    "XAUUSD": (SymbolSpec("XAUUSD", 0.01, 2, 1.0, 0.01, 100, 0.01, 50.0, 0.01), 4_000.0, 6.0, 7.0),
    "US30": (SymbolSpec("US30", 1.0, 1, 1.0, 1.0, 1, 0.1, 50.0, 0.1), 42_000.0, 80.0, 0.0),
    "COARSE": (SymbolSpec("COARSE", 0.01, 2, 1.0, 0.01, 100, 1.0, 1000.0, 1.0), 100.0, 1.0, 0.0),
}
SWEEP = np.logspace(0, 12, 200)
# A currency whose balances run in the millions: every money value scales, percentages do not.
CURRENCIES = {"USD": 1.0, "EUR": 0.92, "JPY": 150.0, "AOA": 900.0, "UZS": 12_500.0}
PROFILE_NAMES = tuple(PROFILES)


def candidate(name: str, fx: float = 1.0) -> tuple[fz.Candidate, SymbolSpec]:
    spec, price, stop, commission = SPECS[name]
    loss = (spec.money(stop, 1.0) + commission) * fx
    notional = spec.contract_size * price * fx
    return fz.Candidate(name, "M15", "atr_1.5", stop, loss, spec.volume_min, spec.volume_step, spec.volume_max,
                        margin_per_lot=notional / 100.0, notional_per_lot=notional, cost_per_lot=loss * 0.05), spec


def rules(profile: str = "aggressive") -> fz.FeasibilityRules:
    lim = effective_limits(Config(risk_profile=profile), True)
    return fz.FeasibilityRules(lim.min_viable_cap_pct, lim.t1_risk_pct, 150.0)


def size(c: fz.Candidate, spec: SymbolSpec, equity: float, risk_pct: float, cap_pct: float,
         capacity_lots: float) -> Any:
    def loss_fn(side: str, entry: float, sl: float, volume: float) -> float:
        return c.loss_per_lot

    price = SPECS[c.symbol][1]
    atr = c.stop_distance / 2.0
    return plan_trade("BUY", atr, 2.0, 3.0, price, 0.0, spec, equity, risk_pct, 0.0, 0.0, loss_fn,
                      min_lot_cap_pct=cap_pct, max_volume=max(capacity_lots, spec.volume_min))


class TestBalanceSweep:
    @pytest.mark.parametrize("currency", list(CURRENCIES))
    @pytest.mark.parametrize("symbol", list(SPECS))
    def test_properties_at_every_magnitude(self, symbol: str, currency: str) -> None:
        fx = CURRENCIES[currency]
        c, spec = candidate(symbol, fx)
        r = rules()
        capacity = fz.capacity_lots(spec.volume_max, limits.MAX_SPLIT_ORDERS)
        was_feasible = False
        for equity in SWEEP * fx:
            row = fz.evaluate(c, float(equity), r)
            assert row.feasible or row.reason
            if was_feasible:
                assert row.feasible, f"{symbol}: more equity shrank the feasible set at {equity}"
            was_feasible = row.feasible
            result = size(c, spec, float(equity), r.risk_target_pct, r.min_viable_cap_pct, capacity.lots)
            if not row.feasible:
                # Below the floor the plan may only exist if the cost was the reason.
                assert result.plan is None or row.reason == "COST_TO_EDGE" or row.reason in ("MARGIN_GUARD", "NOTIONAL_CAP")
                continue
            plan = result.plan
            assert plan is not None
            assert math.isfinite(plan.volume) and spec.volume_min <= plan.volume <= capacity.lots + 1e-9
            steps = plan.volume / spec.volume_step
            assert abs(steps - round(steps)) < 1e-6
            assert plan.planned_risk <= equity * limits.MAX_RISK_PER_TRADE_PCT / 100.0 * (1 + 1e-9)
            children, _left = fz.plan_slices(plan.volume, capacity.lots, spec.volume_min, spec.volume_max,
                                             spec.volume_step, limits.MAX_SPLIT_ORDERS)
            assert children and len(children) <= limits.MAX_SPLIT_ORDERS
            assert all(spec.volume_min <= v <= spec.volume_max for v in children)
            assert sum(children) <= plan.volume + 1e-9

    @pytest.mark.parametrize("symbol", list(SPECS))
    def test_there_is_a_floor_and_a_ceiling_and_both_come_from_the_specs(self, symbol: str) -> None:
        c, spec = candidate(symbol)
        r = rules()
        row = fz.evaluate(c, 1e12, r)
        assert row.feasible and not fz.evaluate(c, row.floor_equity * 0.99, r).feasible
        assert fz.evaluate(c, row.floor_equity * 1.0001, r).feasible
        lots = fz.capacity_lots(spec.volume_max, limits.MAX_SPLIT_ORDERS).lots
        ceiling = fz.capacity_ceiling(lots, c.loss_per_lot, r.risk_target_pct)
        assert ceiling is not None and fz.undeployed(ceiling * 3, ceiling) == pytest.approx(ceiling * 2)
        assert fz.undeployed(ceiling / 2, ceiling) == 0.0

    def test_above_the_ceiling_orders_are_sliced_and_capacity_holds(self) -> None:
        c, spec = candidate("US30")
        cap = fz.capacity_lots(spec.volume_max, limits.MAX_SPLIT_ORDERS)
        plan = size(c, spec, 1e12, 1.0, 2.0, cap.lots).plan
        assert plan.volume == pytest.approx(cap.lots)
        children, _ = fz.plan_slices(plan.volume, cap.lots, spec.volume_min, spec.volume_max, spec.volume_step, 4)
        assert children == [50.0] * 4

    @pytest.mark.parametrize("equity", [0.0, -5.0, math.nan, math.inf, -math.inf])
    def test_bad_equity_never_raises_or_trades(self, equity: float) -> None:
        row = fz.evaluate(candidate("EURUSD")[0], equity, rules())
        assert not row.feasible and row.reason == "NO_EQUITY"
        assert "Capital zero" in fz.below_floor_message(equity, None, "USD", {})

    def test_loss_unknown_is_infeasible(self) -> None:
        c, _ = candidate("EURUSD")
        bad = fz.Candidate(c.symbol, c.timeframe, c.stop_policy, c.stop_distance, 0.0, c.volume_min, c.volume_step,
                           c.volume_max)
        assert fz.evaluate(bad, 1000.0, rules()).reason == "LOSS_PER_LOT_UNKNOWN"


class TestVolumeArithmetic:
    @pytest.mark.parametrize("step", [0.01, 0.1, 1.0, 0.001])
    def test_exact_multiples_of_the_step(self, step: float) -> None:
        for raw in (0.3, 0.7, 1.1, 2.3, 17.77):
            v = fz.clamp_volume(raw, step, 1000.0, step)
            assert v <= raw and v == pytest.approx(round(v / step) * step)

    def test_no_binary_artefacts_or_scientific_notation(self) -> None:
        assert fz.volume_from_steps(30, 0.01) == 0.3 and fz.format_volume(0.00001, 0.00001) == "0.00001"
        assert "e" not in fz.format_volume(1e-5, 1e-5) and "e" not in fz.format_volume(1e9, 0.01)

    def test_never_rounds_up_into_more_risk(self) -> None:
        assert lots_for_risk(0.09, 10.0, 0.01, 100.0, 0.01) == 0.0
        assert fz.clamp_volume(0.0099, 0.01, 100.0, 0.01) == 0.0

    def test_slices_never_exceed_the_volume(self) -> None:
        for volume, cap in itertools.product((0.05, 0.23, 7.77, 4000.0), (0.01, 1.0, 1e9)):
            children, left = fz.plan_slices(volume, cap, 0.01, 2.0, 0.01, 4)
            assert sum(children) <= min(volume, cap) + 1e-9 and left >= 0.0


class TestNoFixedMoney:
    def test_no_balance_band_survives_in_the_code(self) -> None:
        text = "\n".join(p.read_text(encoding="utf-8") for p in (PROJECT / "mt5bot").rglob("*.py"))
        for name in ("LARGE_ACCOUNT_REF", "small_account_equity", "large_account_factor", "min_balance", "max_balance"):
            assert name not in text.replace('"small_account_equity"', "").replace("'tail.small_account_equity'", "")

    @pytest.mark.parametrize("module", ["feasibility.py", "bot_feasibility.py", "profiles.py"])
    def test_decision_modules_compare_no_money_literal(self, module: str) -> None:
        """No comparison of equity, balance or capital against a numeric literal in decision code."""
        tree = ast.parse((PROJECT / "mt5bot" / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            numbers = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
                       and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)]
            if names & {"equity", "balance", "capital"}:
                assert all(v == 0 for v in numbers), ast.dump(node)


class TestModes:
    def test_hysteresis_prevents_flipping(self) -> None:
        t = fz.ModeTracker(0.1)
        assert t.update(True, 0.95, 0.0) == fz.MODE_MIN_VIABLE
        assert t.update(True, 1.05, 0.0) == fz.MODE_MIN_VIABLE     # inside the band: no flip
        assert t.update(True, 1.2, 0.0) == fz.MODE_PERCENT
        assert t.update(True, 0.95, 0.0) == fz.MODE_PERCENT
        assert t.update(True, 0.8, 0.0) == fz.MODE_MIN_VIABLE

    def test_capacity_and_below_floor(self) -> None:
        t = fz.ModeTracker(0.1)
        assert t.update(True, 5.0, 1.3) == fz.MODE_CAPACITY
        assert t.update(True, 5.0, 0.95) == fz.MODE_CAPACITY
        assert t.update(True, 5.0, 0.5) == fz.MODE_PERCENT
        assert t.update(False, None, None) == fz.MODE_BELOW_FLOOR

    def test_labels_come_from_ratios(self) -> None:
        assert [fz.size_label(h, 0.0) for h in (0.1, 0.5, 2.0, 10.0, 100.0)] == ["Nano", "Micro", "Small", "Standard", "Large"]
        assert fz.size_label(5.0, 0.7) == "Institutional-scale"


class TestProfiles:
    @pytest.mark.parametrize("name,t1,t2,t3,cap,daily,weekly,trades", [
        ("conservative", 0.10, 0.25, 0.50, 0.75, 2.5, 5.0, 2), ("balanced", 0.25, 0.50, 1.00, 1.25, 3.5, 7.0, 3),
        ("aggressive", 0.50, 1.00, 2.00, 2.00, 5.0, 10.0, 4)])
    def test_documented_numbers(self, name: str, t1: float, t2: float, t3: float, cap: float, daily: float, weekly: float,
                                trades: int) -> None:
        lim = effective_limits(Config(risk_profile=name), True)
        assert (lim.t1_risk_pct, lim.t2_risk_pct, lim.t3_risk_pct, lim.min_viable_cap_pct) == (t1, t2, t3, cap)
        assert (lim.daily_loss_pct, lim.weekly_loss_pct, lim.explore_trades_day) == (daily, weekly, trades)

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    @pytest.mark.parametrize("real", [True, False])
    def test_nothing_exceeds_the_ceilings(self, name: str, real: bool) -> None:
        lim = effective_limits(Config(risk_profile=name), real)
        assert lim.t3_risk_pct <= 2.0 and lim.min_viable_cap_pct <= 2.0 and lim.daily_loss_pct <= 5.0
        assert lim.max_open_risk_pct <= 4.0 and lim.drawdown_breaker_pct <= 20.0

    def test_aggressive_ratchet_locks_30_percent(self) -> None:
        assert effective_limits(Config(risk_profile="aggressive"), True).lock_fraction == 0.30

    def test_user_cap_binds_the_minimum_viable_cap(self) -> None:
        cfg = Config(risk_profile="aggressive", sizing={"min_viable_risk_cap_pct": 0.8})  # type: ignore[arg-type]
        cfg = Config.from_dict({"risk_profile": "aggressive", "sizing": {"min_viable_risk_cap_pct": 0.8}})
        assert effective_limits(cfg, True).min_viable_cap_pct == 0.8
        off = Config.from_dict({"sizing": {"min_viable_enabled": False}})
        assert effective_limits(off, True).min_viable_cap_pct == 0.0

    def test_config_cannot_exceed_the_hard_ceiling(self) -> None:
        with pytest.raises(ValueError):
            Config.from_dict({"sizing": {"min_viable_risk_cap_pct": 2.5}})
        with pytest.raises(ValueError):
            Config.from_dict({"budget": {"weekly_soft_limit_pct": 20.0}})


class TestRiskLayerAtTheMinimumLot:
    def plan(self, volume: float, exception: bool, risk_pct: float) -> TradePlan:
        return TradePlan("BUY", 1.1, 1.09, 1.12, 1000, 2000, volume, 70.0, volume * 70.0, risk_pct, exception)

    def ctx(self, plan: TradePlan, equity: float, notional: float, **kw: Any) -> EntryContext:
        return EntryContext("EURUSD", plan, equity, 5.0, [], notional_money=notional, allowed_risk_pct=0.5,
                            exception_cap_pct=2.0, **kw)

    def manager(self) -> RiskManager:
        return RiskManager(Config(risk_profile="aggressive"), True)

    def test_minimum_lot_has_its_own_exposure_cap(self) -> None:
        plan = self.plan(0.01, True, 1.0)
        assert self.manager().approve_entry(self.ctx(plan, 70.0, 1160.0, min_lot=True))[0]
        assert not self.manager().approve_entry(self.ctx(plan, 70.0, 1160.0, min_lot=False))[0]

    def test_even_the_minimum_lot_has_a_notional_ceiling(self) -> None:
        plan = self.plan(0.01, True, 1.0)
        ok, why = self.manager().approve_entry(self.ctx(plan, 70.0, 70.0 * 31, min_lot=True))
        assert not ok and "exposure" in why

    def test_exception_can_never_pass_the_hard_ceiling(self) -> None:
        plan = self.plan(0.1, True, 10.0)
        ok, why = self.manager().approve_entry(self.ctx(plan, 70.0, 100.0, min_lot=True))
        assert not ok and "planned risk" in why


class TestBudgets:
    NOW = 1_760_000_000.0

    def room(self, rows: list, equity: float = 65.0, trades_day: int = 4, trades_week: int = 12, risk: float = 0.6,
             now: float = NOW) -> Any:
        return exploration_room(rows, now, 0, equity, equity, 0.15, 0.5, trades_day, trades_week, risk, 5.0, 10.0)

    def row(self, pnl: float, ts: float) -> SimpleNamespace:
        return SimpleNamespace(status="closed", close_ts=ts, pnl=pnl, risk_money=0.0)

    def test_percent_alone_could_not_pay_one_trade(self) -> None:
        old = exploration_room([], self.NOW, 0, 65.0, 65.0, 0.15, 0.5)
        assert old.room < 0.2
        assert self.room([]).room == pytest.approx(2.4) and self.room([]).trades_left_day == 4

    def test_bounded_by_the_daily_and_weekly_limits(self) -> None:
        wide = self.room([], risk=1.5)
        assert wide.day_budget == pytest.approx(3.25) and wide.week_budget == pytest.approx(6.5)

    def test_stops_when_exhausted_and_resets_by_day_and_week(self) -> None:
        today = server_day_start(self.NOW) + 10
        spent = [self.row(-0.6, today) for _ in range(4)]
        assert self.room(spent).room == 0.0 and self.room(spent).trades_left_day == 0
        tomorrow = self.NOW + 86400
        assert self.room(spent, now=tomorrow).trades_left_day == 4
        assert self.room(spent, now=tomorrow).week_used == pytest.approx(2.4)
        assert self.room(spent, now=self.NOW + 8 * 86400).week_used == 0.0

    def test_open_trades_count_with_their_full_risk(self) -> None:
        open_row = SimpleNamespace(status="open", close_ts=None, pnl=None, risk_money=0.6)
        assert self.room([open_row]).trades_left_day == 3

    def test_a_win_gives_room_back(self) -> None:
        today = server_day_start(self.NOW) + 10
        assert self.room([self.row(-0.6, today), self.row(0.6, today)]).trades_left_day == 4


class TestAntiMartingaleAtTheMinimumLot:
    def row(self, pnl: float, risk: float, ts: float = 1.0) -> SimpleNamespace:
        return SimpleNamespace(status="closed", pnl=pnl, risk_money=risk, close_ts=ts, extra={"risk_pct": 0.6})

    def test_money_cap_after_a_loss_lifts_after_a_win(self) -> None:
        assert anti_martingale_money([self.row(-0.4, 0.41)]) == 0.41
        assert anti_martingale_money([self.row(-0.4, 0.41, 1.0), self.row(0.5, 0.41, 2.0)]) is None


# -------------------------------------------------- the owner's account, end to end


def owner_terminal(fake: FakeMT5, equity: float) -> FakeMT5:
    """EURUSD and BTCUSD and XAUUSD with realistic specifications."""
    fake.account.equity = fake.account.balance = equity
    fake.account.leverage = 500
    btc = fake.symbols.setdefault("BTCUSD", None)
    fake.add_symbol("BTCUSD", bid=65_000.0, spread_points=2000)
    s = fake.symbols["BTCUSD"]
    s.point, s.digits, s.trade_contract_size, s.trade_tick_size, s.trade_tick_value = 0.01, 2, 1, 0.01, 0.01
    s.volume_max = 10.0
    fake.rates["BTCUSD"] = make_rates(np.full(1200, 65_000.0), half_range=30.0)
    fake.set_tick("BTCUSD", 65_000.0, 2000)
    fake.add_symbol("XAUUSD", bid=4_000.0, spread_points=30)
    x = fake.symbols["XAUUSD"]
    x.point, x.digits, x.trade_contract_size, x.trade_tick_size, x.trade_tick_value = 0.01, 2, 100, 0.01, 1.0
    fake.rates["XAUUSD"] = make_rates(np.full(1200, 4_000.0), half_range=1.0)
    fake.set_tick("XAUUSD", 4_000.0, 30)
    del btc
    return fake


class Scripted:
    def __init__(self, intents: dict) -> None:
        self.intents = intents

    def on_bar(self, symbol: str, closed: Any, spec: Any, costs: Any, equity: float, risk_pct: float) -> Any:
        return self.intents.get(symbol)

    def exit_signal(self, *args: Any) -> bool:
        return False


def t1(stop: float) -> EntryIntent:
    return EntryIntent(Signal("BUY", stop / 1.5, 1.5, 3.0), None, "scripted", "scripted", tier="T1", exploration=True,
                       timeframe="M15")


STOPS_65 = {"EURUSD M5": 0.000413, "EURUSD M15": 0.000983, "BTCUSD M5": 73.5, "BTCUSD M15": 120.7}


@pytest.mark.parametrize("equity", [65.09, 110.0])
@pytest.mark.parametrize("name", list(STOPS_65))
def test_owner_account_places_minimum_viable_trades(make_bot, fake_mt5: FakeMT5, equity: float, name: str) -> None:
    """At both the inferred equity and the spec's 110 USD, each arm trades instead of blocking 100%."""
    symbol = name.split()[0]
    owner_terminal(fake_mt5, equity)
    bot = make_bot(source=Scripted({symbol: t1(STOPS_65[name])}), risk_profile="aggressive", symbols=(symbol,),
                   risk_per_trade_pct_demo=None, max_daily_loss_pct_demo=None, commission_per_lot=0.0,
                   deviation_points=0, max_total_open_risk_pct=None, max_drawdown_from_peak_pct=None,
                   max_open_positions_total=None, universe={"max_spread_to_stop": 1.0})
    bot.startup()
    assert bot.run_once() is None
    fake_mt5.advance_bar(symbol)
    bot.run_once()
    rows = bot.registry.trades(mode="live")
    assert len(rows) == 1, bot.registry.conn.execute("SELECT reason FROM funnel_events").fetchall()
    assert rows[0].volume == 0.01 and rows[0].risk_pct <= 2.0
    bot.close()


def test_gold_is_skipped_with_a_reason_before_it_counts_as_a_signal(make_bot, fake_mt5: FakeMT5) -> None:
    owner_terminal(fake_mt5, 110.0)
    bot = make_bot(source=None, risk_profile="aggressive", symbols=("EURUSD",), team=False,
                   risk_per_trade_pct_demo=None, commission_per_lot=0.0, universe={"candidates": ["EURUSD"]})
    bot.startup()
    bot.specs["XAUUSD"] = SymbolSpec.from_info(fake_mt5.symbols["XAUUSD"])
    sig = SimpleNamespace(signal=Signal("BUY", 4.0, 1.5, 3.0), arm=SimpleNamespace(tier="T0"))
    assert bot._signal_skip("XAUUSD", "M15", sig, fake_mt5.account) == "MIN_LOT_ABOVE_CAP"
    bot.close()


def test_below_the_floor_explains_itself_and_a_deposit_starts_trading(make_bot, fake_mt5: FakeMT5) -> None:
    owner_terminal(fake_mt5, 3.0)
    bot = make_bot(source=None, risk_profile="aggressive", symbols=("EURUSD",), team=False, commission_per_lot=0.0,
                   risk_per_trade_pct_demo=None, universe={"candidates": ["EURUSD"], "timeframes": ["M15"]})
    bot.startup()
    fake_mt5.rates["EURUSD"] = make_rates(1.1 + 0.0004 * np.sin(np.arange(1200) / 5.0), half_range=0.0003)
    fake_mt5.set_tick("EURUSD", 1.1, 8)
    report = bot._scan_feasibility(fake_mt5.account)
    assert report["below_floor"] and report["floor"] > 3.0
    assert "piso de viabilidade" in report["message"] and "micro ou cent" in report["message"]
    assert "sombra continua" in report["message"]
    sig = SimpleNamespace(signal=Signal("BUY", 0.0005, 1.5, 3.0), arm=SimpleNamespace(tier="T0"))
    assert bot._signal_skip("EURUSD", "M15", sig, fake_mt5.account) is not None
    fake_mt5.account.equity = fake_mt5.account.balance = 110.0
    assert bot._signal_skip("EURUSD", "M15", sig, fake_mt5.account) is None     # same cycle, no restart
    assert not bot._scan_feasibility(fake_mt5.account)["below_floor"]
    bot.close()


def test_no_equity_halts_the_cycle_and_alerts(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot()
    bot.startup()
    fake_mt5.account.equity = None
    assert bot.run_once() is None
    assert fake_mt5.sends == []
    bot.close()


def test_larger_size_shrinks_to_the_exposure_cap_instead_of_being_refused(make_bot, fake_mt5: FakeMT5) -> None:
    fake_mt5.account.equity = fake_mt5.account.balance = 300.0
    bot = make_bot(source=Scripted({"EURUSD": t1(0.0007)}), risk_profile="aggressive", commission_per_lot=0.0,
                   risk_per_trade_pct_demo=None, deviation_points=0, universe={"max_spread_to_stop": 1.0})
    bot.startup()
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    [row] = bot.registry.trades(mode="live")
    assert row.volume == 0.01         # 5x of 300 fits 0.01 lot only, where 0.02 would be 7.3x
    bot.close()


def test_slices_are_sent_over_time_each_with_its_own_key(make_bot, fake_mt5: FakeMT5) -> None:
    fake_mt5.symbols["EURUSD"].volume_max = 0.1
    now = [1000.0]
    bot = make_bot(source=Scripted({"EURUSD": t1(0.0010)}), commission_per_lot=0.0, monotonic=lambda: now[0],
                   capacity={"child_interval_seconds": 30.0})
    bot.startup()
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    opening = [s for s in fake_mt5.sends if s.get("sl")]
    assert len(opening) == 1 and len(bot._children) >= 1
    now[0] += 31
    bot._signal_source.intents = {}
    bot.run_once()
    opening = [s for s in fake_mt5.sends if s.get("sl")]
    assert len(opening) >= 2 and len({s["comment"] for s in opening}) == len(opening)
    bot.close()


def test_slices_stop_if_the_entry_is_gone(make_bot, fake_mt5: FakeMT5) -> None:
    fake_mt5.symbols["EURUSD"].volume_max = 0.1
    now = [1000.0]
    bot = make_bot(source=Scripted({"EURUSD": t1(0.0010)}), commission_per_lot=0.0, monotonic=lambda: now[0],
                   capacity={"child_interval_seconds": 30.0})
    bot.startup()
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    sent = len(fake_mt5.sends)
    fake_mt5.positions.clear()
    now[0] += 31
    bot._signal_source.intents = {}
    bot.run_once()
    assert len([s for s in fake_mt5.sends[sent:] if s.get("sl")]) == 0 and bot._children == []
    bot.close()


def test_weekly_soft_limit_pauses_entries_and_cash_flow_does_not_count(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot(risk_profile="aggressive", risk_per_trade_pct_demo=None)
    bot.startup()
    bot.run_once()
    acc = fake_mt5.account
    acc.equity = acc.balance = 8_900.0
    bot._update_weekly_limit(acc)
    assert bot._entry_block_reason() is not None and "weekly" in bot._entry_block_reason()
    assert bot._weekly_block
    bot._rebase_week(-1_100.0)      # the same fall as a withdrawal is not a loss
    bot._update_weekly_limit(acc)
    assert bot._weekly_block is None
    bot.close()


def test_commission_from_history_beats_the_config_and_logs_the_mismatch(make_bot, fake_mt5: FakeMT5, caplog) -> None:
    fake_mt5.deals.append(SimpleNamespace(symbol="EURUSD", volume=1.0, entry=fake_mt5.DEAL_ENTRY_IN, commission=-3.5,
                                          type=0, ticket=1, time=0, position_id=1, profit=0.0))
    fake_mt5.deals.append(SimpleNamespace(symbol="EURUSD", volume=1.0, entry=fake_mt5.DEAL_ENTRY_OUT, commission=-3.5,
                                          type=0, ticket=2, time=0, position_id=1, profit=0.0))
    bot = make_bot(commission_per_lot=0.0)
    bot.startup()
    assert bot.commission["EURUSD"] == pytest.approx(7.0)
    assert re.search(r"commission_per_lot in the config is 0.00 but the deal history shows 7.00", caplog.text)
    bot.close()


class TestGatesOnTheEntryPath:
    def post(self, mean: float, sd: float = 0.02) -> Any:
        from mt5bot.evidence import Posterior

        return Posterior(mean, sd, 0.99, mean - sd, mean + sd, 0.5, 0.0, 0.05, 0, 60, 60.0, mean, 1.1)

    def bot(self, make_bot: Any) -> Any:
        bot = make_bot(commission_per_lot=0.0)
        bot.startup()
        return bot

    def plan(self, exception: bool = False) -> TradePlan:
        return TradePlan("BUY", 1.1, 1.09, 1.12, 1000, 2000, 0.1, 100.0, 10.0, 0.5, exception)

    def intent(self, tier: str, mean: float, sd: float = 0.02) -> EntryIntent:
        return EntryIntent(Signal("BUY", 0.001, 2.0, 3.0), None, "s", "s", tier=tier, posterior=self.post(mean, sd),
                           timeframe="M15")

    def gate(self, bot: Any, intent: EntryIntent, plan: TradePlan, **kw: Any) -> Any:
        info = bot.market.symbol_info("EURUSD")
        return bot._cost_gate("EURUSD", intent, plan, 10.0, bot.specs["EURUSD"], info, **kw)

    def test_t1_uses_the_mean_and_the_lighter_ratio(self, make_bot) -> None:
        bot = self.bot(make_bot)
        assert self.gate(bot, self.intent("T1", 0.25, 0.2), self.plan()) is None    # lower bound well below zero
        bot.close()

    def test_scaling_uses_the_lower_bound_and_the_higher_ratio(self, make_bot) -> None:
        bot = self.bot(make_bot)
        refused = self.gate(bot, self.intent("T2", 0.25, 0.2), self.plan())
        assert refused is not None and "lower edge" in refused
        assert self.gate(bot, self.intent("T2", 0.25, 0.2), self.plan(), entry=True) is None
        bot.close()

    def test_minimum_viable_trades_use_the_entry_gate_at_any_tier(self, make_bot) -> None:
        bot = self.bot(make_bot)
        assert self.gate(bot, self.intent("T3", 0.25, 0.2), self.plan(exception=True)) is None
        bot.close()

    def test_exploration_is_exempt_because_threshold_and_budget_bound_it(self, make_bot) -> None:
        bot = self.bot(make_bot)
        intent = EntryIntent(Signal("BUY", 0.001, 2.0, 3.0), None, "s", "s", tier="T1", exploration=True,
                             posterior=self.post(-0.01, 0.2), timeframe="M15")
        assert Config().costs.exploration_exempt_real is True and self.gate(bot, intent, self.plan()) is None
        bot.close()


def test_a_deposit_is_not_a_weekly_result_and_rescans_at_once(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot(risk_profile="aggressive", risk_per_trade_pct_demo=None)
    bot.startup()
    bot.run_once()
    bot._next_scan = 1e18
    bot.registry.set_kv("week_ref", '{"week": "1", "equity": 100.0}')
    bot.registry.set_kv("cash_flow_checked", "0")
    fake_mt5.deals.append(SimpleNamespace(symbol="", volume=0.0, entry=0, commission=0.0, type=fake_mt5.DEAL_TYPE_BALANCE,
                                          ticket=9, time=1, position_id=0, profit=500.0, comment="deposit"))
    bot._check_cash_flow()
    assert bot._next_scan == 0.0 and '"equity": 600.0' in bot.registry.get_kv("week_ref")
    assert bot.guard.halted_reason is None
    bot.close()


def test_equity_that_is_not_a_number_refuses_to_start(make_bot, fake_mt5: FakeMT5) -> None:
    from mt5bot.bot import StartupError

    fake_mt5.account.equity = None
    bot = make_bot()
    with pytest.raises(StartupError, match="usable equity"):
        bot.startup()


def test_risk_appetite_is_not_loosened_by_a_hot_reload() -> None:
    from mt5bot.config import reload_decision

    old = Config.from_dict({"sizing": {"min_viable_risk_cap_pct": 0.5}})
    new = Config.from_dict({"sizing": {"min_viable_risk_cap_pct": 2.0}})
    merged, notes = reload_decision(old, new)
    assert merged.sizing.min_viable_risk_cap_pct == 0.5 and any("sizing" in n for n in notes)


def test_after_a_loss_the_minimum_lot_keeps_its_size_but_not_more_money(make_bot, fake_mt5: FakeMT5) -> None:
    """Equity fell, so the same lot is a larger percentage: that must not block it, nor allow more money."""
    owner_terminal(fake_mt5, 65.09)
    source = Scripted({"EURUSD": EntryIntent(Signal("BUY", 0.000275, 1.5, 3.0), None, "s", "s", arm_id=7, tier="T1",
                                             exploration=True, timeframe="M15")})
    bot = make_bot(source=source, risk_profile="aggressive", symbols=("EURUSD",), risk_per_trade_pct_demo=None,
                   max_daily_loss_pct_demo=None, commission_per_lot=0.0, deviation_points=0,
                   universe={"max_spread_to_stop": 1.0})
    bot.startup()
    bot.run_once()
    now = bot._utc_now()
    tid = bot.registry.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.0996, 1.101, 0.01, 0.41, ticket=5, arm_id=7,
                                  open_ts=now - 600, tier="T1", extra={"risk_pct": 0.63})
    bot.registry.close_trade(tid, 1.0996, -0.41, -1.0, "sl", close_ts=now - 300)
    fake_mt5.account.equity = fake_mt5.account.balance = 64.68
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    bot.funnel.flush()
    reasons = [r[0] for r in bot.registry.conn.execute("SELECT reason FROM funnel_events")]
    rows = bot.registry.trades(mode="live", status="open")
    assert "ANTI_MARTINGALE" in reasons or len(rows) == 1
    if rows:
        assert rows[0].volume == 0.01 and rows[0].risk_money <= 0.41 * (1 + 1e-9)
    bot.close()


def test_a_closed_market_does_not_erase_the_floor() -> None:
    """Regression: with every symbol quiet the floor was None, so a weekend read as nothing being tradable."""
    from dataclasses import replace

    c, _spec = candidate("EURUSD")
    closed = replace(c, liquid=False)
    row = fz.evaluate(closed, 10_000.0, rules())
    key = (c.symbol, c.timeframe, c.stop_policy)
    assert fz.feasibility_floor([row], {key: closed}) == row.floor_equity


def test_liquid_candidates_decide_the_floor_when_there_are_any() -> None:
    from dataclasses import replace

    cheap, _ = candidate("EURUSD")
    dear, _ = candidate("XAUUSD")
    cheap = replace(cheap, liquid=False)
    rows = [fz.evaluate(cheap, 1e6, rules()), fz.evaluate(dear, 1e6, rules())]
    table = {(c.symbol, c.timeframe, c.stop_policy): c for c in (cheap, dear)}
    assert fz.feasibility_floor(rows, table) == rows[1].floor_equity


def test_risk_view_follows_the_posteriors_and_the_clock(make_bot, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: it only refreshed on a change of profile or mode, so it could stay stale for ever."""
    now = [1000.0]
    bot = make_bot(monotonic=lambda: now[0])
    bot.startup()
    calls: list[int] = []
    monkeypatch.setattr(bot, "_refresh_risk_view", lambda acc: calls.append(1))
    bot.feasibility = {"mode": "percent"}
    bot.arms = SimpleNamespace(arms={}, record_learning_points=lambda ts: 0)
    bot._learning_housekeeping(fake_mt5.account)
    bot._learning_housekeeping(fake_mt5.account)
    assert len(calls) == 1
    bot.arms = SimpleNamespace(arms={1: SimpleNamespace(tier="T1")}, record_learning_points=lambda ts: 0)
    bot._learning_housekeeping(fake_mt5.account)      # the first arm passed the gates
    assert len(calls) == 2
    now[0] += 6 * 3600 + 1
    bot._learning_housekeeping(fake_mt5.account)      # time alone also refreshes it
    assert len(calls) == 3
    bot.arms = None
    bot.close()


def test_a_failing_activity_estimate_never_costs_an_order(make_bot, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an exception in this optional estimate killed every entry and the opportunity scan."""
    bot = make_bot(source=Scripted({"EURUSD": t1(0.0030)}), commission_per_lot=0.0)
    bot.startup()
    bot.run_once()
    real = bot.market.rates

    def rates(symbol: str, timeframe: str, start: int, count: int) -> Any:
        if count == 48:
            raise RuntimeError("no activity data")
        return real(symbol, timeframe, start, count)

    monkeypatch.setattr(bot.market, "rates", rates)
    assert bot._ticks_per_minute("EURUSD") is None
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    assert len(bot.registry.trades(mode="live")) == 1
    bot.close()


def test_capacity_only_reads_the_timeframe_the_bot_trades(make_bot, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = make_bot(timeframe="H1", universe={"timeframes": ["H1"]})
    bot.startup()
    asked: list[str] = []
    real = bot.market.rates

    def rates(symbol: str, timeframe: str, start: int, count: int) -> Any:
        asked.append(timeframe)
        return real(symbol, timeframe, start, count)

    monkeypatch.setattr(bot.market, "rates", rates)
    bot._ticks_per_minute("EURUSD")
    assert asked == ["H1"]
    bot.close()


def test_a_failing_report_or_slice_does_not_stop_the_loop(make_bot, fake_mt5: FakeMT5, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = make_bot()
    bot.startup()

    def boom(*a: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "_update_slo", boom)
    monkeypatch.setattr(bot, "_drain_children", boom)
    bot._children = [SimpleNamespace(group="g")]  # type: ignore[list-item]
    bot._next_housekeeping = 0.0
    assert bot.run_once() is None and bot._children == []
    bot.close()


def test_a_failing_feasibility_check_leaves_the_decision_to_the_risk_layer(make_bot, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = make_bot()
    bot.startup()
    sig = SimpleNamespace(arm=SimpleNamespace(id=1, strategy="s", tier="T1"))

    def boom(*a: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "_signal_skip", boom)
    assert bot._feasible_signals("EURUSD", "M15", [sig], SimpleNamespace(equity=1000.0), 1) == [sig]
    bot.close()
