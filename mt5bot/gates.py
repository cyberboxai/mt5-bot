"""Entry gates and scaling gates are different questions.

* Entry gates: may this arm place its first trades at the smallest size? Deliberately lighter,
  because a small trade is how real evidence is bought.
* Scaling gates: may this arm size above that? The strict machinery: live plus shadow evidence,
  the research verdict (deflated Sharpe and PBO), cost to edge on the lower bound of the posterior
  and the reality gap. Size grows only through these.

Exploration is a third, separate path: arms whose posterior mean is not below
``tiers.exploration_threshold`` may be drawn by Thompson sampling, paid from the trade-count budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

ENTRY_CODES = {
    "ENTRY_GATE_EDGE": "expectativa depois de custos não é positiva",
    "ENTRY_GATE_P": "probabilidade de expectativa positiva abaixo do mínimo",
    "ENTRY_GATE_COST": "vantagem não cobre o custo com a margem exigida",
    "ENTRY_GATE_EVIDENCE": "poucos trades fora da amostra",
}


@dataclass(frozen=True)
class EntryRules:
    min_mean_r: float = 0.0
    min_p: float = 0.55
    min_cost_to_edge: float = 1.2
    min_oos_trades: int = 30

    @classmethod
    def from_config(cls, gates: Any) -> "EntryRules":
        return cls(gates.min_mean_r, gates.min_p, gates.min_cost_to_edge, gates.min_oos_trades)


def cost_to_edge(edge_r: float, cost_r: Optional[float]) -> float:
    """Edge per unit of cost, both in R. inf with no cost, 0 when there is no edge."""
    if edge_r <= 0:
        return 0.0
    return math.inf if not cost_r or cost_r <= 0 else edge_r / cost_r


def entry_gate(post: Any, cost_r: Optional[float], rules: EntryRules) -> Optional[str]:
    """Reason code when the arm fails an entry gate, or None. ``post`` is an ``evidence.Posterior``."""
    if post is None:
        return "ENTRY_GATE_EVIDENCE"
    if post.n_prior + post.n_obs < rules.min_oos_trades:
        return "ENTRY_GATE_EVIDENCE"
    if post.mean <= rules.min_mean_r:
        return "ENTRY_GATE_EDGE"
    if post.p_positive < rules.min_p:
        return "ENTRY_GATE_P"
    if cost_r is not None and cost_to_edge(post.mean, cost_r) < rules.min_cost_to_edge:
        return "ENTRY_GATE_COST"
    return None
