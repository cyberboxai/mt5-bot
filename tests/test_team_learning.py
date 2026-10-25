"""Learning on synthetic data: weights track real predictive power and do not reward noise,
calibration improves the Brier score, the Meta-Labeler abstains unless validated out of
sample, ablation finds useless agents, auto mode falls back to the baseline, benching."""

from __future__ import annotations

import random
import statistics
from pathlib import Path

import numpy as np
import pytest

from team_helpers import runtime, team_config
from mt5bot.config import Config
from mt5bot.team.ablation import ablation_report, select_mode
from mt5bot.team.agents.metalabel import FEATURES, train
from mt5bot.team.ensemble import EnsembleSettings
from mt5bot.team.learning import Learner, OutcomeVote, brier, fit_platt, logit, sigmoid

NOISE = ("mean_reversion_analyst", "breakout_analyst", "mtf_analyst")
SETTINGS = EnsembleSettings()


def simulate(seed: int, with_predictive: bool, n: int = 400) -> dict[str, float]:
    rng = random.Random(seed)
    learner = Learner(Config().team)
    for _ in range(n):
        won = rng.random() < 0.45
        votes = []
        if with_predictive:
            right = rng.random() < 0.68
            votes.append(OutcomeVote("trend_analyst", "x", won if right else not won, 0.6, 0.6))
        votes += [OutcomeVote(a, "x", rng.random() < 0.5, rng.random(), 0.5) for a in NOISE]
        learner.update("trend", votes, won)
    return learner.weights_for("trend", ("trend_analyst",) + NOISE)


def test_predictive_agent_gains_weight_and_noise_is_not_rewarded_across_seeds() -> None:
    for seed in range(25):
        w = simulate(seed, with_predictive=True)
        assert w["trend_analyst"] > 1.15, seed
        assert all(w["trend_analyst"] > w[a] for a in NOISE), seed
        assert all(w[a] <= 1.15 for a in NOISE), seed


def test_pure_noise_agents_stay_near_uniform_across_seeds() -> None:
    weights = []
    for seed in range(25):
        w = simulate(seed, with_predictive=False)
        weights += [w[a] for a in NOISE]
    assert all(0.7 <= x <= 1.35 for x in weights)
    assert 0.93 <= statistics.mean(weights) <= 1.07


def test_warmup_keeps_uniform_weights() -> None:
    learner = Learner(team_config(warmup_outcomes=50).team)
    for _ in range(49):
        learner.update("trend", [OutcomeVote("trend_analyst", "x", True, 0.9, 0.9),
                                 OutcomeVote("mtf_analyst", "x", False, 0.9, 0.9)], True)
    assert learner.weight("trend_analyst", "trend") == 1.0
    learner.update("trend", [OutcomeVote("trend_analyst", "x", True, 0.9, 0.9)], True)
    assert learner.weight("trend_analyst", "trend") > 1.0


@pytest.mark.parametrize("seed", range(5))
def test_platt_calibration_improves_out_of_sample_brier(seed: int) -> None:
    rng = np.random.default_rng(seed)
    q = rng.uniform(0.75, 0.95, 2000)          # claimed: "my direction is right" with 75-95%
    true_p = 0.5 + (q - 0.5) * 0.3             # reality: only 57-63%
    y = (rng.random(2000) < true_p).astype(float)
    a, b = fit_platt(q[:1000], y[:1000])
    calibrated = sigmoid(a * logit(q[1000:]) + b)
    assert brier(calibrated, y[1000:]) < brier(q[1000:], y[1000:]) - 0.02


def test_learner_calibration_only_kept_if_it_helps() -> None:
    learner = Learner(team_config(calibration_min_samples=50).team)
    rng = random.Random(4)
    for _ in range(300):
        learner.samples["trend_analyst"].append((0.95, int(rng.random() < 0.55)))
    fit = learner.fit_calibration("trend_analyst")
    assert fit is not None and fit["brier_cal"] <= fit["brier_raw"]
    assert learner.calibrate("trend_analyst", 0.9) < 0.5  # an overconfident 0.9 becomes much humbler


def _meta_rows(seed: int, informative: bool, n: int = 400) -> list:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        features = {name: rng.random() for name in FEATURES}
        p = 0.15 + 0.7 * features["regime_trend"] if informative else 0.45
        rows.append((features, int(rng.random() < p)))
    return rows


def test_meta_labeler_validates_real_signal_and_abstains_on_noise() -> None:
    validated_signal = sum(train(_meta_rows(s, True), 0.3, 1.0).validated for s in range(10))
    validated_noise = sum(train(_meta_rows(s, False), 0.3, 1.0).validated for s in range(10))
    assert validated_signal >= 9
    assert validated_noise <= 3
    assert train(_meta_rows(0, True)[:20], 0.3, 1.0) is None  # too little data: no model at all


# ------------------------------------------------------------------- ablation


def _dossiers(seed: int, n: int, predictive_filter: bool) -> list[dict]:
    """Proposals with real R outcomes. A predictive agent warns against losers; a useless
    one votes at random. ``predictive_filter`` decides whether the warning is informative."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        r = rng.choice([1.5, -1.0]) if rng.random() < 0.5 else -1.0 if rng.random() < 0.6 else 1.5
        loser = r < 0
        warn = (loser if rng.random() < 0.8 else not loser) if predictive_filter else rng.random() < 0.5
        opinions = [
            {"agent": "breakout_analyst", "role": "voter", "stance": "long", "confidence": 0.7, "weight": 1.0},
            {"agent": "trend_analyst", "role": "voter", "stance": "short" if warn else "long", "confidence": 0.9,
             "weight": 1.0},
            {"agent": "mtf_analyst", "role": "voter", "stance": rng.choice(["long", "short"]), "confidence": 0.1,
             "weight": 1.0},
        ]
        rows.append({"id": f"d{i}", "side": "BUY", "r_base": r, "opinions": opinions, "advisor_multiplier": None,
                     "ensemble": {"cio_rule_multiplier": 1.0}})
    return rows


def test_ablation_credits_the_predictive_agent_and_finds_the_useless_one() -> None:
    report = ablation_report(_dossiers(1, 300, True), SETTINGS, ["breakout_analyst", "trend_analyst", "mtf_analyst"],
                             0.9, 50)
    agents = report["agents"]
    assert agents["trend_analyst"]["value_added_sharpe"] > 0.1 and agents["trend_analyst"]["ci"][0] > 0
    assert abs(agents["mtf_analyst"]["value_added_sharpe"]) < agents["trend_analyst"]["value_added_sharpe"]
    assert agents["mtf_analyst"]["ci"][0] <= 0 <= agents["mtf_analyst"]["ci"][1]
    assert report["team_beats_baseline"] and select_mode("auto", report, 50)[0] == "full"


@pytest.mark.parametrize("seed", range(5))
def test_auto_mode_falls_back_to_baseline_when_the_team_adds_nothing(seed: int) -> None:
    report = ablation_report(_dossiers(seed, 300, False), SETTINGS,
                             ["breakout_analyst", "trend_analyst", "mtf_analyst"], 0.9, 50)
    mode, reason = select_mode("auto", report, 50)
    assert not report["team_beats_baseline"]
    assert mode == "baseline" and "did NOT beat" in reason


def test_auto_mode_waits_for_evidence_and_explicit_modes_win() -> None:
    small = ablation_report(_dossiers(0, 20, True), SETTINGS, ["trend_analyst"], 0.9, 50)
    assert select_mode("auto", small, 50)[0] == "baseline"
    assert select_mode("auto", None, 50)[0] == "baseline"
    assert select_mode("full", small, 50)[0] == "full" and select_mode("baseline", None, 50)[0] == "baseline"


def test_benching_and_return_follow_the_evidence(tmp_path: Path) -> None:
    rt = runtime(tmp_path, team_config("auto", bench_min_samples=20))
    try:
        rt._bench_by_performance({"agents": {"mtf_analyst": {"n": 30, "value_added_sharpe": -0.01},
                                             "trend_analyst": {"n": 10, "value_added_sharpe": -0.5}}})
        assert rt.perf_benched == {"mtf_analyst"}  # trend_analyst: not a significant sample yet
        assert rt.agents["mtf_analyst"].state.value == "resting"
        rt._bench_by_performance({"agents": {"mtf_analyst": {"n": 40, "value_added_sharpe": 0.01}}})
        assert rt.perf_benched == {"mtf_analyst"}  # hysteresis: a tiny positive value is not enough
        rt._bench_by_performance({"agents": {"mtf_analyst": {"n": 60, "value_added_sharpe": 0.05}}})
        assert rt.perf_benched == set() and rt.agents["mtf_analyst"].benched is None
        rt._bench_by_performance({"agents": {"risk_manager": {"n": 999, "value_added_sharpe": -1.0},
                                             "data_clerk": {"n": 999, "value_added_sharpe": -1.0}}})
        assert rt.perf_benched == set()  # never benched
    finally:
        rt.close()


def test_benched_agent_is_excluded_from_the_ensemble_but_still_observed(tmp_path: Path) -> None:
    from team_helpers import RecordingGateway, context, pin_votes

    rt = runtime(tmp_path)
    try:
        pin_votes(rt, breakout_analyst=("long", 0.8), trend_analyst=("short", 0.9), mean_reversion_analyst=("flat", 0.5),
                  mtf_analyst=("flat", 0.5))
        blocked = rt.decide(context(decision_id="a"), RecordingGateway())
        rt.perf_benched.add("trend_analyst")
        rt.agents["trend_analyst"].benched = "test"
        unblocked = rt.decide(context(decision_id="b"), RecordingGateway())
        assert blocked.action == "no_trade" and unblocked.action == "enter"
        ops = rt.store.dossier("b")["opinions"]
        assert any(o["agent"] == "trend_analyst" and o["benched"] for o in ops)
    finally:
        rt.close()
