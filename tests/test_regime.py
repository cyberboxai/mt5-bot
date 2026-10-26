"""Regime labels (causal, shared by the office and the arms) and per-regime arm evidence."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from numpy.lib.stride_tricks import sliding_window_view

from conftest import EURUSD_SPEC, TEST_COSTS, synthetic_bars
from mt5bot import regime
from mt5bot.config import Config
from mt5bot.registry import Registry


@pytest.mark.parametrize("window", [4, 5, 200])
def test_rolling_median_matches_numpy(window: int) -> None:
    x = np.random.default_rng(window).integers(1, 500, 1200).astype(float)
    out = regime._rolling_median(x, window)
    assert np.isnan(out[:window - 1]).all()
    assert np.allclose(out[window - 1:], np.median(sliding_window_view(x, window), axis=1))


def test_regime_codes_are_causal_and_match_the_detector() -> None:
    bars = synthetic_bars(2600, seed=5, trend=0.15)
    codes = regime.regime_codes(bars)
    for k in (800, 1400, 2599):
        assert (regime.regime_codes(bars[:k].copy()) == codes[:k]).all()
    for i in range(900, 2600, 100):
        assert regime.REGIME_LABELS[int(codes[i - 1])] == regime.regime_label(bars[:i])
    assert {regime.REGIME_LABELS[int(c)] for c in codes[700:]} >= {"trend", "range"}


def engine(tmp_path: Path):
    from mt5bot.arms import ArmEngine

    cfg = Config()
    return ArmEngine(Registry(tmp_path / "mt5bot.sqlite"), cfg.tiers, cfg.research, management_cfg=cfg.management)


def test_shadow_trades_carry_the_regime_of_their_signal_bar(tmp_path: Path) -> None:
    eng = engine(tmp_path)
    arm = eng.ensure_arm("EURUSD", "H1", "donchian", {"channel": 20, "sl_mult": 1.5, "tp_mult": 2.0}, "test")
    bars = synthetic_bars(2100, seed=11, trend=0.15)
    eng.on_bar("EURUSD", "H1", bars[:200], EURUSD_SPEC, TEST_COSTS)
    eng.on_bar("EURUSD", "H1", bars, EURUSD_SPEC, TEST_COSTS)
    closed = eng.registry.trades(mode="shadow", status="closed", arm_id=arm.id)
    assert closed
    times = bars["time"].astype(np.int64)
    codes = regime.regime_codes(bars)
    for trade in closed:
        i = int(np.searchsorted(times, trade.extra["signal_bar"]))
        assert trade.extra["regime"] == regime.REGIME_LABELS[int(codes[i])]
    eng.registry.close()


def add_trades(eng, arm, label: str, rs: list[float], start_bar: int, close_ts: float = 1_000.0) -> None:
    for k, r in enumerate(rs):
        trade_id = eng.registry.open_trade("shadow", arm.symbol, arm.param_set_id, "BUY", 1.1, 1.09, 1.12, 1.0,
                                           100.0, arm_id=arm.id, timeframe=arm.timeframe, tier="T0",
                                           extra={"signal_bar": start_bar + k * 3600, "regime": label})
        eng.registry.close_trade(trade_id, 1.1, r * 100.0, r, "tp" if r > 0 else "sl", close_ts=close_ts + k)


def test_regime_view_separates_good_and_bad_regimes(tmp_path: Path) -> None:
    eng = engine(tmp_path)
    arm = eng.ensure_arm("EURUSD", "H1", "donchian", {"channel": 20, "sl_mult": 1.5, "tp_mult": 2.0}, "test")
    rng = np.random.default_rng(3)
    add_trades(eng, arm, "trend", list(rng.normal(0.4, 0.8, 40)), 10_000_000)
    add_trades(eng, arm, "range", list(rng.normal(-0.5, 0.8, 40)), 20_000_000, close_ts=5_000.0)
    eng.refresh(force=True)
    good, bad = eng.regime_view(arm, "trend"), eng.regime_view(arm, "range")
    assert good is not None and bad is not None and good.n == bad.n == 40
    assert good.p_positive > 0.8 and bad.p_positive < 0.25
    assert bad.raw_mean < bad.mean  # shrunk toward the arm's overall mean
    assert eng.regime_view(arm, "high_vol") is None
    eng.registry.close()


def test_trades_closed_at_the_same_instant_are_never_lost(tmp_path: Path) -> None:
    """Regression: evidence was read with ``close_ts > last``; a trade closed at the same clock
    value as the last read (virtual clock in the simulator) was skipped forever."""
    eng = engine(tmp_path)
    arm = eng.ensure_arm("EURUSD", "H1", "donchian", {"channel": 20, "sl_mult": 1.5, "tp_mult": 2.0}, "test")
    add_trades(eng, arm, "trend", [1.0], 30_000_000, close_ts=7_000.0)
    eng.posteriors()
    add_trades(eng, arm, "trend", [-1.0], 40_000_000, close_ts=7_000.0)  # same instant as the last read
    eng.posteriors()
    eng.posteriors()  # idempotent: nothing is read twice
    assert len(eng._evidence[arm.id].observations) == 2
    eng.registry.close()
