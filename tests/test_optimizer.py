"""Walk-forward optimiser and acceptance gates.

The key anti-overfitting test: on pure random-walk data (no edge exists, even with ZERO
costs) the optimiser must return NoTrade in the overwhelming majority of seeded runs.
On data with an injected genuine edge (persistent trend regimes) it must find it.
"""

from __future__ import annotations

import math
import os
from multiprocessing import get_context

import numpy as np
import pytest

from conftest import EURUSD_SPEC, TEST_COSTS, synthetic_bars
from mt5bot import strategies as S
from mt5bot.optimizer import (MONTH_SECONDS, Gates, OptimizerSettings, evaluate_strategy, optimize_symbol,
                              trial_plan, walk_forward_windows)
from mt5bot.stats import (bootstrap_mean_interval, composite_score, deflated_sharpe, expected_max_standard_normal,
                          prob_losing_run, streak_limit)
from optimizer_cases import run_case

RANDOM_WALK_SEEDS = 12
RANDOM_WALK_TOLERANCE = 1  # at most 1 of 12 runs may pass (stated tolerance: >= 91.7% NoTrade)
WORKERS = max(1, min(4, os.cpu_count() or 1))


def _pool_map(cases: list) -> list[dict]:
    if WORKERS == 1:
        return [run_case(c) for c in cases]
    with get_context("spawn").Pool(WORKERS) as pool:
        return pool.map(run_case, cases)


@pytest.fixture(scope="module")
def random_walk_results() -> list[dict]:
    cases = [(seed, 0.0, True) for seed in range(RANDOM_WALK_SEEDS)]
    cases += [(seed, 0.0, False) for seed in range(100, 104)]
    return _pool_map(cases)


def test_random_walk_returns_notrade_in_nearly_all_seeds(random_walk_results: list[dict]) -> None:
    zero_cost = [r for r in random_walk_results if r["seed"] < 100]
    passed = [r for r in zero_cost if r["best"] is not None]
    assert len(passed) <= RANDOM_WALK_TOLERANCE, f"optimiser found an 'edge' in noise: {passed}"
    # Not vacuous: candidates did produce enough out-of-sample trades to be judged on statistics.
    assert sum(1 for r in zero_cost if r["with_enough_trades"] > 0) >= RANDOM_WALK_SEEDS // 2
    assert all(r["trials"] > 0 for r in zero_cost)


def test_random_walk_with_costs_returns_notrade(random_walk_results: list[dict]) -> None:
    costly = [r for r in random_walk_results if r["seed"] >= 100]
    assert all(r["best"] is None for r in costly)


def test_injected_edge_is_found() -> None:
    results = _pool_map([(seed, 0.15, False) for seed in range(3)])
    for r in results:
        assert r["best"] in ("donchian", "volfilter_donchian", "ema_cross", "volfilter_ema_cross"), r


def test_deterministic_for_a_seed() -> None:
    bars = synthetic_bars(24 * 200, seed=3, trend=0.1)
    settings = OptimizerSettings(train_months=2, bar_seconds=3600, trials_per_strategy=4,
                                 gates=Gates(bootstrap_samples=300))
    a = evaluate_strategy(S.get("donchian"), bars, EURUSD_SPEC, TEST_COSTS, settings, 4, 4, "EURUSD")
    b = evaluate_strategy(S.get("donchian"), bars, EURUSD_SPEC, TEST_COSTS, settings, 4, 4, "EURUSD")
    assert a.to_dict() == b.to_dict()


def test_parallel_workers_give_identical_results() -> None:
    bars = synthetic_bars(24 * 200, seed=4, trend=0.1)
    settings = OptimizerSettings(train_months=2, bar_seconds=3600, trials_per_strategy=3,
                                 gates=Gates(bootstrap_samples=300))
    names = ["donchian", "ema_cross"]
    serial = optimize_symbol("EURUSD", bars, EURUSD_SPEC, TEST_COSTS, settings, names, workers=1)
    parallel = optimize_symbol("EURUSD", bars, EURUSD_SPEC, TEST_COSTS, settings, names, workers=2)
    assert [c.to_dict() for c in serial.candidates] == [c.to_dict() for c in parallel.candidates]


def test_short_history_fails_cleanly() -> None:
    bars = synthetic_bars(500, seed=1)
    settings = OptimizerSettings(bar_seconds=3600, gates=Gates(bootstrap_samples=300))
    result = optimize_symbol("EURUSD", bars, EURUSD_SPEC, TEST_COSTS, settings, ["donchian"])
    assert result.best is None
    assert "insufficient history" in result.candidates[0].reasons[0]


def test_trial_cap_and_count() -> None:
    settings = OptimizerSettings(trials_per_strategy=100, max_trials_per_cycle=70)
    plan = trial_plan(S.library_names(), settings)
    assert sum(plan.values()) <= 70
    assert all(v >= 1 for v in plan.values())


def test_walk_forward_windows_are_rolling_and_unseen() -> None:
    times = np.arange(0, 12 * MONTH_SECONDS, 3600)
    windows = walk_forward_windows(times, 3, 1, 1)
    assert len(windows) == 9
    for tr_s, tr_e, te_s, te_e in windows:
        assert tr_s < tr_e == te_s < te_e  # test starts exactly where train ends
    assert all(b[2] - a[2] > 0 for a, b in zip(windows, windows[1:]))


def test_train_selection_never_sees_test_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Corrupting every bar of a test window must not change which parameters were chosen."""
    bars = synthetic_bars(24 * 200, seed=8, trend=0.1)
    settings = OptimizerSettings(train_months=2, bar_seconds=3600, trials_per_strategy=5,
                                 gates=Gates(bootstrap_samples=300))
    base = evaluate_strategy(S.get("donchian"), bars, EURUSD_SPEC, TEST_COSTS, settings, 5, 5, "EURUSD")
    windows = walk_forward_windows(bars["time"], 2, 1, 1)
    _, _, te_s, te_e = windows[0]
    corrupted = bars.copy()
    rng = np.random.default_rng(0)
    corrupted["close"][te_s:] *= np.exp(rng.normal(0, 0.01, len(corrupted) - te_s))
    corrupted["open"][te_s:] = corrupted["close"][te_s:]
    corrupted["high"][te_s:] = corrupted["close"][te_s:] * 1.001
    corrupted["low"][te_s:] = corrupted["close"][te_s:] * 0.999
    other = evaluate_strategy(S.get("donchian"), corrupted, EURUSD_SPEC, TEST_COSTS, settings, 5, 5, "EURUSD")
    assert base.metrics["windows_chosen"][0] == other.metrics["windows_chosen"][0]


class TestStats:
    def test_bootstrap_interval_contains_mean_and_is_seeded(self) -> None:
        x = np.random.default_rng(0).normal(0.2, 1.0, 400)
        lo, hi = bootstrap_mean_interval(x, 0.9, 1000, seed=1)
        assert lo < x.mean() < hi
        assert (lo, hi) == bootstrap_mean_interval(x, 0.9, 1000, seed=1)

    def test_deflated_sharpe_penalises_trials(self) -> None:
        x = np.random.default_rng(1).normal(0.15, 1.0, 300)
        assert deflated_sharpe(x, 1) > deflated_sharpe(x, 50) > deflated_sharpe(x, 5000)
        assert expected_max_standard_normal(1) == 0.0
        assert expected_max_standard_normal(100) == pytest.approx(2.5, abs=0.15)

    def test_losing_run_probability(self) -> None:
        assert prob_losing_run(3, 3, 0.5) == pytest.approx(0.125)
        assert prob_losing_run(4, 3, 0.5) == pytest.approx(3 / 16)
        assert prob_losing_run(2, 3, 0.5) == 0.0
        k = streak_limit(100, 0.6, 0.01)
        assert prob_losing_run(100, k, 0.6) < 0.01 <= prob_losing_run(100, k - 1, 0.6)

    def test_composite_score_penalises_variance_and_drawdown(self) -> None:
        stable = composite_score([2, 2, 2, 2], [1, 1, 1, 1], 3, 1.0)
        volatile = composite_score([8, -2, 4, -2], [1, 1, 1, 1], 3, 1.0)
        deep = composite_score([2, 2, 2, 2], [1, 1, 1, 1], 30, 1.0)
        assert stable > volatile and stable > deep and stable > 0
        assert composite_score([-1, -1], [1, 1], 5, 1.0) < 0
        assert math.isclose(composite_score([], [], 0, 1.0), 0.0)
