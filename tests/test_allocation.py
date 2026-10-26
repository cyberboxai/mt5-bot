"""CIO allocation: one proposal per symbol and bar, every signal ends with a funnel code,
and the arm's own evidence in the current regime ranks and filters the signals."""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pytest

from mt5bot.allocation import REGIME_BLOCK_P, AllocationInputs, allocate
from mt5bot.arms import Arm, ArmSignal, RegimeView
from mt5bot.evidence import Posterior
from mt5bot.strategies.base import Signal

BAR = 1_700_000_000


def post(p: float, mean: float = 0.1, sd: float = 0.1) -> Posterior:
    return Posterior(mean, sd, p, mean - 0.2, mean + 0.2, 0.3, 0.0, 0.05, 50, 0, 50.0, mean, 1.0)


def signal(arm_id: int, tier: str, p: float, strategy: str = "donchian", policy: str = "fixed") -> ArmSignal:
    arm = Arm(arm_id, "EURUSD", "M15", strategy, {}, arm_id, "test", tier=tier, policy=policy, posterior=post(p))
    return ArmSignal(arm, Signal("BUY", 0.001, 2.0, 3.0), BAR)


class Recorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, Optional[int], dict]] = []

    def __call__(self, stage: str, outcome: str, reason: str, arm_id: Optional[int] = None,
                 bar_time: Optional[int] = None, values: Optional[dict] = None) -> None:
        self.rows.append((stage, outcome, reason, arm_id, values or {}))

    def codes(self) -> dict[int, str]:
        return {arm_id: reason for _s, _o, reason, arm_id, _v in self.rows if arm_id is not None}


def run(signals: list[ArmSignal], **kwargs: Any) -> tuple[Any, Recorder]:
    rec = Recorder()
    intent = allocate(AllocationInputs("EURUSD", "M15", signals, **kwargs), np.random.default_rng(1), rec)
    return intent, rec


def test_best_tier_wins_and_every_other_signal_gets_a_code() -> None:
    intent, rec = run([signal(1, "T1", 0.6), signal(2, "T2", 0.85), signal(3, "T1", 0.7)])
    assert intent is not None and intent.arm_id == 2 and intent.tier == "T2"
    assert rec.codes() == {2: "SELECTED", 1: "ONE_PER_SYMBOL", 3: "ONE_PER_SYMBOL"}


def test_busy_symbol_blocks_everything_with_its_reason() -> None:
    intent, rec = run([signal(1, "T2", 0.9)], symbol_busy="RISK_OPEN_RISK")
    assert intent is None and rec.codes() == {1: "RISK_OPEN_RISK"}


def test_unaffordable_signal_falls_through_to_the_next() -> None:
    intent, rec = run([signal(1, "T2", 0.9), signal(2, "T1", 0.6)],
                      affordable=lambda s: "MIN_LOT_EXCEEDS_RISK" if s.arm.id == 1 else None)
    assert intent is not None and intent.arm_id == 2
    assert rec.codes()[1] == "MIN_LOT_EXCEEDS_RISK"


def test_shadow_arm_needs_a_free_exploration_slot() -> None:
    intent, rec = run([signal(1, "T0", 0.6)], open_exploration=2, exploration_slots=2)
    assert intent is None and rec.codes() == {1: "EXPLORATION_SLOTS_FULL"}
    intent, rec = run([signal(1, "T0", 0.6)], exploration_enabled=False)
    assert intent is None and rec.codes() == {1: "TIER_T0_SHADOW"}


def test_exploration_trades_at_t1_and_keeps_the_arm_policy() -> None:
    sig = signal(1, "T0", 0.9, policy="trail_atr")
    sig.arm.posterior = post(0.99, mean=0.5, sd=0.05)  # a draw below 0 is practically impossible
    intent, rec = run([sig])
    assert intent is not None and intent.exploration and intent.tier == "T1" and intent.policy == "trail_atr"
    assert rec.codes() == {1: "SELECTED"}


def test_thompson_rejects_arms_the_evidence_turned_against() -> None:
    sig = signal(1, "T0", 0.01)
    sig.arm.posterior = post(0.01, mean=-0.5, sd=0.05)
    intent, rec = run([sig])
    assert intent is None and rec.codes() == {1: "THOMPSON_REJECT"}


def test_strategy_level_regime_veto_applies_to_tiered_arms() -> None:
    intent, rec = run([signal(1, "T2", 0.9)], regime_ineligible=lambda strategy: "no edge in range")
    assert intent is None and rec.codes() == {1: "REGIME_INELIGIBLE"}


def view(p: float, n: int, regime: str = "range") -> RegimeView:
    return RegimeView(regime, n, -0.4 if p < 0.5 else 0.3, -0.2 if p < 0.5 else 0.2, p)


def test_arm_sits_out_a_regime_where_its_own_evidence_is_negative() -> None:
    views = {1: view(0.10, 40), 2: view(0.70, 40)}
    intent, rec = run([signal(1, "T2", 0.95), signal(2, "T2", 0.80)], regime="range",
                      regime_view=lambda s: views.get(s.arm.id))
    assert intent is not None and intent.arm_id == 2 and intent.regime == "range"
    assert rec.codes()[1] in ("REGIME_INELIGIBLE", "ONE_PER_SYMBOL")


def test_regime_evidence_ranks_signals_within_a_tier() -> None:
    views = {1: view(0.55, 30), 2: view(0.90, 30)}
    intent, rec = run([signal(1, "T1", 0.95), signal(2, "T1", 0.60)], regime="trend",
                      regime_view=lambda s: views.get(s.arm.id))
    assert intent is not None and intent.arm_id == 2  # better in THIS regime, despite the lower overall P


@pytest.mark.parametrize("n,expected", [(19, "SELECTED"), (20, "REGIME_INELIGIBLE")])
def test_regime_filter_needs_enough_trades(n: int, expected: str) -> None:
    intent, rec = run([signal(1, "T2", 0.9)], regime="range", regime_view=lambda s: view(REGIME_BLOCK_P - 0.05, n),
                      regime_min_trades=20)
    assert rec.codes() == {1: expected}


def test_a_failing_regime_view_falls_back_to_the_overall_posterior() -> None:
    def broken(sig: ArmSignal) -> RegimeView:
        raise RuntimeError("bug")

    intent, rec = run([signal(1, "T2", 0.9)], regime="range", regime_view=broken)
    assert intent is not None and rec.codes() == {1: "SELECTED"}
