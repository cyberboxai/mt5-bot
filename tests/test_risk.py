"""Hard safety layer: ceilings, forbidden behaviours and the drawdown circuit breaker."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conftest import EURUSD_SPEC, FakeMT5, FixedSource
from mt5bot import limits
from mt5bot.config import Config
from mt5bot.risk import (EntryContext, EquityGuard, NewsCalendar, OpenRisk, RiskManager, RiskViolation, TradePlan,
                         plan_trade, request_resume, validate_open_request, validate_plan_stops, validate_sl_change)


def plan(side: str = "BUY", price: float = 1.1, sl_points: int = 200, tp_points: int = 300,
         volume: float = 0.23, loss_per_lot: float = 210.0, risk_pct: float = 0.5) -> TradePlan:
    sign = 1 if side == "BUY" else -1
    return TradePlan(side, price, round(price - sign * sl_points * 1e-5, 5), round(price + sign * tp_points * 1e-5, 5),
                     sl_points, tp_points, volume, loss_per_lot, 10_000 * risk_pct / 100, risk_pct)


def ctx(p: TradePlan, **kw) -> EntryContext:
    # allowed_risk_pct is the sizer's verdict for this entry (0.5% here, like the v3.0 fixed risk).
    values = dict(symbol="EURUSD", plan=p, equity=10_000.0, spread_points=10.0, bot_positions=[],
                  notional_money=25_000.0, allowed_risk_pct=0.5)
    values.update(kw)
    return EntryContext(**values)


@pytest.fixture
def manager() -> RiskManager:
    return RiskManager(Config(risk_per_trade_pct_demo=0.5, max_total_open_risk_pct=2.0), is_real=False)


class TestCeilings:
    @pytest.mark.parametrize("key,value", [
        ("risk_per_trade_pct_demo", limits.MAX_RISK_PER_TRADE_PCT + 0.01),
        ("risk_per_trade_pct_real", 5.0),
        ("max_daily_loss_pct_demo", limits.MAX_DAILY_LOSS_PCT + 0.1),
        ("max_daily_loss_pct_real", 10.0),
        ("max_open_positions_total", limits.MAX_OPEN_POSITIONS + 1),
        ("max_total_open_risk_pct", limits.MAX_TOTAL_OPEN_RISK_PCT + 0.1),
        ("max_symbol_exposure_x", limits.MAX_SYMBOL_EXPOSURE_X + 1),
        ("max_drawdown_from_peak_pct", limits.MAX_DRAWDOWN_FROM_PEAK_PCT + 1),
    ])
    def test_config_cannot_exceed_hard_ceilings(self, key: str, value: float) -> None:
        with pytest.raises(ValueError):
            Config.from_dict({key: value})

    def test_risk_manager_clamps_even_if_validation_is_bypassed(self) -> None:
        # dataclasses.replace skips validate(): the risk layer must still clamp.
        cfg = dataclasses.replace(Config(), risk_per_trade_pct_demo=50.0, max_daily_loss_pct_demo=90.0,
                                  max_open_positions_total=99, max_total_open_risk_pct=80.0,
                                  max_symbol_exposure_x=500.0, max_drawdown_from_peak_pct=95.0)
        # A user cap can only tighten the profile; absurd caps fall back to the profile, itself
        # under the hard ceilings, and the risk layer clamps to the ceilings again.
        for profile in ("conservative", "balanced", "aggressive"):
            rm = RiskManager(dataclasses.replace(cfg, risk_profile=profile), is_real=False)
            assert rm.risk_pct <= limits.MAX_RISK_PER_TRADE_PCT
            assert rm.max_daily_loss_pct <= limits.MAX_DAILY_LOSS_PCT
            assert rm.max_positions <= limits.MAX_OPEN_POSITIONS
            assert rm.max_total_open_risk_pct <= limits.MAX_TOTAL_OPEN_RISK_PCT
            assert rm.max_symbol_exposure_x == limits.MAX_SYMBOL_EXPOSURE_X
            assert rm.max_drawdown_pct <= limits.MAX_DRAWDOWN_FROM_PEAK_PCT
            # Even an allowed fraction above the ceiling cannot pass the gate.
            big = plan(volume=1.0, loss_per_lot=300.0, risk_pct=3.0)
            ok, _ = rm.approve_entry(ctx(big, allowed_risk_pct=50.0, notional_money=1.0))
            assert not ok

    def test_plan_trade_clamps_risk_pct(self) -> None:
        result = plan_trade("BUY", 0.001, 2.0, 3.0, 1.1, 10, EURUSD_SPEC, 10_000, 50.0, 0.0, 10)
        assert result.plan is not None and result.plan.risk_pct == limits.MAX_RISK_PER_TRADE_PCT
        assert result.plan.planned_risk <= 10_000 * limits.MAX_RISK_PER_TRADE_PCT / 100 + 1e-9

    def test_guard_clamps_limits(self, tmp_path: Path) -> None:
        guard = EquityGuard(tmp_path)
        guard.update("2025-10-09", 10_000, 99.0, 99.0)
        events = guard.update("2025-10-09", 10_000 * (1 - limits.MAX_DAILY_LOSS_PCT / 100), 99.0, 99.0)
        assert "daily_limit" in events

    def test_total_open_risk_ceiling(self, manager: RiskManager) -> None:
        open_risk = [OpenRisk("GBPUSD", 100.0), OpenRisk("USDJPY", 60.0)]  # 1.6% at stake
        ok, reason = manager.approve_entry(ctx(plan(), bot_positions=open_risk))
        assert not ok and "total open risk" in reason

    def test_exposure_ceiling(self, manager: RiskManager) -> None:
        ok, reason = manager.approve_entry(ctx(plan(), notional_money=60_000.0))
        assert not ok and "exposure" in reason

    def test_max_positions(self) -> None:
        rm = RiskManager(Config(max_open_positions_total=2, max_total_open_risk_pct=4.0), is_real=False)
        ok, reason = rm.approve_entry(ctx(plan(), bot_positions=[OpenRisk("A", 1), OpenRisk("B", 1)]))
        assert not ok and "max 2" in reason

    def test_ok_case(self, manager: RiskManager) -> None:
        assert manager.approve_entry(ctx(plan())) == (True, "ok")


class TestForbiddenBehaviours:
    def test_martingale_like_size_is_rejected(self, manager: RiskManager) -> None:
        doubled = plan(volume=0.46)  # twice the risk-sized volume after a loss
        ok, reason = manager.approve_entry(ctx(doubled))
        assert not ok and "size increases are forbidden" in reason

    def test_higher_risk_fraction_is_rejected(self, manager: RiskManager) -> None:
        ok, _ = manager.approve_entry(ctx(plan(risk_pct=1.0, volume=0.2)))
        assert not ok

    def test_size_never_increases_after_losses(self) -> None:
        # Sizing is a pure function of equity: after a loss the size can only shrink.
        sizes = []
        for equity in (10_000, 9_950, 9_900, 9_850):
            result = plan_trade("BUY", 0.001, 2.0, 3.0, 1.1, 10, EURUSD_SPEC, equity, 0.5, 0.0, 10)
            sizes.append(result.plan.volume)
        assert sizes == sorted(sizes, reverse=True)

    def test_averaging_down_and_grid_are_rejected(self, manager: RiskManager) -> None:
        ok, reason = manager.approve_entry(ctx(plan(), bot_positions=[OpenRisk("EURUSD", 50.0)]))
        assert not ok and "averaging down" in reason

    @pytest.mark.parametrize("bad", [
        dict(sl=0.0), dict(tp=0.0), dict(sl=1.2), dict(tp=1.0),
    ])
    def test_orders_without_valid_sl_tp_are_rejected(self, manager: RiskManager, bad: dict) -> None:
        broken = dataclasses.replace(plan(), **bad)
        with pytest.raises(RiskViolation):
            validate_plan_stops(broken)
        ok, _ = manager.approve_entry(ctx(broken))
        assert not ok

    def test_request_without_sl_or_tp_is_blocked(self) -> None:
        p = plan()
        request = {"price": p.price, "sl": p.sl, "tp": p.tp, "volume": p.volume}
        validate_open_request(request, p)
        for key in ("sl", "tp"):
            with pytest.raises(RiskViolation):
                validate_open_request({**request, key: 0.0}, p)
        with pytest.raises(RiskViolation):
            validate_open_request({**request, "volume": p.volume * 2}, p)
        with pytest.raises(RiskViolation):
            validate_open_request({**request, "sl": p.sl - 0.001}, p)  # wider than planned

    @pytest.mark.parametrize("side,current,new,allowed", [
        ("BUY", 1.09, 1.095, True),     # tighten
        ("BUY", 1.09, 1.08, False),     # widen
        ("BUY", 1.09, 0.0, False),      # remove
        ("SELL", 1.11, 1.105, True),
        ("SELL", 1.11, 1.12, False),
        ("BUY", 0.0, 1.09, True),       # repair an unprotected position
    ])
    def test_sl_can_only_be_added_or_tightened(self, side: str, current: float, new: float, allowed: bool) -> None:
        if allowed:
            validate_sl_change(side, current, new)
        else:
            with pytest.raises(RiskViolation):
                validate_sl_change(side, current, new)

    def test_spread_limit(self, manager: RiskManager) -> None:
        ok, reason = manager.approve_entry(ctx(plan(), spread_points=50.0, spread_limit_points=20.0,
                                               spread_limit_reason="cost vs stop"))
        assert not ok and "spread" in reason and "cost vs stop" in reason
        assert manager.approve_entry(ctx(plan(), spread_points=15.0, spread_limit_points=20.0))[0]

    def test_min_lot_exception_needs_an_explicit_cap(self, manager: RiskManager) -> None:
        exc = dataclasses.replace(plan(volume=0.01, loss_per_lot=210.0, risk_pct=0.021), exception=True)
        assert not manager.approve_entry(ctx(exc, allowed_risk_pct=0.01))[0]          # no cap: refused
        assert manager.approve_entry(ctx(exc, allowed_risk_pct=0.01, exception_cap_pct=0.05))[0]
        assert not manager.approve_entry(ctx(exc, allowed_risk_pct=0.01, exception_cap_pct=0.02))[0]

    def test_slippage_pause(self, manager: RiskManager) -> None:
        ok, reason = manager.approve_entry(ctx(plan(), slippage_paused=True))
        assert not ok and "slippage" in reason

    def test_news_window(self, manager: RiskManager) -> None:
        cal = NewsCalendar([(1_000_000.0, "USD", "NFP")])
        blocked = cal.blocked({"EUR", "USD"}, 1_000_000.0 - 600, 30, 30)
        assert blocked and "NFP" in blocked
        assert cal.blocked({"EUR", "USD"}, 1_000_000.0 - 3600, 30, 30) is None
        assert cal.blocked({"GBP", "JPY"}, 1_000_000.0, 30, 30) is None
        ok, reason = manager.approve_entry(ctx(plan(), news_blocked=blocked))
        assert not ok and "news" in reason

    def test_news_csv(self, tmp_path: Path) -> None:
        path = tmp_path / "news.csv"
        path.write_text("time_utc,currency,impact,title\n2026-10-30T12:30:00Z,USD,high,NFP\n", encoding="utf-8")
        cal = NewsCalendar.load(path)
        assert cal.events[0][1] == "USD"

    def test_halted_blocks_everything(self, manager: RiskManager) -> None:
        ok, reason = manager.approve_entry(ctx(plan(), halted_reason="daily loss limit"))
        assert not ok and "halted" in reason

    def test_suggested_mults_outside_bounds_rejected(self) -> None:
        result = plan_trade("BUY", 0.001, limits.MAX_SL_ATR_MULT + 1, 3.0, 1.1, 10, EURUSD_SPEC, 10_000, 0.5, 0, 10)
        assert result.plan is None and "hard bounds" in result.reason


class TestDrawdownCircuitBreaker:
    def test_trip_resume_cycle(self, tmp_path: Path) -> None:
        guard = EquityGuard(tmp_path)
        guard.update("d1", 10_000, 5.0, 12.0)
        guard.update("d2", 11_000, 5.0, 12.0)
        assert guard.state.peak_equity == 11_000
        events = guard.update("d3", 9_660, 5.0, 12.0)  # -12.18% from the peak
        assert "drawdown_trip" in events
        assert guard.halted_reason and "RESUME" in guard.halted_reason
        assert guard.marker.exists()
        # A new day does NOT clear it, and neither does a restart.
        guard.update("d4", 9_700, 5.0, 12.0)
        assert EquityGuard(tmp_path).state.dd_halted
        # Only a human: --resume deletes the marker; the peak restarts at current equity.
        assert request_resume(tmp_path) is True
        events = guard.update("d4", 9_700, 5.0, 12.0)
        assert "resumed" in events and guard.halted_reason is None
        assert guard.state.peak_equity == 9_700
        assert request_resume(tmp_path) is False

    def test_cash_flow_does_not_trip(self, tmp_path: Path) -> None:
        guard = EquityGuard(tmp_path)
        guard.update("d1", 10_000, 5.0, 12.0)
        guard.adjust_for_cash_flow(-3_000)  # withdrawal
        assert "drawdown_trip" not in guard.update("d1", 7_000, 5.0, 12.0)
        assert "daily_limit" not in guard.update("d1", 7_000, 5.0, 12.0)

    def test_bot_closes_everything_and_halts(self, make_bot, fake_mt5: FakeMT5) -> None:
        mine = fake_mt5.add_position("EURUSD", "BUY", magic=510001, sl=1.09, tp=1.12)
        bot = make_bot(risk_profile="aggressive", max_daily_loss_pct_demo=5.0, max_drawdown_from_peak_pct=3.0)
        bot.startup()
        bot.run_once()
        fake_mt5.account.equity = 9_650.0  # -3.5%: drawdown breaker trips, daily limit (5%) does not
        bot.run_once()
        assert bot.guard.state.dd_halted and bot.guard.marker.exists()
        assert any(r.get("position") == mine.ticket for r in fake_mt5.sends)
        sends = len(fake_mt5.sends)
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(fake_mt5.sends) == sends  # no entries while halted
        bot.guard.marker.unlink()
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert not bot.guard.state.dd_halted
        assert len(fake_mt5.sends) == sends + 1  # trading resumed

    def test_strategy_cannot_bypass_breaker(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = make_bot(source=FixedSource("BUY"), max_daily_loss_pct_demo=5.0, max_drawdown_from_peak_pct=5.0)
        bot.startup()
        bot.run_once()
        fake_mt5.account.equity = 9_000.0
        for _ in range(3):
            fake_mt5.advance_bar("EURUSD")
            bot.run_once()
        assert [r for r in fake_mt5.sends if r.get("sl")] == []
