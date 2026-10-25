"""CIO decision protocol: unanimous / split / contradictory votes, the Risk Manager's final
veto, the Guardian, safety gates in every mode, isolation of failing agents, dossiers."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from team_helpers import UTC_NOW, RecordingGateway, context, pin_votes, runtime, team_config
from mt5bot import limits
from mt5bot.config import Config
from mt5bot.officedb import OfficeStore
from mt5bot.team.agents.control import GuardianSnapshot
from mt5bot.team.ensemble import Constraint, EnsembleSettings, Vote, aggregate
from mt5bot.team.learning import Learner, OutcomeVote
from mt5bot.team.runtime import TeamRuntime

SETTINGS = EnsembleSettings()


# ------------------------------------------------------------------------ ensemble


def test_unanimous_agreement_gives_full_base_size() -> None:
    votes = [Vote(a, "long", 0.8) for a in ("trend_analyst", "breakout_analyst", "mtf_analyst")]
    result = aggregate(1, votes, [], SETTINGS)
    assert result.action == "enter" and result.multiplier == 1.0 and result.disagreement == 0.0


def test_split_vote_shrinks_size() -> None:
    result = aggregate(1, [Vote("trend_analyst", "long", 0.8), Vote("mean_reversion_analyst", "short", 0.3)], [],
                       SETTINGS)
    assert result.action == "enter" and 0.0 < result.multiplier < 1.0
    assert result.disagreement == pytest.approx(0.3 / 1.1)


def test_contradictory_votes_mean_no_trade() -> None:
    result = aggregate(1, [Vote("trend_analyst", "long", 0.6), Vote("mean_reversion_analyst", "short", 0.9),
                           Vote("breakout_analyst", "short", 0.7)], [], SETTINGS)
    assert result.action == "no_trade" and result.multiplier == 0.0


def test_any_gate_veto_ends_the_decision() -> None:
    votes = [Vote("trend_analyst", "long", 1.0, 5.0)] * 3
    result = aggregate(1, votes, [Constraint("news_sentinel", "veto", 0.0, "NFP")], SETTINGS)
    assert result.action == "no_trade" and result.vetoed_by == ("news_sentinel",)


def test_min_agreement_is_enforced() -> None:
    settings = EnsembleSettings(min_agreement=2)
    assert aggregate(1, [Vote("trend_analyst", "long", 0.9)], [], settings).action == "no_trade"


@pytest.mark.parametrize("seed", range(40))
def test_multiplier_is_bounded_and_disagreement_only_shrinks(seed: int) -> None:
    rng = random.Random(seed)
    agents = ["trend_analyst", "mean_reversion_analyst", "breakout_analyst", "mtf_analyst", "meta_labeler"]
    votes = [Vote(a, rng.choice(["long", "short", "flat"]), rng.random(), rng.uniform(0.05, 5.0)) for a in agents]
    side = rng.choice([1, -1])
    constraints = [Constraint("session_analyst", "size", rng.random())] if rng.random() < 0.5 else []
    base = aggregate(side, votes, constraints, SETTINGS)
    assert 0.0 <= base.multiplier <= limits.MAX_SIZE_MULTIPLIER
    against = "short" if side > 0 else "long"
    more = aggregate(side, votes + [Vote("extra", against, rng.random(), rng.uniform(0.05, 5.0))], constraints,
                     SETTINGS)
    assert more.multiplier <= base.multiplier + 1e-12
    vetoed = aggregate(side, votes, constraints + [Constraint("data_clerk", "veto")], SETTINGS)
    assert vetoed.multiplier == 0.0


def test_learned_weights_stay_within_bounds() -> None:
    team = team_config(weight_min=0.25, weight_max=2.0, learning_rate=0.5, warmup_outcomes=1).team
    learner = Learner(team)
    rng = random.Random(1)
    for _ in range(500):
        votes = [OutcomeVote("trend_analyst", "long", True, 1.0, 1.0),
                 OutcomeVote("mtf_analyst", "short", False, 1.0, 1.0)]
        learner.update("trend", votes, won=rng.random() < 0.95)
    weights = learner.weights_for("trend", ["trend_analyst", "mtf_analyst"])
    assert weights["trend_analyst"] == pytest.approx(2.0) and weights["mtf_analyst"] == pytest.approx(0.25)


# ------------------------------------------------------------------------ protocol


def test_risk_manager_veto_cannot_be_outvoted(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    try:
        pin_votes(rt, trend_analyst=("long", 1.0), breakout_analyst=("long", 1.0), mtf_analyst=("long", 1.0),
                  mean_reversion_analyst=("long", 1.0))
        gateway = RecordingGateway(allowed=False, reason="daily loss limit")
        decision = rt.decide(context(), gateway)
        assert decision.action == "no_trade" and gateway.submits == []
        assert any("risk layer" in r for r in decision.reasons)
        # ...and if the gate only refuses at send time, the result is still no trade.
        gateway = RecordingGateway(allowed=True, submit_allowed=False, reason="spread too wide")
        assert rt.decide(context(decision_id="d2"), gateway).action == "no_trade"
    finally:
        rt.close()


def test_disagreement_reduces_size_in_full_mode(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    try:
        pin_votes(rt, breakout_analyst=("long", 0.7), trend_analyst=("long", 0.7), mtf_analyst=("long", 0.7),
                  mean_reversion_analyst=("long", 0.7))
        unanimous = rt.decide(context(decision_id="a"), RecordingGateway())
        pin_votes(rt, mean_reversion_analyst=("short", 0.6))
        split = rt.decide(context(decision_id="b"), RecordingGateway())
        assert unanimous.action == split.action == "enter"
        assert split.multiplier < unanimous.multiplier == 1.0
    finally:
        rt.close()


def test_baseline_mode_keeps_base_size_and_records_the_counterfactual(tmp_path: Path) -> None:
    rt = runtime(tmp_path, team_config("baseline"))
    try:
        pin_votes(rt, breakout_analyst=("long", 0.6), mean_reversion_analyst=("short", 0.9),
                  trend_analyst=("short", 0.8), mtf_analyst=("short", 0.8))
        gateway = RecordingGateway()
        decision = rt.decide(context(), gateway)
        assert decision.action == "enter" and gateway.submits == [1.0]
        assert decision.counterfactual_full == 0.0
        dossier = rt.store.dossier(decision.dossier_id)
        assert dossier["mode"] == "baseline" and dossier["ensemble"]["counterfactual_full"] == 0.0
    finally:
        rt.close()


@pytest.mark.parametrize("mode", ["baseline", "full"])
def test_unhealthy_data_blocks_entries_in_every_mode(tmp_path: Path, mode: str) -> None:
    rt = runtime(tmp_path, team_config(mode))
    try:
        gateway = RecordingGateway()
        decision = rt.decide(context(tick_age=3600.0), gateway)
        assert decision.action == "no_trade" and gateway.previews == [] and gateway.submits == []
        assert any("data unhealthy" in r for r in decision.reasons)
    finally:
        rt.close()


def test_guardian_freezes_and_clears(tmp_path: Path) -> None:
    rt = runtime(tmp_path, team_config("full", guardian_freeze_clear_checks=2))
    try:
        bad = GuardianSnapshot(({"ticket": 1, "symbol": "EURUSD", "sl": 0.0, "tp": 1.2, "volume": 0.1},),
                               frozenset({1}), 10_000, 50, 2.0, 0.5, 3)
        report = rt.guardian_check(bad)
        assert report.freeze_reason and not report.kill_reason
        report = rt.guardian_check(bad)
        report = rt.guardian_check(bad)
        assert report.kill_reason, "an unprotected position that persists triggers the kill switch"
        good = GuardianSnapshot(({"ticket": 1, "symbol": "EURUSD", "sl": 1.0, "tp": 1.2, "volume": 0.1},),
                                frozenset({1}), 10_000, 50, 2.0, 0.5, 3)
        assert rt.guardian_check(good).freeze_reason  # needs consecutive clean checks
        assert rt.guardian_check(good).freeze_reason is None
        crowded = GuardianSnapshot(tuple({"ticket": i, "symbol": "EURUSD", "sl": 1.0, "tp": 1.2, "volume": 0.1}
                                         for i in range(2)), frozenset({0, 1}), 10_000, 50, 2.0, 0.5, 3)
        assert rt.guardian_check(crowded).kill_reason  # two positions on one symbol: a hard ceiling breach
    finally:
        rt.close()


@pytest.mark.parametrize("enabled", [True, False])
def test_guardian_notification_follows_the_config(tmp_path: Path, enabled: bool) -> None:
    cfg = Config.from_dict({"team": {"team_mode": "full"}, "trading_hours_server": [0, 24],
                            "notifications": {"guardian": enabled}})
    sent: list[str] = []
    store = OfficeStore(tmp_path / "office.sqlite", clock=lambda: UTC_NOW)
    rt = TeamRuntime(cfg, tmp_path, None, None, notify=sent.append, clock=lambda: UTC_NOW, store=store)
    try:
        bad = GuardianSnapshot(({"ticket": 1, "symbol": "EURUSD", "sl": 0.0, "tp": 1.2, "volume": 0.1},),
                               frozenset({1}), 10_000, 50, 2.0, 0.5, 3)
        assert rt.guardian_check(bad).freeze_reason  # the freeze itself never depends on notifications
        assert bool(sent) is enabled
    finally:
        rt.close()


def test_crashing_voter_is_isolated_and_the_decision_completes(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    try:
        def boom(ctx):
            raise RuntimeError("bug in an agent")

        rt.agents["mtf_analyst"].analyze = boom  # type: ignore[method-assign]
        pin_votes(rt, breakout_analyst=("long", 0.8), trend_analyst=("long", 0.8), mean_reversion_analyst=("flat", 0.5))
        decision = rt.decide(context(), RecordingGateway())
        assert decision.action == "enter"
        dossier = rt.store.dossier(decision.dossier_id)
        failed = [o for o in dossier["opinions"] if o["agent"] == "mtf_analyst"]
        assert failed and failed[0]["stance"] == "unavailable"
    finally:
        rt.close()


def test_owner_failure_leaves_the_champion_as_its_own_proposer(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    try:
        rt.agents["breakout_analyst"].analyze = lambda ctx: None  # type: ignore[method-assign]
        pin_votes(rt, trend_analyst=("flat", 0.5), mean_reversion_analyst=("flat", 0.5), mtf_analyst=("flat", 0.5))
        decision = rt.decide(context(), RecordingGateway())
        dossier = rt.store.dossier(decision.dossier_id)
        assert any(o["agent"] == "champion" for o in dossier["opinions"])
    finally:
        rt.close()


def test_dossier_contains_the_full_story(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    try:
        pin_votes(rt, breakout_analyst=("long", 0.8), trend_analyst=("long", 0.7), mean_reversion_analyst=("short", 0.2),
                  mtf_analyst=("flat", 0.5))
        decision = rt.decide(context(), RecordingGateway())
        d = rt.store.dossier(decision.dossier_id)
        for key in ("inputs", "opinions", "weights", "ensemble", "advisor", "risk", "execution"):
            assert d[key] is not None
        assert d["advisor"]["status"] == "unavailable"
        assert {o["agent"] for o in d["opinions"]} >= {"data_clerk", "regime_detector", "risk_manager",
                                                       "breakout_analyst", "trend_analyst"}
        assert d["risk"]["preview"]["allowed"] is True and d["execution"]["ok"] is True
        rt.bus.flush()
        topics = [e["topic"] for e in rt.store.events_after(0, 1000) if e["correlation_id"] == decision.dossier_id]
        assert topics[0] == "decision.started" and "decision.finished" in topics and "risk.decision" in topics
    finally:
        rt.close()


def test_regime_ineligibility_blocks_only_in_full_mode(tmp_path: Path) -> None:
    rt = runtime(tmp_path, team_config("full", regime_min_trades=5))
    try:
        pin_votes(rt, breakout_analyst=("long", 0.9), trend_analyst=("long", 0.9))
        regime = rt.decide(context(decision_id="probe"), RecordingGateway())
        label = rt.store.dossier(regime.dossier_id)["regime"]
        rt.regime_stats[("donchian", label)] = (30, -0.4, 1.0)
        decision = rt.decide(context(decision_id="x"), RecordingGateway())
        assert decision.action == "no_trade" and any("no edge" in r for r in decision.reasons)
    finally:
        rt.close()
