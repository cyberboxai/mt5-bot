"""CIO allocation: which of this bar's arm signals, if any, becomes an entry proposal.

At most one proposal per symbol and bar (one entry per symbol is a hard rule). Signals are
ranked by tier, then by ``P(E>0)`` in the current regime (the arm's evidence on trades
whose signal bar had this regime, shrunk toward its overall posterior; the overall
``P(E>0)`` when there is none). For the best-ranked signal:

* an arm whose own evidence in this regime is clearly negative (at least
  ``team.regime_min_trades`` trades and ``P(E>0) < 0.25``) sits this bar out;
* T1, T2 and T3 arms propose at their tier, if the strategy is eligible in the current regime;
* a T0 (shadow) arm may get ONE exploration trade: its expectancy is drawn from its posterior
  (Thompson sampling) and the trade is proposed at T1 micro risk when the draw clears the
  threshold and an exploration slot is free. The exploration budget is checked later, at
  sizing, with the exact money at stake.

When the best signal is blocked the next one is tried. Every signal ends with a funnel
outcome, so ``--diagnose`` can always say why a signal did not become an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

import numpy as np

from .arms import ArmSignal, EntryIntent, RegimeView
from .evidence import Posterior
from .tiers import TIER_INDEX, thompson_select


@dataclass(frozen=True)
class AllocationInputs:
    symbol: str
    timeframe: str
    signals: Sequence[ArmSignal]
    symbol_busy: Optional[str] = None            # reason code when the symbol already has an entry
    open_exploration: int = 0
    exploration_slots: int = 2
    exploration_enabled: bool = True
    exploration_threshold: float = 0.0
    affordable: Optional[Callable[[ArmSignal], Optional[str]]] = None  # reason code when unaffordable
    regime_ineligible: Optional[Callable[[str], Optional[str]]] = None
    regime: Optional[str] = None                                       # regime of this bar
    regime_view: Optional[Callable[[ArmSignal], Optional[RegimeView]]] = None
    regime_min_trades: int = 20


REGIME_BLOCK_P = 0.25  # an arm sits out a regime where its own evidence says P(E>0) < 0.25


def _rank(sig: ArmSignal, view: Optional[RegimeView]) -> tuple:
    post = sig.arm.posterior
    p = view.p_positive if view is not None else post.p_positive if post is not None else 0.0
    return (-TIER_INDEX.get(sig.arm.tier, 0), -p, sig.arm.id)


def _prior_posterior(family_mean: float = -0.03, sd: float = 0.08) -> Posterior:
    from statistics import NormalDist

    p = NormalDist().cdf(family_mean / sd)
    return Posterior(family_mean, sd, p, family_mean - 1.645 * sd, family_mean + 1.645 * sd, 1.0, family_mean, sd, 0,
                     0, 0.0, 0.0, 1.1)


def allocate(inp: AllocationInputs, rng: np.random.Generator,
             record: Callable[..., None]) -> Optional[EntryIntent]:
    """Return at most one EntryIntent; ``record(stage, outcome, reason, arm_id=..., values=...)``."""
    views: dict[int, Optional[RegimeView]] = {}
    for sig in inp.signals:
        try:
            views[sig.arm.id] = inp.regime_view(sig) if inp.regime_view is not None and inp.regime else None
        except Exception:  # noqa: BLE001 - regime evidence is advisory; fall back to the overall posterior
            views[sig.arm.id] = None
    ranked = sorted(inp.signals, key=lambda s: _rank(s, views.get(s.arm.id)))
    chosen: Optional[EntryIntent] = None
    for sig in ranked:
        arm = sig.arm
        post = arm.posterior or _prior_posterior()
        view = views.get(arm.id)
        values = {"strategy": arm.strategy, "tier": arm.tier, "p": round(post.p_positive, 4), "side": sig.signal.side,
                  "regime": inp.regime}
        if view is not None:
            values.update(p_regime=round(view.p_positive, 4), n_regime=view.n)

        def block(stage: str, reason: str, **extra: Any) -> None:
            record(stage, "block", reason, arm_id=arm.id, bar_time=sig.bar_time, values=dict(values, **extra))

        if chosen is not None:
            block("risk", "ONE_PER_SYMBOL", detail="another arm was selected on this bar")
            continue
        if inp.symbol_busy:
            block("risk", inp.symbol_busy)
            continue
        if inp.affordable is not None:
            reason = inp.affordable(sig)
            if reason:
                block("affordability", reason)
                continue
        if view is not None and view.n >= inp.regime_min_trades and view.p_positive < REGIME_BLOCK_P:
            block("tier", "REGIME_INELIGIBLE", detail=(f"arm evidence in the {view.regime} regime: {view.n} trades, "
                                                      f"mean {view.raw_mean:+.2f}R, P(E>0) {view.p_positive:.2f}"))
            continue
        exploration = False
        draw: Optional[float] = None
        tier = arm.tier
        if tier == "T0":
            if not inp.exploration_enabled:
                block("tier", "TIER_T0_SHADOW")
                continue
            if inp.open_exploration >= inp.exploration_slots:
                block("exploration", "EXPLORATION_SLOTS_FULL", open=inp.open_exploration, slots=inp.exploration_slots)
                continue
            ok, draw = thompson_select(post, rng, inp.exploration_threshold)
            if not ok:
                block("exploration", "THOMPSON_REJECT", draw=round(draw, 4), threshold=inp.exploration_threshold)
                continue
            exploration, tier = True, "T1"
        elif inp.regime_ineligible is not None:
            why = inp.regime_ineligible(arm.strategy)
            if why:
                block("tier", "REGIME_INELIGIBLE", detail=why[:160])
                continue
        record("tier", "pass", "SELECTED", arm_id=arm.id, bar_time=sig.bar_time,
               values=dict(values, selected=True, exploration=exploration,
                           draw=None if draw is None else round(draw, 4)))
        chosen = EntryIntent(sig.signal, arm.param_set_id, arm.strategy, arm.describe(), arm_id=arm.id, tier=tier,
                             timeframe=arm.timeframe, exploration=exploration, posterior=post, thompson=draw,
                             bar_time=sig.bar_time, policy=arm.policy, regime=inp.regime)
    return chosen
