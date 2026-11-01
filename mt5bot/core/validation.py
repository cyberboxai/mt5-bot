"""Statistical honesty: purged validation and selection-bias corrections.

* CPCV (Lopez de Prado, 2018): split the timeline into N groups and test on every combination of
  k groups, purging training trades that overlap a test group and embargoing what follows it.
* Probabilistic and Deflated Sharpe (Bailey and Lopez de Prado, 2012/2014): is the Sharpe above
  what the best of N useless configurations would show by luck?
* PBO by CSCV (Bailey et al., 2017): how often does the configuration that looks best in-sample
  land in the bottom half out-of-sample?
* Minimum track record length: how many observations a claimed Sharpe needs to be believable.

Sharpe ratios here are per observation (per trade), not annualised.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Iterator, Optional, Sequence

import numpy as np

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class SharpeStats:
    sharpe: float
    n: int
    skew: float
    kurtosis: float      # raw kurtosis: 3 for a normal distribution


def sharpe_stats(returns: Sequence[float]) -> SharpeStats:
    x = np.asarray(returns, dtype=float)
    n = int(x.size)
    if n < 2:
        return SharpeStats(0.0, n, 0.0, 3.0)
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return SharpeStats(0.0, n, 0.0, 3.0)
    z = (x - x.mean()) / sd
    return SharpeStats(float(x.mean() / sd), n, float(np.mean(z ** 3)), float(np.mean(z ** 4)))


def _sr_variance_term(s: SharpeStats) -> float:
    term = 1.0 - s.skew * s.sharpe + (s.kurtosis - 1.0) / 4.0 * s.sharpe ** 2
    return max(term, 1e-9)


def probabilistic_sharpe(s: SharpeStats, benchmark: float = 0.0) -> float:
    """P(true Sharpe > benchmark) given the sample."""
    if s.n < 2:
        return 0.0
    return _N.cdf((s.sharpe - benchmark) * math.sqrt(s.n - 1) / math.sqrt(_sr_variance_term(s)))


def expected_max_sharpe(n_trials: int, trial_variance: float) -> float:
    """Expected best Sharpe among ``n_trials`` configurations with no skill at all."""
    if n_trials < 2 or trial_variance <= 0:
        return 0.0
    n = float(n_trials)
    a = _N.inv_cdf(1.0 - 1.0 / n)
    b = _N.inv_cdf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(trial_variance) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(s: SharpeStats, n_trials: int, trial_variance: float) -> float:
    """Probabilistic Sharpe against the luck of the best of ``n_trials``.

    The sampling variance of a Sharpe under the null (1 / n) is a floor on the trial variance:
    correlated configurations look less dispersed than they are independent, and counting them
    as independent is the conservative direction."""
    floor = 1.0 / max(s.n, 2)
    return probabilistic_sharpe(s, expected_max_sharpe(n_trials, max(trial_variance, floor)))


def min_track_record(s: SharpeStats, target: float = 0.0, confidence: float = 0.95) -> float:
    """Observations needed for the observed Sharpe to beat ``target`` at this confidence."""
    if s.sharpe <= target:
        return math.inf
    z = _N.inv_cdf(confidence)
    return 1.0 + _sr_variance_term(s) * (z / (s.sharpe - target)) ** 2


# ---------------------------------------------------------------------------- CPCV


@dataclass(frozen=True)
class CpcvSplit:
    train_groups: tuple[int, ...]
    test_groups: tuple[int, ...]


def group_bounds(n_bars: int, n_groups: int) -> list[tuple[int, int]]:
    edges = np.linspace(0, n_bars, n_groups + 1).astype(int)
    return [(int(edges[i]), int(edges[i + 1])) for i in range(n_groups)]


def cpcv_splits(n_groups: int, k_test: int) -> Iterator[CpcvSplit]:
    groups = range(n_groups)
    for test in itertools.combinations(groups, k_test):
        yield CpcvSplit(tuple(g for g in groups if g not in test), test)


def n_paths(n_groups: int, k_test: int) -> int:
    """Complete out-of-sample paths CPCV yields: C(N-1, k-1)."""
    return math.comb(n_groups - 1, k_test - 1)


def purged_train_mask(entries: np.ndarray, exits: np.ndarray, test_bounds: Sequence[tuple[int, int]],
                      embargo: int) -> np.ndarray:
    """True for trades usable in training: neither overlapping a test group nor inside the embargo
    after one. Without this, a label that spans the boundary leaks the test set into training."""
    keep = np.ones(entries.shape, dtype=bool)
    for start, end in test_bounds:
        keep &= ~((entries < end + embargo) & (exits >= start))
    return keep


def in_groups_mask(entries: np.ndarray, bounds: Sequence[tuple[int, int]]) -> np.ndarray:
    mask = np.zeros(entries.shape, dtype=bool)
    for start, end in bounds:
        mask |= (entries >= start) & (entries < end)
    return mask


# ----------------------------------------------------------------------------- PBO


def pbo_cscv(performance: np.ndarray, n_blocks: int = 10) -> Optional[float]:
    """Probability of backtest overfitting. ``performance`` has one row per time step and one column
    per configuration. Returns None when there is too little to compare."""
    m = np.asarray(performance, dtype=float)
    if m.ndim != 2 or m.shape[1] < 2 or m.shape[0] < n_blocks:
        return None
    n_blocks -= n_blocks % 2
    blocks = np.array_split(np.arange(m.shape[0]), n_blocks)
    totals = np.stack([m[idx].sum(axis=0) for idx in blocks])    # blocks x configurations
    overfit = count = 0
    all_blocks = frozenset(range(n_blocks))
    for chosen in itertools.combinations(range(n_blocks), n_blocks // 2):
        train = list(chosen)
        test = sorted(all_blocks - set(chosen))
        best = int(np.argmax(totals[train].sum(axis=0)))
        oos = totals[test].sum(axis=0)
        rank = float(np.sum(oos < oos[best]) + 0.5 * (np.sum(oos == oos[best]) - 1) + 1.0)
        relative = rank / (m.shape[1] + 1.0)
        overfit += relative <= 0.5
        count += 1
    return overfit / count if count else None


# ----------------------------------------------------------------- triple barrier


@dataclass(frozen=True)
class BarrierLabel:
    label: int        # +1 take profit, -1 stop loss, 0 time stop
    exit_index: int
    r: float          # result in R, after costs


def triple_barrier(side: int, entry_index: int, entry_price: float, sl: float, tp: float, highs: np.ndarray,
                   lows: np.ndarray, closes: np.ndarray, max_bars: int, cost_r: float = 0.0,
                   spread: float = 0.0) -> BarrierLabel:
    """Label one trade exactly as execution would resolve it: real SL, real TP and a time stop, with
    the stop assumed first when one bar touches both. SELL positions trigger on the ask."""
    risk = abs(entry_price - sl)
    if risk <= 0:
        raise ValueError("stop distance must be positive")
    last = min(entry_index + max_bars, len(closes) - 1)
    shift = 0.0 if side > 0 else spread
    for i in range(entry_index, last + 1):
        high, low = float(highs[i]) + shift, float(lows[i]) + shift
        if (low <= sl) if side > 0 else (high >= sl):
            return BarrierLabel(-1, i, (sl - entry_price) * side / risk - cost_r)
        if (high >= tp) if side > 0 else (low <= tp):
            return BarrierLabel(1, i, (tp - entry_price) * side / risk - cost_r)
    exit_price = float(closes[last]) + shift
    return BarrierLabel(0, last, (exit_price - entry_price) * side / risk - cost_r)
