"""Evidence tiers and the exploration budget.

Tiers (risk as percent of risk capital, see ``profiles``):

* T0 Shadow     every new arm: paper trades only;
* T1 Probation  ``P(E>0) >= t1_enter_p`` with at least ``t1_min_evidence`` trades of its own
                (usually the walk-forward prior), or chosen by Thompson sampling for one
                EXPLORATION trade: micro risk, paid from the budget;
* T2 Active     ``P(E>0) >= t2_enter_p`` with at least ``t2_min_trades`` shadow+live trades;
* T3 Proven     ``P(E>0) >= t3_enter_p``, at least ``t3_min_trades`` trades and a drawdown
                inside the budget.

Promotions move one tier at a time, need ``promote_cooldown_hours`` since the arm's last
change and use the enter thresholds; demotions use LOWER exit thresholds (hysteresis), are
immediate and also fire on a drawdown beyond the tier budget. Every change carries the
metrics that justified it. Open positions keep their SL/TP and management whatever happens
to their arm's tier.

The exploration budget is the visible price of information: the net realised loss of T1
trades in the current server day and week, plus the risk still open on T1 trades, plus the
new trade's risk, must fit inside the daily and the weekly budgets. When it is spent T1
pauses until the next window.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from .evidence import Posterior

TIERS = ("T0", "T1", "T2", "T3")
TIER_INDEX = {t: i for i, t in enumerate(TIERS)}
TIER_PT = {"T0": "Sombra", "T1": "Probação", "T2": "Ativo", "T3": "Comprovado"}


@dataclass(frozen=True)
class TierSettings:
    t1_enter_p: float = 0.55
    t1_exit_p: float = 0.50
    t1_min_evidence: int = 20
    t2_enter_p: float = 0.80
    t2_exit_p: float = 0.70
    t2_min_trades: int = 50
    t2_max_dd_r: float = 6.0
    t3_enter_p: float = 0.93
    t3_exit_p: float = 0.85
    t3_min_trades: int = 100
    t3_max_dd_r: float = 12.0
    promote_cooldown_hours: float = 4.0

    @classmethod
    def from_config(cls, tiers: Any, t1_enter_override: Optional[float] = None) -> "TierSettings":
        values = {f: getattr(tiers, f) for f in cls.__dataclass_fields__ if hasattr(tiers, f)}
        if t1_enter_override is not None:
            values["t1_enter_p"] = t1_enter_override
            values["t1_exit_p"] = max(0.50, min(values.get("t1_exit_p", cls.t1_exit_p), t1_enter_override - 0.02))
        return cls(**values)


@dataclass(frozen=True)
class TierDecision:
    tier: str
    previous: str
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.tier != self.previous

    @property
    def direction(self) -> Optional[str]:
        if not self.changed:
            return None
        return "up" if TIER_INDEX[self.tier] > TIER_INDEX[self.previous] else "down"


def decide_tier(current: str, post: Posterior, settings: TierSettings, now: float, last_change: Optional[float],
                pinned_until: Optional[float] = None, dd_since_change_r: float = 0.0) -> TierDecision:
    """Next tier for one arm. Demotions first (immediate), then at most one promotion step."""
    if current not in TIER_INDEX:
        current = "T0"
    p, n = post.p_positive, post.n_obs
    detail = f"P(E>0)={p:.2f}, E={post.mean:+.3f}R [{post.lo:+.3f}, {post.hi:+.3f}], {n} trades"
    if current == "T3" and (p < settings.t3_exit_p or post.dd_r > settings.t3_max_dd_r):
        why = (f"drawdown {post.dd_r:.1f}R > budget {settings.t3_max_dd_r:.1f}R" if post.dd_r > settings.t3_max_dd_r
               else f"P(E>0) {p:.2f} < {settings.t3_exit_p:.2f}")
        return TierDecision("T2", current, f"demote: {why}; {detail}")
    if current == "T2" and (p < settings.t2_exit_p or dd_since_change_r > settings.t2_max_dd_r):
        why = (f"drawdown since promotion {dd_since_change_r:.1f}R > budget {settings.t2_max_dd_r:.1f}R"
               if dd_since_change_r > settings.t2_max_dd_r else f"P(E>0) {p:.2f} < {settings.t2_exit_p:.2f}")
        return TierDecision("T1", current, f"demote: {why}; {detail}")
    if current == "T1" and p < settings.t1_exit_p:
        return TierDecision("T0", current, f"demote: P(E>0) {p:.2f} < {settings.t1_exit_p:.2f}; {detail}")
    if pinned_until is not None and now < pinned_until:
        return TierDecision(current, current, "pinned by a manual decision")
    if last_change is not None and now - last_change < settings.promote_cooldown_hours * 3600:
        return TierDecision(current, current, "promotion cooldown")
    evidence = post.n_obs + post.n_prior
    if current == "T0" and p >= settings.t1_enter_p and evidence >= settings.t1_min_evidence:
        source = f"{post.n_prior} walk-forward + {post.n_obs} observed trades"
        return TierDecision("T1", current, f"promote: P(E>0) {p:.2f} >= {settings.t1_enter_p:.2f} from {source}; "
                                           f"{detail}")
    if current == "T1" and p >= settings.t2_enter_p and n >= settings.t2_min_trades:
        return TierDecision("T2", current, f"promote: P(E>0) {p:.2f} >= {settings.t2_enter_p:.2f} with {n} >= "
                                           f"{settings.t2_min_trades} trades; {detail}")
    if (current == "T2" and p >= settings.t3_enter_p and n >= settings.t3_min_trades
            and post.dd_r <= settings.t3_max_dd_r):
        return TierDecision("T3", current, f"promote: P(E>0) {p:.2f} >= {settings.t3_enter_p:.2f} with {n} >= "
                                           f"{settings.t3_min_trades} trades, drawdown {post.dd_r:.1f}R in budget; "
                                           f"{detail}")
    return TierDecision(current, current, "")


# ------------------------------------------------------------------ exploration


def thompson_select(post: Posterior, rng: np.random.Generator, threshold: float = 0.0) -> tuple[bool, float]:
    """Exploration choice for one signal: draw the arm's expectancy from its posterior and
    explore when the draw clears ``threshold``. Uncertain arms with plausible upside win more
    often; arms the evidence has turned against are drawn below zero almost always."""
    draw = post.thompson(rng)
    return draw > threshold, draw


def server_day_start(server_ts: float) -> float:
    return float(int(server_ts) // 86400 * 86400)


def server_week_start(server_ts: float) -> float:
    dt = datetime.fromtimestamp(server_day_start(server_ts), tz=timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    return monday.timestamp()


@dataclass(frozen=True)
class BudgetRoom:
    day_budget: float
    week_budget: float
    day_used: float
    week_used: float
    open_risk: float

    @property
    def room(self) -> float:
        return max(0.0, min(self.day_budget - self.day_used, self.week_budget - self.week_used) - self.open_risk)

    @property
    def day_fraction(self) -> float:
        return min(1.0, (self.day_used + self.open_risk) / self.day_budget) if self.day_budget > 0 else 1.0

    @property
    def week_fraction(self) -> float:
        return min(1.0, (self.week_used + self.open_risk) / self.week_budget) if self.week_budget > 0 else 1.0

    def allows(self, risk_money: float) -> bool:
        return risk_money <= self.room + 1e-9

    def to_dict(self) -> dict:
        return {"day_budget": round(self.day_budget, 2), "week_budget": round(self.week_budget, 2),
                "day_used": round(self.day_used, 2), "week_used": round(self.week_used, 2),
                "open_risk": round(self.open_risk, 2), "room": round(self.room, 2),
                "day_fraction": round(self.day_fraction, 4), "week_fraction": round(self.week_fraction, 4)}


def exploration_room(t1_trades: Iterable[Any], server_now: float, server_offset: float, day_equity: float,
                     week_equity: float, day_pct: float, week_pct: float) -> BudgetRoom:
    """Budget left for T1 risk. ``t1_trades`` are live journal rows (TradeRow-like) with tier T1.

    Net realised P&L counts (a winning T1 trade gives room back, never more than the budget);
    open T1 trades count with the full money at stake to their stop."""
    day_start = server_day_start(server_now) - server_offset
    week_start = server_week_start(server_now) - server_offset
    day_net = week_net = open_risk = 0.0
    for row in t1_trades:
        if row.status == "open":
            open_risk += max(float(row.risk_money), 0.0)
            continue
        closed = float(row.close_ts or 0.0)
        pnl = float(row.pnl or 0.0)
        if closed >= week_start:
            week_net += pnl
            if closed >= day_start:
                day_net += pnl
    day_budget = max(day_equity, 0.0) * day_pct / 100.0
    week_budget = max(week_equity, 0.0) * week_pct / 100.0
    return BudgetRoom(day_budget, week_budget, max(0.0, -day_net), max(0.0, -week_net), open_risk)


def tier_counts(tiers: Sequence[str]) -> dict[str, int]:
    out = {t: 0 for t in TIERS}
    for t in tiers:
        if t in out:
            out[t] += 1
    return out


def p_label(p: float) -> str:
    if not math.isfinite(p):
        return "?"
    return f"{p:.0%}"
