"""Backtester: hand-computed P&L with spread, commission, slippage and the pessimistic
same-bar SL/TP rule; and proof that backtest, shadow and live share the sizing code."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from conftest import EURUSD_SPEC, RATES_DTYPE, FakeMT5, synthetic_bars
from mt5bot import backtest, bot, engine, risk
from mt5bot.backtest import CostModel, compute_metrics, rollover_nights, simulate
from mt5bot.strategies.base import SignalArrays

DAY = 86400
T0 = 20_000 * DAY + 8 * 3600  # a Monday? 20000 days after epoch is a Thursday; hours 08:00


def bars_from(rows: list[tuple[float, float, float, float]], spread: int = 10, start: int = T0,
              step: int = 3600) -> np.ndarray:
    out = np.zeros(len(rows), dtype=RATES_DTYPE)
    for i, (o, h, lo, c) in enumerate(rows):
        out[i] = (start + i * step, o, h, lo, c, 100, spread, 0)
    return out


def one_signal(n: int, index: int, side: int, atr: float = 0.0010, sl: float = 1.0, tp: float = 2.0) -> SignalArrays:
    sides = np.zeros(n, dtype=np.int8)
    sides[index] = side
    return SignalArrays(sides, np.full(n, atr), sl, tp)


COSTS = CostModel(commission_per_lot=7.0, slippage_points=2.0, max_spread_points=30.0, trading_hours=(0, 24))
FLAT = (1.10000, 1.10005, 1.09995, 1.10000)


class TestHandComputed:
    def test_buy_take_profit(self) -> None:
        # Signal on bar 1 -> entry at bar 2 open: ask 1.10010, fill 1.10012 (2 points slippage).
        # SL 100 points below the ask quote = 1.09910, TP 200 points above = 1.10210.
        # Loss per lot = (1.10012 - 1.09910) * 100000 + 7 commission = 109 -> 50 / 109 = 0.45 lots.
        bars = bars_from([FLAT, FLAT, FLAT, (1.10000, 1.10300, 1.09950, 1.10250)])
        [t] = simulate(bars, one_signal(4, 1, +1), EURUSD_SPEC, COSTS, 10_000, 0.5)
        assert (t.entry_index, t.exit_index, t.exit_reason) == (2, 3, "tp")
        assert t.entry_price == pytest.approx(1.10012)
        assert (t.sl, t.tp) == (pytest.approx(1.09910), pytest.approx(1.10210))
        assert t.volume == 0.45
        assert t.risk_money == pytest.approx(0.45 * 109)
        assert t.pnl == pytest.approx(198 * 0.45 - 7 * 0.45)  # 85.95
        assert t.r == pytest.approx(85.95 / 49.05)

    def test_same_bar_sl_and_tp_assumes_sl_first(self) -> None:
        bars = bars_from([FLAT, FLAT, FLAT, (1.10000, 1.10300, 1.09800, 1.10000)])
        [t] = simulate(bars, one_signal(4, 1, +1), EURUSD_SPEC, COSTS, 10_000, 0.5)
        assert t.exit_reason == "sl"
        assert t.exit_price == pytest.approx(1.09910 - 0.00002)  # SL minus slippage
        assert t.pnl == pytest.approx(-104 * 0.45 - 3.15)
        assert t.r == pytest.approx((-104 * 0.45 - 3.15) / 49.05)

    def test_sell_uses_ask_for_exits(self) -> None:
        # SELL at bid open 1.10000 - 2 slippage = 1.09998. SL 1.10100, TP 1.09800 (vs the bid quote).
        # Exits trigger on ask = bid + 10 points: TP needs bid low <= 1.09790.
        bars = bars_from([FLAT, FLAT, FLAT, (1.10000, 1.10010, 1.09795, 1.09800), (1.098, 1.098, 1.0978, 1.0979)])
        [t] = simulate(bars, one_signal(5, 1, -1), EURUSD_SPEC, COSTS, 10_000, 0.5)
        assert t.exit_index == 4 and t.exit_reason == "tp"
        assert t.entry_price == pytest.approx(1.09998)
        # loss per lot = (1.10100 - 1.09998) * 1e5 + 7 = 109 -> 0.45 lots
        assert t.volume == 0.45
        assert t.pnl == pytest.approx((1.09998 - 1.09800) * 1e5 * 0.45 - 3.15)

    def test_gap_through_stop_fills_at_open(self) -> None:
        bars = bars_from([FLAT, FLAT, FLAT, (1.09800, 1.09850, 1.09700, 1.09750)])
        [t] = simulate(bars, one_signal(4, 1, +1), EURUSD_SPEC, COSTS, 10_000, 0.5)
        assert t.exit_reason == "sl_gap"
        assert t.exit_price == pytest.approx(1.09800 - 0.00002)
        assert t.r < -1.0  # gaps can lose more than 1R

    def test_spread_filter_blocks_entry(self) -> None:
        bars = bars_from([FLAT, FLAT, FLAT, FLAT], spread=50)
        assert simulate(bars, one_signal(4, 1, +1), EURUSD_SPEC, COSTS, 10_000, 0.5) == []

    def test_trading_hours_filter(self) -> None:
        costs = dataclasses.replace(COSTS, trading_hours=(12, 20))
        bars = bars_from([FLAT, FLAT, FLAT, FLAT])  # entry at 10:00 server
        assert simulate(bars, one_signal(4, 1, +1), EURUSD_SPEC, costs, 10_000, 0.5) == []

    def test_open_position_closed_at_window_end(self) -> None:
        bars = bars_from([FLAT] * 6)
        [t] = simulate(bars, one_signal(6, 1, +1), EURUSD_SPEC, COSTS, 10_000, 0.5)
        assert t.exit_reason == "window_end" and t.exit_index == 5
        assert t.exit_price == pytest.approx(1.10000 - 0.00002)

    def test_opposite_signal_reverses_at_next_open(self) -> None:
        sides = np.zeros(8, dtype=np.int8)
        sides[1], sides[3] = 1, -1
        sig = SignalArrays(sides, np.full(8, 0.0010), 1.0, 2.0)
        trades = simulate(bars_from([FLAT] * 8), sig, EURUSD_SPEC, COSTS, 10_000, 0.5)
        assert [(t.side, t.entry_index, t.exit_reason) for t in trades] == [
            ("BUY", 2, "opposite"), ("SELL", 4, "window_end")]
        assert trades[0].exit_index == 4

    def test_swap_charged_per_rollover(self) -> None:
        spec = dataclasses.replace(EURUSD_SPEC, swap_long=-5.0)
        bars = bars_from([FLAT] * 30, step=3600)
        [t] = simulate(bars, one_signal(30, 1, +1), spec, COSTS, 10_000, 0.5)
        nights = rollover_nights(t.entry_time, t.exit_time, spec.swap_3day)
        assert nights >= 1
        assert t.swap == pytest.approx(5 * 1e-5 * 1e5 * t.volume * nights)

    def test_rollover_nights_triple_wednesday(self) -> None:
        # 1970-01-07 was a Wednesday (MQL weekday 3).
        wed = 6 * DAY + 12 * 3600
        assert rollover_nights(wed, wed + DAY, 3) == 3
        assert rollover_nights(wed + DAY, wed + 2 * DAY, 3) == 1
        assert rollover_nights(wed + 2 * DAY, wed + 3 * DAY, 3) == 1  # Friday rollover
        assert rollover_nights(wed + 3 * DAY, wed + 4 * DAY, 3) == 0  # Saturday rollover not charged


class TestSharedCode:
    def test_backtest_shadow_and_live_use_the_same_sizing_function(self) -> None:
        assert backtest.plan_trade is risk.plan_trade
        assert bot.plan_trade is risk.plan_trade
        assert engine.open_position is backtest.open_position
        assert engine.exit_for_bar is backtest.exit_for_bar

    def test_live_and_backtest_size_identically(self, make_bot, fake_mt5: FakeMT5) -> None:
        from conftest import FixedSource

        b = make_bot(source=FixedSource("BUY", 0.0010), commission_per_lot=7.0)
        b.startup()
        b.run_once()
        fake_mt5.advance_bar("EURUSD")
        b.run_once()
        [req] = [r for r in fake_mt5.sends if r.get("sl")]
        tick = fake_mt5.ticks["EURUSD"]
        expected = risk.plan_trade("BUY", 0.0010, 2.0, 3.0, tick.ask, 10, EURUSD_SPEC, 10_000, 0.5, 7.0, 10).plan
        assert req["volume"] == expected.volume and req["sl"] == expected.sl and req["tp"] == expected.tp


class TestMetrics:
    def test_known_trades(self) -> None:
        mk = lambda r, pnl, i: backtest.Trade("BUY", i, i + 1, T0 + i * DAY, T0 + i * DAY + 3600, 1, 1, 0, 2, 1,
                                               pnl, 50, r, 0, 0, "tp")
        trades = [mk(2.0, 100, 0), mk(-1.0, -50, 1), mk(-1.0, -50, 2), mk(1.5, 75, 3)]
        times = np.array([T0 + i * 3600 for i in range(24 * 5)])
        m = compute_metrics(trades, 10_000, times, 0, len(times), 3600)
        assert m["trades"] == 4 and m["win_rate"] == 0.5
        assert m["expectancy_r"] == pytest.approx(0.375)
        assert m["profit_factor"] == pytest.approx(3.5 / 2.0)
        assert m["max_drawdown_r"] == pytest.approx(2.0)
        assert m["longest_losing_streak"] == 2
        assert m["net_profit"] == pytest.approx(75)

    def test_deterministic(self) -> None:
        from mt5bot import strategies as S

        bars = synthetic_bars(3000, seed=5, trend=0.1)
        strat = S.get("donchian")
        sig = strat.signals(bars, strat.default_params())
        a = simulate(bars, sig, EURUSD_SPEC, COSTS, 10_000, 0.5)
        b = simulate(bars, sig, EURUSD_SPEC, COSTS, 10_000, 0.5)
        assert a == b and len(a) > 10
