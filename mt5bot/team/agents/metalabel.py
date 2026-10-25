"""Meta-Labeler: probability that a champion proposal wins, from its context features.

A small L2-regularised logistic regression (numpy, Newton/IRLS) trained walk-forward on
resolved decision dossiers: fit on the older part, validated on the most recent
``meta_holdout_fraction``. It votes only while the holdout Brier score beats the
base-rate forecast; otherwise it ABSTAINS. It is retrained every ``meta_retrain_every``
new outcomes. It runs after the desk (stage 2b) because agreement is one of its features.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from ..agent import Agent, AgentState, Opinion
from ..context import DecisionContext

FEATURES = ("regime_trend", "regime_range", "regime_high_vol", "regime_illiquid", "session_london",
            "session_new_york", "session_tokyo", "overlap", "rollover", "log_spread_ratio", "atr_pct",
            "side", "agreement", "aggregate_confidence", "disagreement", "proposer_confidence")


def feature_vector(f: Mapping[str, Any]) -> np.ndarray:
    def g(name: str, default: float = 0.0) -> float:
        value = f.get(name, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    return np.array([g(name) for name in FEATURES], dtype=float)


def features_from(ctx_views: Mapping[str, Any], side: str, desk_summary: Mapping[str, float]) -> dict[str, float]:
    regime = ctx_views.get("regime_detector") or {}
    probs = regime.get("probs") or {}
    session = ctx_views.get("session_analyst") or {}
    open_now = set(session.get("sessions") or [])
    ratio = float(session.get("spread_ratio") or 1.0)
    return {
        "regime_trend": probs.get("trend", 0.25), "regime_range": probs.get("range", 0.25),
        "regime_high_vol": probs.get("high_vol", 0.25), "regime_illiquid": probs.get("illiquid", 0.25),
        "session_london": float("london" in open_now), "session_new_york": float("new_york" in open_now),
        "session_tokyo": float("tokyo" in open_now), "overlap": float(bool(session.get("overlap"))),
        "rollover": float(bool(session.get("rollover"))), "log_spread_ratio": math.log(max(ratio, 1e-3)),
        "atr_pct": float(regime.get("atr_pct", 50.0)) / 100.0, "side": 1.0 if side == "BUY" else -1.0,
        "agreement": float(desk_summary.get("agreement", 0.0)),
        "aggregate_confidence": float(desk_summary.get("aggregate_confidence", 0.5)),
        "disagreement": float(desk_summary.get("disagreement", 0.0)),
        "proposer_confidence": float(desk_summary.get("proposer_confidence", 0.5)),
    }


def fit_logistic(x: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 30) -> np.ndarray:
    """Weights (bias first) of an L2-penalised logistic regression (bias not penalised)."""
    n, k = x.shape
    xb = np.hstack([np.ones((n, 1)), x])
    w = np.zeros(k + 1)
    penalty = np.full(k + 1, l2)
    penalty[0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(xb @ w, -40, 40)))
        grad = xb.T @ (p - y) + penalty * w
        hess = (xb * (p * (1 - p))[:, None]).T @ xb + np.diag(penalty + 1e-9)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        w -= step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return w


@dataclass
class MetaModel:
    weights: np.ndarray
    mean: np.ndarray
    std: np.ndarray
    holdout_brier: float
    base_brier: float
    n_train: int
    n_holdout: int

    @property
    def validated(self) -> bool:
        return self.n_holdout >= 20 and self.holdout_brier < self.base_brier

    def predict(self, features: Mapping[str, Any]) -> float:
        x = (feature_vector(features) - self.mean) / self.std
        z = float(self.weights[0] + x @ self.weights[1:])
        return 1.0 / (1.0 + math.exp(-max(min(z, 40.0), -40.0)))

    def to_dict(self) -> dict[str, Any]:
        return {"validated": self.validated, "holdout_brier": round(self.holdout_brier, 5),
                "base_brier": round(self.base_brier, 5), "n_train": self.n_train, "n_holdout": self.n_holdout,
                "coefficients": {name: round(float(c), 4) for name, c in zip(("bias",) + FEATURES, self.weights)}}


def train(rows: Sequence[tuple[Mapping[str, Any], int]], holdout_fraction: float, l2: float) -> Optional[MetaModel]:
    """Walk-forward split (rows must be in time order): fit on the past, score on the most recent part."""
    if len(rows) < 30:
        return None
    x = np.vstack([feature_vector(f) for f, _y in rows])
    y = np.array([float(label) for _f, label in rows])
    cut = int(len(rows) * (1 - holdout_fraction))
    xtr, ytr, xho, yho = x[:cut], y[:cut], x[cut:], y[cut:]
    mean = xtr.mean(axis=0)
    std = np.where(xtr.std(axis=0) > 1e-9, xtr.std(axis=0), 1.0)
    w = fit_logistic((xtr - mean) / std, ytr, l2)
    zb = w[0] + ((xho - mean) / std) @ w[1:]
    p = 1.0 / (1.0 + np.exp(-np.clip(zb, -40, 40)))
    holdout = float(np.mean((p - yho) ** 2))
    base = float(np.mean((ytr.mean() - yho) ** 2))
    # Deploy the coefficients fitted on ALL rows; the validation verdict comes from the holdout.
    mean_all = x.mean(axis=0)
    std_all = np.where(x.std(axis=0) > 1e-9, x.std(axis=0), 1.0)
    w_all = fit_logistic((x - mean_all) / std_all, y, l2)
    return MetaModel(w_all, mean_all, std_all, holdout, base, cut, len(rows) - cut)


class MetaLabeler(Agent):
    id = "meta_labeler"
    MARGIN = 0.05

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self.model: Optional[MetaModel] = None
        self._trained_on = 0

    def maybe_retrain(self, rows: Sequence[tuple[Mapping[str, Any], int]]) -> Optional[MetaModel]:
        team = self.cfg.team
        if len(rows) < team.meta_min_train or len(rows) - self._trained_on < team.meta_retrain_every:
            return None
        self._trained_on = len(rows)
        self.set_state(AgentState.REVIEWING, task="a treinar o modelo", message=f"Retreino com {len(rows)} resultados")
        self.model = train(rows, team.meta_holdout_fraction, team.meta_l2)
        if self.model is not None:
            self.stats["model"] = self.model.to_dict()
            verdict = "validado" if self.model.validated else "NÃO validado: abstenho-me"
            self.say(f"Modelo {verdict} (Brier {self.model.holdout_brier:.3f} vs base {self.model.base_brier:.3f})")
        return self.model

    def analyze(self, ctx: DecisionContext) -> Opinion:
        features = dict(ctx.view("meta_features") or {})
        if self.model is None or not self.model.validated or not features:
            status = "sem modelo" if self.model is None else "modelo não validado fora da amostra"
            self.set_state(AgentState.IDLE, task=status, message=f"Abstenho-me: {status}")
            return self.opinion(ctx, "abstain", 0.0, [("NOT_VALIDATED", status)],
                                details={"model": self.stats.get("model")})
        p = self.model.predict(features)
        side = 1 if ctx.proposal is not None and ctx.proposal.side == "BUY" else -1
        if p >= 0.5 + self.MARGIN:
            stance, conf = ("long" if side > 0 else "short"), (p - 0.5) * 2
        elif p <= 0.5 - self.MARGIN:
            stance, conf = ("short" if side > 0 else "long"), (0.5 - p) * 2
        else:
            stance, conf = "flat", 0.5
        self.set_state(AgentState.ANALYZING, task="probabilidade de ganho",
                       message=f"{ctx.market.symbol}: P(ganho) = {p:.2f}")
        return self.opinion(ctx, stance, conf, [("META_PROBABILITY", f"P(win) = {p:.2f}")], features=FEATURES,
                            details={"p_win": round(p, 4), "model": self.stats.get("model")})
