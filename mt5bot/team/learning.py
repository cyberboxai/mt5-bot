"""Bounded, honest learning for the team (numpy only).

Weights: exponential weights (Hedge) per (agent, regime) on the Brier loss of each
agent's probability that the proposal wins, centred on the mean loss of the agents that
voted, with a learning-rate cap, a decay toward uniform (so noise random-walks around 1
instead of drifting), and clipping to [w_min, w_max] so no agent is ever zeroed by noise.
Until a regime has ``warmup_outcomes`` resolved outcomes the ensemble uses uniform weights.

Calibration: per-agent Platt scaling of the agent's claimed probability that ITS direction
is right, fitted by regularised Newton steps once ``calibration_min_samples`` exist, and
kept only if it improves the Brier score on the samples (otherwise identity).

Everything is persisted, logged in ``weights_history`` and reversible (``--reset-team-weights``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from .. import limits

EPS = 1e-6
ALL_REGIMES = "all"


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((np.asarray(p, dtype=float) - np.asarray(y, dtype=float)) ** 2)) if len(p) else float("nan")


def fit_platt(q: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 60) -> tuple[float, float]:
    """Fit p' = sigmoid(a * logit(q) + b), penalising (a - 1)^2 + b^2 (shrinks toward identity).

    Damped Newton: each step is halved until the penalised log-loss decreases, because a
    full Newton step overshoots badly when the claimed probabilities are near 0 or 1.
    """
    x = logit(np.asarray(q, dtype=float))
    y = np.asarray(y, dtype=float)

    def loss(a: float, b: float) -> float:
        z = a * x + b
        return float(np.sum(np.logaddexp(0.0, z) - y * z) + 0.5 * l2 * ((a - 1.0) ** 2 + b * b))

    a, b = 1.0, 0.0
    current = loss(a, b)
    for _ in range(iters):
        p = sigmoid(a * x + b)
        w = p * (1 - p)
        g_a = float(np.sum((p - y) * x) + l2 * (a - 1.0))
        g_b = float(np.sum(p - y) + l2 * b)
        h_aa = float(np.sum(w * x * x) + l2)
        h_ab = float(np.sum(w * x))
        h_bb = float(np.sum(w) + l2)
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        step = 1.0
        while step > 1e-6:
            na, nb = a - step * da, b - step * db
            candidate = loss(na, nb)
            if candidate < current:
                break
            step /= 2
        else:
            break
        a, b, previous, current = na, nb, current, candidate
        if previous - current < 1e-10:
            break
    return float(np.clip(a, 0.0, 5.0)), float(np.clip(b, -5.0, 5.0))


def reliability(q: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict]:
    q = np.asarray(q, dtype=float)
    y = np.asarray(y, dtype=float)
    out = []
    edges = np.linspace(0.5, 1.0, bins // 2 + 1) if q.size and q.min() >= 0.5 else np.linspace(0.0, 1.0, bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (q >= lo) & ((q < hi) | (hi >= 1.0))
        if sel.any():
            out.append({"bin": [round(float(lo), 2), round(float(hi), 2)], "n": int(sel.sum()),
                        "claimed": round(float(q[sel].mean()), 4), "observed": round(float(y[sel].mean()), 4)})
    return out


@dataclass(frozen=True)
class OutcomeVote:
    """One agent's vote on one resolved proposal, as stored in the dossier."""

    agent: str
    stance: str            # long | short | flat (relative direction resolved below)
    agrees: Optional[bool]  # True agree, False disagree, None flat
    confidence: float      # the calibrated confidence actually used
    raw_confidence: float


def proposal_probability(agrees: Optional[bool], confidence: float) -> float:
    if agrees is None:
        return 0.5
    return 0.5 + confidence / 2 if agrees else 0.5 - confidence / 2


class Learner:
    def __init__(self, team: Any, store: Any = None) -> None:
        self.team = team
        self.store = store
        self.log_w: dict[tuple[str, str], list] = {}
        self.calibration: dict[str, tuple[float, float, int]] = {}
        self.samples: dict[str, list[tuple[float, int]]] = defaultdict(list)  # agent -> (claimed q, right?)
        self.regime_counts: dict[str, int] = defaultdict(int)
        if store is not None:
            for key, (logw, n) in store.load_weights().items():
                self.log_w[key] = [float(logw), int(n)]
            for agent, row in store.load_calibration().items():
                self.calibration[agent] = (float(row["a"]), float(row["b"]), int(row["n"]))
            for (agent, regime), (_w, n) in self.log_w.items():
                self.regime_counts[regime] = max(self.regime_counts[regime], n)

    def update_settings(self, team: Any) -> None:
        self.team = team

    # ------------------------------------------------------------------ weights

    def _bounds(self) -> tuple[float, float]:
        lo = max(self.team.weight_min, limits.MIN_AGENT_WEIGHT)
        hi = min(self.team.weight_max, limits.MAX_AGENT_WEIGHT)
        return math.log(lo), math.log(hi)

    def weight(self, agent: str, regime: str) -> float:
        if self.regime_counts.get(regime, 0) < self.team.warmup_outcomes:
            if regime != ALL_REGIMES:
                return self.weight(agent, ALL_REGIMES)  # pooled weights while this regime warms up
            return 1.0  # warm-up: uniform until enough resolved outcomes exist
        lo, hi = self._bounds()
        logw = self.log_w.get((agent, regime), [0.0, 0])[0]
        return float(math.exp(min(max(logw, lo), hi)))

    def weights_for(self, regime: str, agents: Iterable[str]) -> dict[str, float]:
        return {a: self.weight(a, regime) for a in agents}

    def update(self, regime: str, votes: Sequence[OutcomeVote], won: bool) -> dict[str, float]:
        """One Hedge step for every agent that voted on this resolved proposal."""
        y = 1.0 if won else 0.0
        losses = {v.agent: (proposal_probability(v.agrees, v.confidence) - y) ** 2 for v in votes}
        if not losses:
            return {}
        mean = sum(losses.values()) / len(losses)
        eta = min(self.team.learning_rate, limits.MAX_LEARNING_RATE)
        decay = self.team.weight_decay
        lo, hi = self._bounds()
        changed = {}
        for regime_key in (regime, ALL_REGIMES):
            for agent, loss in losses.items():
                entry = self.log_w.setdefault((agent, regime_key), [0.0, 0])
                entry[0] = (1.0 - decay) * entry[0] - eta * (loss - mean)
                entry[0] = min(max(entry[0], lo), hi)
                entry[1] += 1
                changed[f"{agent}|{regime_key}"] = math.exp(entry[0])
            self.regime_counts[regime_key] += 1
        for v in votes:
            if v.agrees is not None:
                q = 0.5 + v.raw_confidence / 2
                self.samples[v.agent].append((q, int(won == v.agrees)))
                del self.samples[v.agent][:-5000]
        return changed

    # -------------------------------------------------------------- calibration

    def calibrate(self, agent: str, raw_confidence: float) -> float:
        """Map an agent's raw confidence to the calibrated one used in the ensemble."""
        params = self.calibration.get(agent)
        if params is None:
            return float(min(max(raw_confidence, 0.0), 1.0))
        a, b, _n = params
        q = 0.5 + min(max(raw_confidence, 0.0), 1.0) / 2
        q_cal = float(sigmoid(np.array([a * float(logit(np.array([q]))[0]) + b]))[0])
        return float(min(max(2 * q_cal - 1, 0.0), 1.0))  # an anti-predictive agent becomes uninformative, never flipped

    def fit_calibration(self, agent: str) -> Optional[dict]:
        data = self.samples.get(agent, [])
        if len(data) < self.team.calibration_min_samples:
            return None
        q = np.array([d[0] for d in data])
        y = np.array([d[1] for d in data], dtype=float)
        a, b = fit_platt(q, y)
        q_cal = sigmoid(a * logit(q) + b)
        raw_b, cal_b = brier(q, y), brier(q_cal, y)
        if not cal_b < raw_b:
            a, b, cal_b = 1.0, 0.0, raw_b  # keep identity unless calibration actually helps
        self.calibration[agent] = (a, b, len(data))
        rel = reliability(q, y)
        if self.store is not None:
            self.store.save_calibration(agent, a, b, len(data), raw_b, cal_b, rel)
        return {"a": a, "b": b, "n": len(data), "brier_raw": raw_b, "brier_cal": cal_b, "reliability": rel}

    # ------------------------------------------------------------------ persist

    def save(self) -> None:
        if self.store is not None and self.log_w:
            self.store.save_weights({k: (v[0], v[1]) for k, v in self.log_w.items()})

    def reset(self) -> None:
        self.log_w.clear()
        self.calibration.clear()
        self.samples.clear()
        self.regime_counts.clear()
        if self.store is not None:
            self.store.reset_weights()

    def snapshot(self) -> dict[str, Any]:
        weights: dict[str, dict[str, float]] = defaultdict(dict)
        lo, hi = self._bounds()
        for (agent, regime), (logw, n) in self.log_w.items():
            weights[agent][regime] = round(math.exp(min(max(logw, lo), hi)), 4)
        return {"weights": dict(weights), "regime_counts": dict(self.regime_counts),
                "calibration": {a: {"a": p[0], "b": p[1], "n": p[2]} for a, p in self.calibration.items()},
                "warmup_outcomes": self.team.warmup_outcomes}


def votes_from_dossier(dossier: Mapping[str, Any]) -> list[OutcomeVote]:
    """Directional votes stored in a dossier, relative to its proposal."""
    side = 1 if dossier.get("side") == "BUY" else -1
    out = []
    for op in dossier.get("opinions", []):
        if op.get("role") != "voter" or op.get("stance") not in ("long", "short", "flat"):
            continue
        s = 1 if op["stance"] == "long" else -1 if op["stance"] == "short" else 0
        agrees = None if s == 0 else s == side
        out.append(OutcomeVote(op["agent"], op["stance"], agrees, float(op.get("confidence", 0.0)),
                               float(op.get("raw_confidence", op.get("confidence", 0.0)) or 0.0)))
    return out
