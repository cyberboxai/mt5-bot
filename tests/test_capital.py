"""Capital that adapts to any account size: the account-size matrix, cash flows, the profit
ratchet, house money, anti-martingale sizing, Kelly caps, affordability and order splitting."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from mt5bot import limits
from mt5bot.capital import (CapitalState, SizingInputs, affordability, anti_martingale_cap, large_account_factor,
                            size_risk, split_volume)
from mt5bot.config import Config
from mt5bot.profiles import effective_limits
from mt5bot.risk import SymbolSpec, plan_trade

LIM = effective_limits(Config(), is_real=False)
LIM_REAL = effective_limits(Config(), is_real=True)

# Fake contract specifications: an FX major, gold, a stock index CFD and a crypto CFD.
SPECS = {
    "EURUSD": (SymbolSpec("EURUSD", 0.00001, 5, 1.0, 0.00001, 100_000, 0.01, 100.0, 0.01), 0.0020, 7.0),
    "XAUUSD": (SymbolSpec("XAUUSD", 0.01, 2, 1.0, 0.01, 100, 0.01, 50.0, 0.01), 8.0, 7.0),
    "US30": (SymbolSpec("US30", 1.0, 1, 1.0, 1.0, 1, 0.1, 50.0, 0.1), 150.0, 0.0),
    "BTCUSD": (SymbolSpec("BTCUSD", 0.01, 2, 0.01, 0.01, 1, 0.01, 10.0, 0.01), 1500.0, 0.0),
}
EQUITIES = (50, 100, 500, 1_000, 10_000, 100_000, 1_000_000)


def fresh(equity: float) -> CapitalState:
    state = CapitalState()
    state.observe(equity, 0.0)
    return state


def plan_for(spec: SymbolSpec, stop: float, commission: float, equity: float, risk_pct: float,
             cap: float | None) -> object:
    atr = stop / 2.0
    price = {"EURUSD": 1.1, "XAUUSD": 2350.0, "US30": 42_000.0, "BTCUSD": 65_000.0}[spec.name]
    return plan_trade("BUY", atr, 2.0, 3.0, price, 0.0, spec, equity, risk_pct, commission, 0.0,
                      min_lot_cap_pct=cap, max_volume=spec.volume_max * limits.MAX_SPLIT_ORDERS)


class TestAccountSizeMatrix:
    @pytest.mark.parametrize("symbol", list(SPECS))
    @pytest.mark.parametrize("equity", EQUITIES)
    @pytest.mark.parametrize("tier", ["T1", "T2", "T3"])
    def test_sizing_never_exceeds_caps_and_rounds_down(self, symbol: str, equity: float, tier: str) -> None:
        spec, stop, commission = SPECS[symbol]
        lim = LIM
        decision = size_risk(SizingInputs(tier, equity, kelly_fraction_full=1.0,
                                          budget_room_money=equity * lim.explore_day_pct / 100),
                             lim, fresh(equity))
        assert decision.ok and 0 < decision.risk_pct <= lim.t3_risk_pct <= limits.MAX_RISK_PER_TRADE_PCT
        cap = lim.explore_trade_pct if tier == "T1" else lim.affordability_cap_pct
        result = plan_for(spec, stop, commission, equity, decision.risk_pct, cap)
        afford = affordability(symbol, "M15", stop, spec.value_per_price, spec.volume_min, commission, equity,
                               decision.risk_pct, cap)
        if result.plan is None:
            # Infeasible: skipped with an explicit reason, listed in the affordability map.
            assert "minimum volume" in result.reason and not afford.feasible
            assert afford.reason == "MIN_LOT_EXCEEDS_RISK" and afford.min_equity > equity
            return
        plan = result.plan
        allowed = cap if plan.exception else decision.risk_pct
        assert plan.planned_risk <= equity * allowed / 100 * (1 + 1e-9)
        assert plan.planned_risk <= equity * limits.MAX_RISK_PER_TRADE_PCT / 100
        steps = plan.volume / spec.volume_step
        assert abs(steps - round(steps)) < 1e-9 and plan.volume >= spec.volume_min
        assert afford.feasible and afford.exception == plan.exception

    def test_small_accounts_are_honestly_constrained(self) -> None:
        spec, stop, commission = SPECS["EURUSD"]
        a = affordability("EURUSD", "M15", stop, spec.value_per_price, spec.volume_min, commission, 50.0,
                          LIM.t1_risk_pct, LIM.explore_trade_pct)
        assert not a.feasible and a.min_equity > 1000  # 0.01 lot at 20 pips: ~2 USD at risk
        tighter = affordability("EURUSD", "M5", stop / 4, spec.value_per_price, spec.volume_min, commission, 500.0,
                                LIM.t1_risk_pct, LIM.explore_trade_pct)
        assert tighter.min_lot_risk_pct < a.min_lot_risk_pct  # shorter stops make small accounts feasible sooner

    def test_large_accounts_split_orders_and_damp(self) -> None:
        spec, stop, commission = SPECS["US30"]
        result = plan_for(spec, stop, commission, 1_000_000, 1.0, None)
        assert result.plan is not None and result.plan.volume > spec.volume_max
        parts = split_volume(result.plan.volume, spec.volume_max, spec.volume_step, spec.volume_min)
        assert 1 < len(parts) <= limits.MAX_SPLIT_ORDERS and all(p <= spec.volume_max for p in parts)
        assert sum(parts) <= result.plan.volume + 1e-9
        assert large_account_factor(1_000_000) == pytest.approx(0.85)
        assert large_account_factor(50_000) == 1.0
        t3_small = size_risk(SizingInputs("T3", 10_000), LIM, fresh(10_000)).risk_pct
        t3_large = size_risk(SizingInputs("T3", 1_000_000), LIM, fresh(1_000_000)).risk_pct
        assert t3_large < t3_small

    @pytest.mark.parametrize("volume,vmax,expected", [(10.0, 50.0, [10.0]), (120.0, 50.0, [50.0, 50.0, 20.0]),
                                                       (500.0, 50.0, [50.0] * 4), (0.004, 50.0, [])])
    def test_split_volume(self, volume: float, vmax: float, expected: list[float]) -> None:
        assert split_volume(volume, vmax, 0.01, 0.01) == expected


class TestCashFlows:
    def test_deposits_and_withdrawals_rebase_and_are_not_profit(self) -> None:
        cap = fresh(10_000)
        cap.observe(11_000, 1)                      # a real gain: the floor ratchets up
        floor_before, locked_before = cap.floor(LIM), cap.locked_profit(LIM)
        cap.apply_cash_flow(+5_000)                 # deposit
        cap.observe(16_000, 2)
        assert cap.c0 == 15_000 and cap.hwm == 16_000
        dd = LIM.floor_drawdown_pct / 100
        assert cap.locked_profit(LIM) == pytest.approx(locked_before)   # the deposit is not profit
        # The deposited money gets the same proportional drawdown budget as the rest.
        assert cap.floor(LIM) == pytest.approx(floor_before + 5_000 * (1 - dd))
        cap.apply_cash_flow(-12_000)                # large withdrawal
        cap.observe(4_000, 3)
        assert cap.c0 == 3_000 and cap.hwm == 4_000
        assert cap.locked_profit(LIM) == pytest.approx(locked_before)   # profit stays protected
        assert cap.cushion(4_000, LIM) == pytest.approx(dd * 3_000 + (1 - LIM.lock_fraction) * 1_000)
        assert cap.cash_flow_total == pytest.approx(-7_000)

    def test_withdrawal_never_trips_the_soft_floor(self) -> None:
        cap = fresh(10_000)
        cap.apply_cash_flow(-9_000)
        cap.observe(1_000, 1)
        assert cap.cushion(1_000, LIM) > 0
        assert size_risk(SizingInputs("T2", 1_000), LIM, cap).ok


class TestProfitRatchet:
    def test_floor_starts_below_capital_and_ratchets_up(self) -> None:
        cap = fresh(10_000)
        assert cap.floor(LIM) == pytest.approx(10_000 * (1 - LIM.floor_drawdown_pct / 100))
        assert cap.cushion_factor(10_000, LIM) == pytest.approx(1.0)
        cap.observe(12_000, 1)
        expected = 10_000 * (1 - LIM.floor_drawdown_pct / 100) + LIM.lock_fraction * 2_000
        assert cap.floor(LIM) == pytest.approx(expected)
        assert cap.locked_profit(LIM) == pytest.approx(LIM.lock_fraction * 2_000)

    def test_risk_shrinks_near_the_floor_and_stops_at_it(self) -> None:
        cap = fresh(10_000)
        risks = [size_risk(SizingInputs("T2", eq), LIM, cap).risk_pct for eq in (10_000, 9_700, 9_400, 9_250)]
        assert risks == sorted(risks, reverse=True) and risks[-1] < risks[0] / 2
        at_floor = size_risk(SizingInputs("T2", cap.floor(LIM)), LIM, cap)
        assert not at_floor.ok and at_floor.blocked == "FLOOR_SOFT_STOP"
        t1 = size_risk(SizingInputs("T1", cap.floor(LIM), budget_room_money=100.0), LIM, cap)
        assert not t1.ok  # exploration cannot cross the floor either

    def test_house_money_scales_within_caps(self) -> None:
        cap = fresh(10_000)
        cap.observe(13_000, 1)
        assert cap.cushion_factor(13_000, LIM) == pytest.approx(LIM.house_money_max)
        decision = size_risk(SizingInputs("T2", 13_000), LIM, cap)
        assert decision.risk_pct == pytest.approx(LIM.t2_risk_pct * LIM.house_money_max)
        t3 = size_risk(SizingInputs("T3", 13_000), LIM, cap)
        assert t3.risk_pct <= LIM.t3_risk_pct <= limits.MAX_RISK_PER_TRADE_PCT  # the profile cap still binds

    def test_portfolio_heat_and_correlation_bind(self) -> None:
        cap = fresh(10_000)
        hot = size_risk(SizingInputs("T2", 10_000, open_risk_money=10_000 * LIM.max_open_risk_pct / 100), LIM, cap)
        assert not hot.ok and hot.blocked == "RISK_OPEN_RISK"
        corr = size_risk(SizingInputs("T2", 10_000, correlation_factor=0.5), LIM, cap)
        assert corr.risk_pct == pytest.approx(LIM.t2_risk_pct * 0.5) and corr.binding == "correlation"

    def test_kelly_caps_t2_t3_but_not_exploration(self) -> None:
        cap = fresh(10_000)
        weak = size_risk(SizingInputs("T3", 10_000, kelly_fraction_full=0.004), LIM, cap)
        assert weak.binding == "kelly" and weak.risk_pct == pytest.approx(0.004 * LIM.kelly_fraction * 100)
        none = size_risk(SizingInputs("T2", 10_000, kelly_fraction_full=0.0), LIM, cap)
        assert not none.ok and none.blocked == "KELLY_ZERO"
        explore = size_risk(SizingInputs("T1", 10_000, kelly_fraction_full=0.0, budget_room_money=20.0), LIM, cap)
        assert explore.ok

    def test_exploration_budget_binds_t1(self) -> None:
        cap = fresh(10_000)
        d = size_risk(SizingInputs("T1", 10_000, budget_room_money=3.0), LIM, cap)
        assert d.risk_pct == pytest.approx(0.03) and d.binding == "exploration_budget"
        spent = size_risk(SizingInputs("T1", 10_000, budget_room_money=0.0), LIM, cap)
        assert not spent.ok and spent.blocked == "EXPLORATION_BUDGET_SPENT"

    def test_real_account_profile_is_stricter(self) -> None:
        assert LIM_REAL.t1_risk_pct < LIM.t1_risk_pct and LIM_REAL.explore_day_pct < LIM.explore_day_pct


class TestAntiMartingale:
    def test_cap_after_a_loss_lifts_after_a_win(self) -> None:
        rows = [SimpleNamespace(status="closed", pnl=-5.0, close_ts=10.0, extra={"risk_pct": 0.3}),
                SimpleNamespace(status="closed", pnl=8.0, close_ts=5.0, extra={"risk_pct": 0.5})]
        assert anti_martingale_cap(rows) == 0.3
        rows.append(SimpleNamespace(status="closed", pnl=1.0, close_ts=20.0, extra={"risk_pct": 0.3}))
        assert anti_martingale_cap(rows) is None
        assert anti_martingale_cap([]) is None

    @pytest.mark.parametrize("seed", range(8))
    def test_size_never_increases_after_a_loss(self, seed: int) -> None:
        """Property: whatever the tier changes and outcomes, after a losing trade the next trade of
        the same arm never risks more money than the losing one."""
        rng = np.random.default_rng(seed)
        cap = fresh(10_000)
        equity, history, sizes = 10_000.0, [], []
        tiers = ["T1", "T2", "T3"]
        for k in range(200):
            tier = tiers[int(rng.integers(0, 3))]   # evidence may promote the arm at any time
            amc = anti_martingale_cap(history)
            d = size_risk(SizingInputs(tier, equity, kelly_fraction_full=0.05, budget_room_money=50.0,
                                       last_loss_risk_pct=amc), LIM, cap)
            if not d.ok:
                continue
            money = equity * d.risk_pct / 100
            won = rng.random() < 0.45
            pnl = money * (1.5 if won else -1.0)
            sizes.append((money, pnl))
            history.append(SimpleNamespace(status="closed", pnl=pnl, close_ts=float(k), extra={"risk_pct": d.risk_pct}))
            equity += pnl
            cap.observe(equity, float(k))
        for (m0, p0), (m1, _p1) in zip(sizes, sizes[1:]):
            if p0 < 0:
                assert m1 <= m0 + 1e-9

    def test_equity_only_sizing_shrinks_after_losses(self) -> None:
        cap = fresh(10_000)
        risks = [size_risk(SizingInputs("T2", eq), LIM, cap).risk_pct * eq for eq in (10_000, 9_900, 9_800)]
        assert risks == sorted(risks, reverse=True)


def test_capital_state_roundtrip_and_corruption() -> None:
    cap = fresh(1234.5)
    cap.apply_cash_flow(100)
    again = CapitalState.from_json(cap.to_json())
    assert again.c0 == cap.c0 and again.cash_flow_total == 100
    assert CapitalState.from_json("{not json").c0 is None
    assert CapitalState.from_json('{"c0": "x", "hwm": 5}').c0 is None
    assert math.isclose(CapitalState.from_json('{"c0": 10, "hwm": 12}').floor(LIM), 12 - (0.08 * 10 + 0.6 * 2))
