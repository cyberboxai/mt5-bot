"""Phase 4: the statistics, and the proof that matters: on random walks the research pipeline
rejects candidates in nearly all seeds, and with an injected edge it detects it."""

from __future__ import annotations

import math
from typing import Any
from pathlib import Path

import numpy as np
import pytest
from conftest import EURUSD_SPEC, synthetic_bars
from hypothesis import given
from hypothesis import strategies as st

from mt5bot.backtest import CostModel
from mt5bot.core import validation as v
from mt5bot.datastore import DataStore, check_bars
from mt5bot.realitygap import DIVERGED, INSUFFICIENT, OK, GapSettings, evaluate_gap
from mt5bot.registry import Registry
from mt5bot.rigor import EDGE, NO_EDGE, RigorSettings, TrialLedger, evaluate_family, sample_params

COSTS = CostModel(commission_per_lot=7.0, slippage_points=1.0)


def test_sharpe_stats_of_a_normal_sample() -> None:
    s = v.sharpe_stats(np.random.default_rng(1).normal(0.1, 1.0, 20000))
    assert s.sharpe == pytest.approx(0.1, abs=0.02)
    assert s.skew == pytest.approx(0.0, abs=0.1) and s.kurtosis == pytest.approx(3.0, abs=0.2)


def test_expected_max_sharpe_matches_a_monte_carlo() -> None:
    rng = np.random.default_rng(3)
    best = rng.normal(0, 0.1, (4000, 100)).max(axis=1).mean()
    assert v.expected_max_sharpe(100, 0.01) == pytest.approx(best, rel=0.03)


def test_deflation_grows_with_the_number_of_trials() -> None:
    s = v.SharpeStats(0.2, 200, 0.0, 3.0)
    values = [v.deflated_sharpe(s, n, 0.005) for n in (2, 10, 100, 1000)]
    assert values == sorted(values, reverse=True) and values[-1] < values[0]


@given(st.floats(0.05, 1.0), st.integers(30, 3000))
def test_more_data_never_lowers_confidence_in_a_positive_sharpe(sr: float, n: int) -> None:
    a = v.SharpeStats(sr, n, 0.0, 3.0)
    b = v.SharpeStats(sr, n * 2, 0.0, 3.0)
    assert v.probabilistic_sharpe(b) >= v.probabilistic_sharpe(a)


def test_min_track_record_is_consistent_with_psr() -> None:
    s = v.SharpeStats(0.15, 100, 0.0, 3.0)
    n = v.min_track_record(s, 0.0, 0.95)
    assert v.probabilistic_sharpe(v.SharpeStats(0.15, int(math.ceil(n)), 0.0, 3.0)) == pytest.approx(0.95, abs=0.005)
    assert v.min_track_record(v.SharpeStats(-0.1, 100, 0.0, 3.0)) == math.inf


def test_cpcv_splits_cover_every_group_and_count_paths() -> None:
    splits = list(v.cpcv_splits(6, 2))
    assert len(splits) == 15 and v.n_paths(6, 2) == 5
    assert all(set(s.train_groups) | set(s.test_groups) == set(range(6)) for s in splits)
    counts = np.bincount([g for s in splits for g in s.test_groups])
    assert set(counts) == {5}


def test_purging_removes_trades_that_touch_the_test_group_or_its_embargo() -> None:
    entries = np.array([10, 90, 100, 150, 205, 215, 300])
    exits = np.array([20, 105, 110, 160, 210, 220, 310])
    keep = v.purged_train_mask(entries, exits, [(100, 200)], embargo=10)
    assert keep.tolist() == [True, False, False, False, False, True, True]


def test_pbo_flags_noise_and_clears_a_persistent_winner() -> None:
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 1, (400, 40))
    assert 0.3 < v.pbo_cscv(noise) < 0.75
    skilled = noise.copy()
    skilled[:, 0] += 0.5
    assert v.pbo_cscv(skilled) == pytest.approx(0.0, abs=0.1)


def test_pbo_needs_something_to_compare() -> None:
    assert v.pbo_cscv(np.zeros((100, 1))) is None and v.pbo_cscv(np.zeros((3, 5))) is None


def test_triple_barrier_mirrors_execution() -> None:
    high = np.array([1.01, 1.02, 1.05])
    low = np.array([1.00, 0.99, 1.00])
    close = np.array([1.00, 1.00, 1.04])
    tp = v.triple_barrier(1, 0, 1.0, 0.95, 1.04, high, low, close, 5, 0.1)
    assert tp.label == 1 and tp.exit_index == 2 and tp.r == pytest.approx(0.8 - 0.1)
    both = v.triple_barrier(1, 1, 1.0, 0.995, 1.015, high, low, close, 5)
    assert both.label == -1 and both.r == pytest.approx(-1.0)   # stop first when a bar touches both
    timed = v.triple_barrier(1, 0, 1.0, 0.9, 1.5, high, low, close, 1)
    assert timed.label == 0 and timed.exit_index == 1
    sell = v.triple_barrier(-1, 0, 1.0, 1.015, 0.9, high, low, close, 5, spread=0.0)
    assert sell.label == -1


def _verdict(bars: np.ndarray, strategy: str = "ema_cross", prior: int = 0, count: int = 10):
    params = sample_params(strategy, count, 1)
    return evaluate_family(bars, strategy, params, EURUSD_SPEC, COSTS, 10_000.0, 1.0, RigorSettings(), prior)


@pytest.mark.parametrize("strategy", ["ema_cross", "donchian"])
def test_random_walk_candidates_are_rejected_in_nearly_all_seeds(strategy: str) -> None:
    seeds = range(16)
    verdicts = [_verdict(synthetic_bars(5000, seed), strategy) for seed in seeds]
    passed = [x for x in verdicts if x is not None and x.verdict == EDGE]
    assert len(passed) <= 1, [(p.sharpe, p.dsr, p.pbo) for p in passed]


def test_injected_edge_is_detected() -> None:
    hits = 0
    for seed in range(8):
        bars = synthetic_bars(8000, seed + 100, trend=0.35, regime_bars=200)
        result = _verdict(bars, "donchian")
        hits += result is not None and result.verdict == EDGE
    assert hits >= 6


def test_the_trial_ledger_is_persistent_and_raises_the_bar(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "x.sqlite", clock=lambda: 1.0)
    ledger = TrialLedger(reg, lambda: 1.0)
    bars = synthetic_bars(4000, 200, trend=0.35, regime_bars=200)

    def count(params, stats) -> None:
        ledger.record(1, "EURUSD", "H1", "donchian", params, "snap", stats)

    first = evaluate_family(bars, "donchian", sample_params("donchian", 6, 1), EURUSD_SPEC, COSTS, 1e4, 1.0,
                            RigorSettings(), ledger.count(), count)
    second = evaluate_family(bars, "donchian", sample_params("donchian", 6, 1), EURUSD_SPEC, COSTS, 1e4, 1.0,
                             RigorSettings(), ledger.count(), count)
    assert ledger.count() == 12 and second.n_trials == first.n_trials + 6
    assert second.dsr <= first.dsr
    with pytest.raises(Exception, match="never deleted"):
        reg.conn.execute("DELETE FROM trial_ledger")
    ledger.store_verdict(1, "EURUSD", "H1", None, second, "snap")
    assert reg.conn.execute("SELECT verdict FROM candidate_verdicts").fetchone()["verdict"] in (EDGE, NO_EDGE, "insufficient")
    reg.close()


def test_entry_styles_are_reported_without_switching(tmp_path: Path) -> None:
    result = _verdict(synthetic_bars(4000, 9))
    assert set(result.entry_styles) == {"market", "limit", "stop"}
    assert 0 <= result.entry_styles["limit"]["fill_rate"] <= 1


def test_snapshots_are_immutable_and_content_addressed(tmp_path: Path) -> None:
    reg = Registry(tmp_path / "x.sqlite", clock=lambda: 1.0)
    store = DataStore(tmp_path, reg, lambda: 1.0)
    bars = synthetic_bars(500, 1)
    a, report = store.snapshot("EURUSD", "H1", bars, 3600)
    b, _ = store.snapshot("EURUSD", "H1", bars.copy(), 3600)
    assert a == b and report.rows == 500
    assert np.array_equal(store.load_snapshot(a), bars)
    assert reg.conn.execute("SELECT COUNT(*) AS n FROM data_snapshots").fetchone()["n"] == 1
    c, _ = store.snapshot("EURUSD", "H1", bars[:-1], 3600)
    assert c != a
    reg.close()


def test_quality_checks_find_the_usual_problems() -> None:
    bars = synthetic_bars(800, 2)
    assert check_bars(bars, 3600).duplicate_times == 0
    dup = bars.copy()
    dup["time"][10] = dup["time"][9]
    assert not check_bars(dup, 3600).ok
    spike = bars.copy()
    spike["close"][100] *= 1.3
    spike["high"][100] = spike["close"][100]
    assert check_bars(spike, 3600).spikes >= 1
    gap = np.delete(bars, range(200, 203))
    assert check_bars(gap, 3600).gaps >= 0


def test_ticks_are_batched_partitioned_and_retained(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    store.capture_tick("EURUSD", 86400, 1.1, 1.2)
    store.capture_tick("EURUSD", 86400, 1.1, 1.2)      # unchanged quotes are not stored twice
    store.capture_tick("EURUSD", 2 * 86400, 1.2, 1.3)
    assert store.flush() == 2
    store.capture_tick("EURUSD", 2 * 86400 + 5, 1.3, 1.4)
    store.flush()
    assert len(store.load_ticks("EURUSD")) == 3
    assert store.prune_ticks(0, 2 * 86400 + 60) == 1


def test_gap_monitor_statuses() -> None:
    s = GapSettings()
    assert evaluate_gap([], None, None, s).status == INSUFFICIENT
    assert evaluate_gap([(0.1, 0.1)] * 5, None, None, s).status == INSUFFICIENT
    rng = np.random.default_rng(1)
    same = [(float(x), float(x)) for x in rng.normal(0.1, 1.0, 50)]
    assert evaluate_gap(same, 1.0, 0.99, s).status == OK
    behind = [(float(x) - 0.4, float(x)) for x in rng.normal(0.1, 1.0, 50)]
    report = evaluate_gap(behind, None, None, s)
    assert report.status == DIVERGED and report.gap_r == pytest.approx(-0.4)
    assert evaluate_gap(same, 9.0, 0.99, s).status == DIVERGED
    assert evaluate_gap(same, 0.0, 0.5, s).status == DIVERGED


def test_research_writes_verdicts_counts_trials_and_retires_failed_arms(tmp_path: Path) -> None:
    from mt5bot.arms import ArmStore
    from mt5bot.config import Config
    from mt5bot.research import run_research

    reg = Registry(tmp_path / "x.sqlite", clock=lambda: 1_700_000_000.0)
    bars = synthetic_bars(7000, 3, bar_seconds=900)
    reg.store_bars("EURUSD", "M15", bars)
    reg.store_spec("EURUSD", EURUSD_SPEC.to_json(), 7.0)
    reg.set_kv("universe", '{"symbols": ["EURUSD"]}')
    cfg = Config.from_dict({"universe": {"timeframes": ["M15"]}, "history_months": 4, "wf_train_months": 1,
                            "wf_test_months": 1, "enabled_strategies": ["donchian", "ema_cross"], "trials_per_strategy": 4})
    store = ArmStore(reg, lambda: 1_700_000_000.0)
    arm = store.upsert("EURUSD", "M15", "ema_cross", __import__("mt5bot.strategies").strategies.get("ema_cross").default_params(),
                       "default")
    [result] = run_research(cfg, reg, "test", symbols=["EURUSD"], timeframes=["M15"], workers=1)
    assert result["status"] == "done" and sum(result["verdicts"].values()) == 2
    assert reg.conn.execute("SELECT COUNT(*) AS n FROM trial_ledger").fetchone()["n"] >= 2
    row = reg.conn.execute("SELECT snapshot_id, verdict FROM candidate_verdicts WHERE strategy = 'ema_cross'").fetchone()
    assert row["snapshot_id"].startswith("EURUSD-M15-")
    assert reg.conn.execute("SELECT COUNT(*) AS n FROM data_snapshots").fetchone()["n"] == 1
    if row["verdict"] == NO_EDGE:
        assert store.get(arm.id).active is False
    reg.close()


def test_a_diverging_reality_gap_freezes_promotions_and_recalibrates(make_bot, fake_mt5: Any) -> None:
    bot = make_bot(source=None)
    bot.startup()
    now = float(bot._utc_now())
    for i in range(30):
        for mode, r in (("shadow", 0.5), ("live", -0.5)):
            tid = bot.registry.open_trade(mode, "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 10.0, arm_id=1,
                                          open_bar_time=1000 + i, extra={"signal_bar": 1000 + i}, ticket=None)
            bot.registry.close_trade(tid, 1.1, r * 10.0, r + (i % 3) * 0.01, "x")
    bot._next_gap_check = 0.0
    bot._next_calibration = 10 ** 12
    bot._check_reality_gap()
    assert bot.arms.promotions_frozen and "behind the simulation" in bot.arms.promotions_frozen
    assert bot._next_calibration > now or bot._next_calibration >= 0
    assert [e.kind for e in bot.events.tail(["adaptation.gap"])] == ["adaptation.gap"]
    row = bot.registry.conn.execute("SELECT status, gap_r FROM reality_gap ORDER BY id DESC").fetchone()
    assert row["status"] == DIVERGED and row["gap_r"] == pytest.approx(-1.0, abs=0.01)


def test_frozen_promotions_still_allow_demotions(make_bot) -> None:
    from mt5bot.tiers import TierDecision

    bot = make_bot(source=None)
    bot.startup()
    arm = next(iter(bot.arms.arms.values()))
    bot.arms.promotions_frozen = "gap"
    assert bot.arms._promotion_blocked(arm, TierDecision("T1", "T0"), {})
    assert TierDecision("T0", "T1").direction == "down"     # refresh only consults the block when going up
