"""Phase 7: every incremental indicator must equal its batch twin, and the append-only path of
``indicators`` must give the same numbers as the batch path that research uses."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pytest
from conftest import synthetic_bars
from hypothesis import given, settings
from hypothesis import strategies as st

from mt5bot import indicators as ind
from mt5bot import regime
from mt5bot import strategies as lib
from mt5bot.core import incremental as inc
from mt5bot.indicators import SeriesBuffer

PRICES = st.lists(st.floats(1.0, 2.0, allow_nan=False, width=32), min_size=3, max_size=120)


def feed(step: Any, values: Any) -> np.ndarray:
    return np.array([step.update(float(v)) for v in values], dtype=float)


def same(a: np.ndarray, b: np.ndarray, tol: float = 1e-9) -> None:
    assert np.array_equal(np.isnan(a), np.isnan(b))
    assert np.nanmax(np.abs(a - b), initial=0.0) <= tol


ONE_INPUT: list[tuple[str, Callable[[int], Any], Callable[[Any, int], np.ndarray]]] = [
    ("ema", inc.Ema, ind.ema), ("sma", inc.Sma, ind.sma), ("std", inc.RollingStd, ind.rolling_std),
    ("rsi", inc.WilderRsi, ind.rsi), ("maxprev", inc.RollingMaxPrev, ind.rolling_max_prev),
    ("minprev", inc.RollingMinPrev, ind.rolling_min_prev), ("rank", inc.PercentileRank, ind.rolling_percentile_rank),
    ("er", inc.EfficiencyRatio, ind.efficiency_ratio),
]


@pytest.mark.parametrize("name,make,batch", ONE_INPUT, ids=[c[0] for c in ONE_INPUT])
@settings(max_examples=60, deadline=None)
@given(values=PRICES, period=st.integers(2, 30))
def test_one_input_indicator_matches_batch(name: str, make: Any, batch: Any, values: list[float], period: int) -> None:
    same(feed(make(period), values), batch(values, period))


@settings(max_examples=60, deadline=None)
@given(values=PRICES, period=st.integers(2, 20))
def test_atr_and_adx_match_batch(values: list[float], period: int) -> None:
    close = np.array(values)
    high, low = close + 0.01, close - 0.02
    atr = inc.WilderAtr(period)
    adx = inc.WilderAdx(period)
    same(np.array([atr.update(h, lo, c) for h, lo, c in zip(high, low, close)]), ind.atr(high, low, close, period))
    same(np.array([adx.update(h, lo, c) for h, lo, c in zip(high, low, close)]), ind.adx(high, low, close, period), 1e-7)


def test_percentile_rank_handles_nan_like_batch() -> None:
    values = [float("nan")] * 5 + list(np.random.default_rng(1).normal(size=40))
    same(feed(inc.PercentileRank(10), values), ind.rolling_percentile_rank(values, 10))


def grown(bars: np.ndarray, start: int) -> SeriesBuffer:
    buffer = SeriesBuffer(bars[:start], len(bars) + 10)
    return buffer


def test_the_append_only_path_equals_the_batch_path_for_every_bar() -> None:
    bars = synthetic_bars(900, 3)
    buffer = grown(bars, 600)
    for n in range(600, 900):
        view = buffer.view()
        for name, fast, slow in (
                ("ema", ind.ema(view["close"], 20), ind.ema(bars["close"][:n].copy(), 20)),
                ("rsi", ind.rsi(view["close"], 14), ind.rsi(bars["close"][:n].copy(), 14)),
                ("max", ind.rolling_max_prev(view["high"], 30), ind.rolling_max_prev(bars["high"][:n].copy(), 30)),
                ("atr", ind.bars_atr(view, 14), ind.atr(view["high"].copy(), view["low"].copy(), view["close"].copy(), 14)),
        ):
            same(np.asarray(fast), slow, 1e-9)
            assert isinstance(name, str)
        buffer.append(bars[n:n + 1])
    buffer.release()


def test_extended_results_are_read_only_and_only_row_zero_views_extend() -> None:
    bars = synthetic_bars(300, 4)
    buffer = grown(bars, 300)
    out = ind.ema(buffer.view()["close"], 10)
    with pytest.raises(ValueError):
        out[0] = 1.0
    tail = buffer.view()[-100:]
    same(ind.ema(tail["close"], 10), ind.ema(tail["close"].copy(), 10))   # a slice is never extended
    buffer.release()


def test_chained_indicator_on_an_extended_output() -> None:
    bars = synthetic_bars(800, 5)
    buffer = grown(bars, 700)
    for _ in range(100):
        rank = ind.rolling_percentile_rank(ind.bars_atr(buffer.view(), 14), 200)
        buffer.append(bars[buffer.n:buffer.n + 1])
    view = buffer.view()
    fresh = ind.rolling_percentile_rank(ind.atr(view["high"].copy(), view["low"].copy(), view["close"].copy(), 14), 200)
    same(np.asarray(rank), fresh[:len(rank)], 1e-9)
    buffer.release()


@pytest.mark.parametrize("name", sorted(lib.library()))
def test_every_strategy_gives_the_same_signals_on_both_paths(name: str) -> None:
    strat = lib.get(name)
    bars = synthetic_bars(1200, 6, bar_seconds=900)
    params = strat.default_params()
    buffer = grown(bars, 900)
    for _ in range(300):
        fast = strat.signals(buffer.view(), params)
        slow = strat.signals(np.array(buffer.view(), copy=True), params)
        assert np.array_equal(fast.side, slow.side), name
        buffer.append(bars[buffer.n:buffer.n + 1])
    buffer.release()


def test_regime_labels_match_on_both_paths() -> None:
    bars = synthetic_bars(1000, 7)
    buffer = grown(bars, 950)
    for _ in range(40):
        assert regime.regime_codes(buffer.view()).tolist() == regime.regime_codes(np.array(buffer.view(), copy=True)).tolist()
        buffer.append(bars[buffer.n:buffer.n + 1])
    buffer.release()


def test_released_series_leave_nothing_behind() -> None:
    before = len(ind._ROOTS)
    buffer = grown(synthetic_bars(100, 8), 100)
    ind.ema(buffer.view()["close"], 5)
    assert len(ind._ROOTS) > before
    buffer.release()
    assert len(ind._ROOTS) == before
