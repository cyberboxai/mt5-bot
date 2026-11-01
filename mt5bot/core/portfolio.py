"""Portfolio construction: a few uncorrelated bets, sized by risk, never leveraged up.

Weights can only SHRINK what the sizer allowed: a factor is always in [0, 1], so nothing here can
raise a position above its tier, the profile cap or the hard ceilings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class PortfolioRules:
    max_concurrent_arms: int = 4
    correlation_cap: float = 0.7
    max_cluster_weight: float = 0.5
    max_currency_risk_pct: float = 2.0
    target_correlated_risk_pct: float = 2.5


def inverse_vol_weights(vols: Mapping[str, float]) -> dict[str, float]:
    """Risk parity without correlations: each symbol contributes the same volatility."""
    usable = {k: v for k, v in vols.items() if v > 0 and math.isfinite(v)}
    if not usable:
        return {}
    inverse = {k: 1.0 / v for k, v in usable.items()}
    total = sum(inverse.values())
    return {k: w / total for k, w in inverse.items()}


def clusters(names: Sequence[str], corr: np.ndarray, threshold: float) -> list[list[str]]:
    """Connected components of |correlation| >= threshold."""
    n = len(names)
    seen: set[int] = set()
    out: list[list[str]] = []
    for i in range(n):
        if i in seen:
            continue
        stack, group = [i], []
        seen.add(i)
        while stack:
            a = stack.pop()
            group.append(names[a])
            for b in range(n):
                if b not in seen and abs(float(corr[a, b])) >= threshold:
                    seen.add(b)
                    stack.append(b)
        out.append(sorted(group))
    return out


def cap_clusters(weights: Mapping[str, float], groups: Sequence[Sequence[str]], cap: float) -> dict[str, float]:
    """Scale down every cluster above ``cap``. The total may end below 1: unused risk stays unused."""
    out = dict(weights)
    for group in groups:
        total = sum(out.get(k, 0.0) for k in group)
        if total > cap > 0:
            scale = cap / total
            for k in group:
                if k in out:
                    out[k] *= scale
    return out


def effective_bets(weights: Sequence[float], corr: np.ndarray) -> float:
    """How many independent bets the book really is: 1 for clones, n for n uncorrelated equal bets."""
    w = np.asarray(weights, dtype=float)
    if w.size == 0 or float(w.sum()) <= 0:
        return 0.0
    denominator = float(w @ np.asarray(corr, dtype=float) @ w)
    return float(w.sum() ** 2 / denominator) if denominator > 0 else 0.0


def correlated_risk(risks: Sequence[float], corr: np.ndarray) -> float:
    """Portfolio risk at the stops once correlation is counted: sqrt(r' rho r)."""
    r = np.asarray(risks, dtype=float)
    return math.sqrt(max(float(r @ np.asarray(corr, dtype=float) @ r), 0.0)) if r.size else 0.0


def correlation_matrix(returns: Sequence[np.ndarray]) -> np.ndarray:
    """Correlations on the common tail. Too little data means correlation 1 (the cautious answer)."""
    n = len(returns)
    out = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            k = min(len(returns[i]), len(returns[j]))
            value = 1.0
            if k >= 30:
                a, b = returns[i][-k:], returns[j][-k:]
                if float(a.std()) > 0 and float(b.std()) > 0:
                    value = float(np.corrcoef(a, b)[0, 1])
            out[i, j] = out[j, i] = value
    return out



def size_factor(symbol: str, symbols: Sequence[str], returns: Mapping[str, np.ndarray], open_risk_pct: Mapping[str, float],
                new_risk_pct: float, rules: PortfolioRules) -> tuple[float, dict[str, float]]:
    """Factor in [0, 1] for a new entry on ``symbol``, and the numbers behind it."""
    names = sorted(set(symbols) | {symbol})
    series = [returns.get(s, np.zeros(0)) for s in names]
    known = {s: float(np.std(returns[s])) for s in names if s in returns and len(returns[s]) >= 30}
    if symbol not in known or len(names) < 2:
        return 1.0, {"n_eff": float(len(names))}
    corr = correlation_matrix(series)
    weights = inverse_vol_weights(known)
    weights = cap_clusters(weights, clusters(names, corr, rules.correlation_cap), rules.max_cluster_weight)
    parity = weights.get(symbol, 0.0) * len(known)
    factor = min(max(parity, 0.0), 1.0)
    risks = [open_risk_pct.get(s, 0.0) + (new_risk_pct * factor if s == symbol else 0.0) for s in names]
    total = correlated_risk(risks, corr)
    if total > rules.target_correlated_risk_pct > 0:
        factor *= rules.target_correlated_risk_pct / total
    n_eff = effective_bets([weights.get(s, 0.0) for s in names], corr)
    return float(max(min(factor, 1.0), 0.0)), {"parity": round(parity, 4), "correlated_risk_pct": round(total, 4),
                                                "n_eff": round(n_eff, 3)}
