"""Expected value of a candidate signal at decision time, in R, after costs.

    EV = P(win) x avg_win_R - P(loss) x avg_loss_R - cost_R

The arm's posterior is the anchor: its mean is already net of the costs the shadow book paid, and
it carries the shrinkage that protects against lucky arms. From that mean and the trade's geometry
(reward = TP distance / SL distance) comes the break-even-consistent win rate; the arm's own wins
and losses then pull the estimate only as far as the posterior trusts them, using the same
shrinkage. ``cost_R`` is therefore only the cost ABOVE what history already paid, for example a
spread wider than usual right now.

With no observations the estimate equals the posterior mean. An estimate, not a forecast.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional, Sequence

MAX_PRIOR_STRENGTH = 1e6


@dataclass(frozen=True)
class EvEstimate:
    p_win: float
    avg_win_r: float
    avg_loss_r: float
    cost_r: float
    ev_r: float

    def to_dict(self) -> dict[str, float]:
        return {k: round(v, 5) for k, v in asdict(self).items()}


def expected_value(p_win: float, avg_win_r: float, avg_loss_r: float, cost_r: float = 0.0) -> float:
    return p_win * avg_win_r - (1.0 - p_win) * avg_loss_r - cost_r


def estimate(mean_r: float, reward_r: float, outcomes: Sequence[float], shrinkage: float,
             extra_cost_r: float = 0.0) -> EvEstimate:
    """``shrinkage`` is ``Posterior.shrinkage``: 1 = ignore the arm's own outcomes, 0 = trust them."""
    reward = max(float(reward_r), 1e-6)
    prior_p = min(max((float(mean_r) + 1.0) / (reward + 1.0), 0.0), 1.0)
    rs = [float(r) for r in outcomes if math.isfinite(float(r))]
    n = len(rs)
    b = min(max(float(shrinkage), 0.0), 1.0)
    k = MAX_PRIOR_STRENGTH if b >= 1.0 or n == 0 else min(n * b / (1.0 - b), MAX_PRIOR_STRENGTH)
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    p = (len(wins) + k * prior_p) / (n + k)
    avg_win = (sum(wins) + k * reward) / (len(wins) + k)
    avg_loss = (sum(losses) + k) / (len(losses) + k)
    cost = max(float(extra_cost_r), 0.0)
    return EvEstimate(p, avg_win, avg_loss, cost, expected_value(p, avg_win, avg_loss, cost))


def rank_key(ev_r: Optional[float]) -> float:
    """Sort key: best EV per unit of risk first. R is already per unit of risk; unknown goes last."""
    return -ev_r if ev_r is not None and math.isfinite(ev_r) else math.inf
