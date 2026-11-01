"""Project governance: when the evidence says stop, the system stops risking real money.

The question is the only one that matters to the owner: after every cost, is the expectancy of the
real account positive? With a confidence interval over net P&L per trade:

* upper bound below zero: **no edge detected**, and the real account goes research-only;
* lower bound above zero: **edge plausible** (said with the same rigor, uncertainty included);
* anything else: **evidence insufficient**.

The check runs continuously once enough trades exist, not only at the evaluation horizon, because a
system without edge should not get to spend the whole horizon proving it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Optional, Sequence

INSUFFICIENT = "evidence_insufficient"
PLAUSIBLE = "edge_plausible"
NO_EDGE = "no_edge"
VERDICT_PT = {INSUFFICIENT: "evidência insuficiente", PLAUSIBLE: "vantagem plausível", NO_EDGE: "sem vantagem detectada"}
_N = NormalDist()


@dataclass(frozen=True)
class GovernanceRules:
    horizon_trades: int = 200
    horizon_days: float = 91.0
    rolling_min_trades: int = 30
    confidence: float = 0.95


@dataclass(frozen=True)
class Verdict:
    status: str
    n: int
    mean: Optional[float]
    ci_low: Optional[float]
    ci_high: Optional[float]
    horizon_reached: bool
    progress: float
    stop_real_trading: bool
    reason: str


def mean_interval(values: Sequence[float], confidence: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
    n = len(values)
    if n == 0:
        return None, None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None, None
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = _N.inv_cdf(0.5 + confidence / 2.0) * math.sqrt(var / n)
    return mean, mean - half, mean + half


def evaluate(net_pnls: Sequence[float], first_trade_ts: Optional[float], now: float, rules: GovernanceRules) -> Verdict:
    """``net_pnls``: money per trade after commission, swap and slippage, oldest first."""
    n = len(net_pnls)
    days = (now - first_trade_ts) / 86400.0 if first_trade_ts is not None else 0.0
    progress = min(max(n / rules.horizon_trades, days / rules.horizon_days), 1.0)
    horizon = progress >= 1.0
    mean, low, high = mean_interval(net_pnls, rules.confidence)
    if n < rules.rolling_min_trades or high is None or low is None:
        return Verdict(INSUFFICIENT, n, mean, low, high, horizon, progress, False,
                       f"{n} trades (need at least {rules.rolling_min_trades} for a verdict)")
    if high < 0:
        return Verdict(NO_EDGE, n, mean, low, high, horizon, progress, True,
                       f"net expectancy {mean:+.2f} per trade, {rules.confidence:.0%} interval [{low:+.2f}, {high:+.2f}] "
                       "entirely below zero after all costs")
    if low > 0:
        return Verdict(PLAUSIBLE, n, mean, low, high, horizon, progress, False,
                       f"net expectancy {mean:+.2f} per trade, {rules.confidence:.0%} interval [{low:+.2f}, {high:+.2f}] "
                       "above zero; a past interval promises nothing about the future")
    return Verdict(INSUFFICIENT, n, mean, low, high, horizon, progress, False,
                   f"net expectancy {mean:+.2f} per trade, interval [{low:+.2f}, {high:+.2f}] includes zero")
