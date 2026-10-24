"""Statistics for the acceptance gates, promotion and degradation rules (stdlib + numpy)."""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Sequence

import numpy as np

_NORMAL = NormalDist()
EULER_GAMMA = 0.5772156649015329


def bootstrap_mean_interval(rs: Sequence[float], confidence: float, samples: int, seed: int) -> tuple[float, float]:
    """Two-sided percentile bootstrap interval of the mean. Deterministic for a given seed."""
    x = np.asarray(rs, dtype=float)
    if x.size < 2:
        return (-math.inf, math.inf)
    rng = np.random.default_rng(seed)
    means = np.empty(samples)
    # Chunked so memory stays bounded for long series.
    chunk = max(1, 2_000_000 // x.size)
    for start in range(0, samples, chunk):
        count = min(chunk, samples - start)
        idx = rng.integers(0, x.size, size=(count, x.size))
        means[start:start + count] = x[idx].mean(axis=1)
    alpha = (1.0 - confidence) / 2.0
    return float(np.quantile(means, alpha)), float(np.quantile(means, 1.0 - alpha))


def expected_max_standard_normal(trials: int) -> float:
    """E[max of N iid standard normals] (Bailey and Lopez de Prado approximation)."""
    if trials <= 1:
        return 0.0
    return ((1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1.0 / trials)
            + EULER_GAMMA * _NORMAL.inv_cdf(1 - 1.0 / (trials * math.e)))


def deflated_sharpe(rs: Sequence[float], trials: int) -> float:
    """Probability that the per-trade Sharpe ratio beats the best expected from luck over N trials.

    Deflated Sharpe Ratio (Bailey and Lopez de Prado, 2014) with the null variance of the
    Sharpe estimate taken as 1/T, so the benchmark is E[max of N null Sharpe ratios].
    """
    x = np.asarray(rs, dtype=float)
    t = x.size
    if t < 3:
        return 0.0
    sd = x.std(ddof=1)
    if sd <= 0:
        return 1.0 if x.mean() > 0 else 0.0
    sr = x.mean() / sd
    z = (x - x.mean()) / x.std(ddof=0) if x.std(ddof=0) > 0 else np.zeros_like(x)
    skew = float(np.mean(z ** 3))
    kurt = float(np.mean(z ** 4))
    sr0 = math.sqrt(1.0 / t) * expected_max_standard_normal(trials)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        denom = 1e-9
    return float(_NORMAL.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)))


def prob_losing_run(n: int, k: int, loss_prob: float) -> float:
    """P(at least one run of >= k consecutive losses in n independent trades)."""
    if k <= 0:
        return 1.0
    if n < k or loss_prob <= 0:
        return 0.0
    if loss_prob >= 1:
        return 1.0
    q = loss_prob
    # state[j] = P(current trailing loss run == j and no run of k yet)
    state = [0.0] * k
    state[0] = 1.0
    hit = 0.0
    for _ in range(n):
        new = [0.0] * k
        new[0] = sum(state) * (1 - q)
        for j in range(k - 1):
            new[j + 1] = state[j] * q
        hit += state[k - 1] * q
        state = new
    return min(1.0, hit)


def streak_limit(n: int, loss_prob: float, alpha: float) -> int:
    """Smallest streak length k whose probability over n trades is below ``alpha``."""
    n = max(n, 1)
    for k in range(1, n + 2):
        if prob_losing_run(n, k, loss_prob) < alpha:
            return k
    return n + 1


def composite_score(window_r: Sequence[float], window_months: Sequence[float], max_dd_r: float,
                    dsr: float, variance_penalty: float = 0.5, dd_scale_r: float = 10.0) -> float:
    """Robust ranking score, roughly "R per month", penalised for instability and drawdown.

    score = (mean - penalty * std) of per-window R per month, divided by (1 + DD_R / scale),
    multiplied by the deflated Sharpe probability (the multiple-testing penalty).
    """
    rpm = [r / m for r, m in zip(window_r, window_months) if m > 0]
    if not rpm:
        return 0.0
    arr = np.asarray(rpm, dtype=float)
    stability = float(arr.mean() - variance_penalty * (arr.std(ddof=1) if arr.size > 1 else 0.0))
    dd_factor = 1.0 / (1.0 + max(max_dd_r, 0.0) / dd_scale_r)
    if stability <= 0:
        return stability * (2.0 - dd_factor)  # negative scores get worse, never better, with drawdown
    return stability * dd_factor * dsr
