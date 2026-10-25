"""The CIO's bounded, transparent ensemble. Pure functions: same inputs, same output.

For a proposal in direction ``side`` each non-abstaining voter i contributes
``p_i`` = its probability that the proposal's direction is right:

    agree     p = 0.5 + c/2       disagree  p = 0.5 - c/2       flat  p = 0.5

with weight ``w_i`` (learned, clipped; flat votes count ``flat_vote_weight`` as much).

    aggregate confidence A = sum(w p) / sum(w)
    disagreement         D = sum(w c | disagree) / sum(w c | agree or disagree)
    multiplier           m = clip((A - A_min) / (A_full - A_min), 0, 1) * (1 - D) * product(size hints)

No trade when a gate vetoes, when fewer than ``min_agreement`` voters agree, when
A < A_min or when D > D_max. ``m`` is always clamped to [0, 1]: disagreement and
uncertainty can only shrink the deterministic base size, never grow it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .. import limits

ENTER = "enter"
NO_TRADE = "no_trade"


@dataclass(frozen=True)
class Vote:
    agent: str
    stance: str        # long | short | flat (abstain and veto are not votes)
    confidence: float
    weight: float = 1.0


@dataclass(frozen=True)
class Constraint:
    agent: str
    kind: str          # veto | size
    value: float = 1.0
    reason: str = ""


@dataclass(frozen=True)
class EnsembleSettings:
    min_agreement: int = 1
    min_aggregate_confidence: float = 0.52
    full_size_confidence: float = 0.65
    max_disagreement: float = 0.5
    flat_vote_weight: float = 0.5

    @classmethod
    def from_config(cls, team: object) -> "EnsembleSettings":
        return cls(team.min_agreement, team.min_aggregate_confidence, team.full_size_confidence,  # type: ignore[attr-defined]
                   team.max_disagreement, team.flat_vote_weight)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class EnsembleResult:
    action: str
    multiplier: float
    aggregate_confidence: float = 0.0
    disagreement: float = 0.0
    agreement_count: int = 0
    support: float = 0.0
    opposition: float = 0.0
    reasons: tuple[str, ...] = ()
    vetoed_by: tuple[str, ...] = ()
    size_hints: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"action": self.action, "multiplier": round(self.multiplier, 6),
                "aggregate_confidence": round(self.aggregate_confidence, 6), "disagreement": round(self.disagreement, 6),
                "agreement_count": self.agreement_count, "support": round(self.support, 6),
                "opposition": round(self.opposition, 6), "reasons": list(self.reasons),
                "vetoed_by": list(self.vetoed_by), "size_hints": dict(self.size_hints)}


def _clip01(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return min(max(x, 0.0), 1.0)


def stance_sign(stance: str) -> int:
    return 1 if stance == "long" else -1 if stance == "short" else 0


def aggregate(side: int, votes: Sequence[Vote], constraints: Sequence[Constraint],
              settings: EnsembleSettings) -> EnsembleResult:
    """``side`` is +1 (BUY) or -1 (SELL)."""
    if side not in (1, -1):
        return EnsembleResult(NO_TRADE, 0.0, reasons=("no directional proposal",))
    vetoes = tuple(sorted({c.agent for c in constraints if c.kind == "veto"}))
    if vetoes:
        reasons = tuple(f"veto by {c.agent}: {c.reason}" for c in constraints if c.kind == "veto")
        return EnsembleResult(NO_TRADE, 0.0, reasons=reasons, vetoed_by=vetoes)
    weighted_p = total_w = support = opposition = 0.0
    agree = 0
    for v in sorted(votes, key=lambda x: x.agent):
        c = _clip01(v.confidence)
        w = max(0.0, v.weight) if math.isfinite(v.weight) else 0.0
        s = stance_sign(v.stance)
        if s == 0:
            if v.stance != "flat":
                continue
            w *= settings.flat_vote_weight
            p = 0.5
        elif s == side:
            p = 0.5 + c / 2
            support += w * c
            agree += 1
        else:
            p = 0.5 - c / 2
            opposition += w * c
        weighted_p += w * p
        total_w += w
    if total_w <= 0:
        return EnsembleResult(NO_TRADE, 0.0, reasons=("no usable votes",))
    a = weighted_p / total_w
    d = opposition / (support + opposition) if support + opposition > 0 else 0.0
    base = dict(aggregate_confidence=a, disagreement=d, agreement_count=agree, support=support, opposition=opposition)
    if agree < settings.min_agreement:
        return EnsembleResult(NO_TRADE, 0.0, reasons=(f"{agree} agreeing voter(s) < min_agreement "
                                                       f"{settings.min_agreement}",), **base)
    if a < settings.min_aggregate_confidence:
        return EnsembleResult(NO_TRADE, 0.0, reasons=(f"aggregate confidence {a:.2f} < "
                                                       f"{settings.min_aggregate_confidence:.2f}",), **base)
    if d > settings.max_disagreement:
        return EnsembleResult(NO_TRADE, 0.0, reasons=(f"disagreement {d:.2f} > {settings.max_disagreement:.2f}",),
                              **base)
    span = settings.full_size_confidence - settings.min_aggregate_confidence
    m_conf = _clip01((a - settings.min_aggregate_confidence) / span) if span > 0 else 1.0
    m = m_conf * (1.0 - d)
    hints: dict[str, float] = {}
    reasons = [f"aggregate confidence {a:.2f}, disagreement {d:.2f}"]
    for c in sorted(constraints, key=lambda x: x.agent):
        if c.kind == "size":
            value = _clip01(c.value)
            if value < 1.0:
                hints[c.agent] = value
                reasons.append(f"size x{value:.2f} from {c.agent}: {c.reason}")
            m *= value
    m = min(_clip01(m), limits.MAX_SIZE_MULTIPLIER)
    if m <= 0:
        return EnsembleResult(NO_TRADE, 0.0, reasons=tuple(reasons) + ("size reduced to zero",), size_hints=hints,
                              **base)
    return EnsembleResult(ENTER, m, reasons=tuple(reasons), size_hints=hints, **base)


def apply_weights(votes: Sequence[Vote], weights: Mapping[str, float],
                  tilts: Optional[Mapping[str, float]] = None) -> list[Vote]:
    """Attach learned weights (and optional advisor tilts) to votes."""
    out = []
    for v in votes:
        w = float(weights.get(v.agent, 1.0))
        if tilts:
            w *= float(tilts.get(v.agent, 1.0))
        out.append(Vote(v.agent, v.stance, v.confidence, w))
    return out


def conservative(*multipliers: float) -> float:
    """The asymmetric-authority combinator: the smallest multiplier wins, clamped to [0, 1]."""
    return min([_clip01(m) for m in multipliers] + [limits.MAX_SIZE_MULTIPLIER])
