"""Risk profiles: the documented numbers behind ``risk_profile`` (conservative, balanced,
aggressive) and the single function that turns profile + optional user caps + hard ceilings
into the effective limits every other module uses.

All values are percent of equity (risk capital for sizing). A user cap in config.json can
only make a limit STRICTER than the profile; nothing can exceed ``limits.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from . import limits

PROFILE_NAMES = ("conservative", "balanced", "aggressive")


@dataclass(frozen=True)
class Profile:
    name: str
    t1_risk_pct_demo: float      # micro risk per exploration / probation trade
    t1_risk_pct_real: float
    t2_risk_pct: float           # active tier
    t3_risk_pct: float           # proven tier: the per-trade cap of the profile
    daily_loss_pct: float
    max_open_risk_pct: float
    drawdown_breaker_pct: float  # hard breaker from the cash-flow adjusted peak
    floor_drawdown_pct: float    # profit-ratchet floor starts this far below starting capital
    lock_fraction: float         # share of every new high locked into the floor
    house_money_max: float       # risk multiplier ceiling when the cushion is comfortable
    kelly_fraction: float        # fraction of the (shrunk) Kelly bet allowed for T2/T3
    explore_day_pct_demo: float
    explore_week_pct_demo: float
    explore_day_pct_real: float
    explore_week_pct_real: float
    max_positions: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PROFILES: dict[str, Profile] = {
    "conservative": Profile("conservative", 0.05, 0.03, 0.25, 0.50, 1.5, 1.5, 8.0, 5.0, 0.50, 1.00, 0.15,
                            0.15, 0.40, 0.07, 0.20, 3),
    "balanced": Profile("balanced", 0.10, 0.05, 0.35, 1.00, 2.5, 2.5, 12.0, 8.0, 0.40, 1.25, 0.25,
                        0.20, 0.50, 0.10, 0.30, 4),
    "aggressive": Profile("aggressive", 0.10, 0.08, 0.50, 1.75, 4.0, 3.5, 18.0, 12.0, 0.30, 1.50, 0.35,
                          0.30, 0.80, 0.15, 0.50, 5),
}


def _check_profile(p: Profile) -> None:
    """Defence in depth: a profile that exceeds a hard ceiling is a code bug."""
    assert p.t3_risk_pct <= limits.MAX_RISK_PER_TRADE_PCT, p
    assert max(p.t1_risk_pct_demo, p.t1_risk_pct_real) <= limits.MAX_EXPLORATION_TRADE_PCT, p
    assert p.t1_risk_pct_real <= p.t2_risk_pct <= p.t3_risk_pct, p
    assert p.daily_loss_pct <= limits.MAX_DAILY_LOSS_PCT, p
    assert p.max_open_risk_pct <= limits.MAX_TOTAL_OPEN_RISK_PCT, p
    assert p.floor_drawdown_pct < p.drawdown_breaker_pct <= limits.MAX_DRAWDOWN_FROM_PEAK_PCT, p
    assert 0.0 <= p.lock_fraction <= limits.MAX_LOCK_FRACTION, p
    assert 1.0 <= p.house_money_max <= limits.MAX_HOUSE_MONEY, p
    assert 0.0 < p.kelly_fraction <= limits.MAX_KELLY_FRACTION, p
    assert max(p.explore_day_pct_demo, p.explore_day_pct_real) <= limits.MAX_EXPLORATION_DAY_PCT, p
    assert max(p.explore_week_pct_demo, p.explore_week_pct_real) <= limits.MAX_EXPLORATION_WEEK_PCT, p
    assert 1 <= p.max_positions <= limits.MAX_OPEN_POSITIONS, p


for _profile in PROFILES.values():
    _check_profile(_profile)


def _cap(*values: Optional[float]) -> float:
    return min(float(v) for v in values if v is not None)


@dataclass(frozen=True)
class EffectiveLimits:
    """What the risk layer, the sizer and the guard actually use (already clamped)."""

    profile: str
    is_real: bool
    t1_risk_pct: float
    t2_risk_pct: float
    t3_risk_pct: float
    daily_loss_pct: float
    max_open_risk_pct: float
    drawdown_breaker_pct: float
    floor_drawdown_pct: float
    lock_fraction: float
    house_money_max: float
    kelly_fraction: float
    explore_day_pct: float
    explore_week_pct: float
    explore_trade_pct: float
    max_positions: int
    affordability_cap_pct: float

    @property
    def max_trade_risk_pct(self) -> float:
        """The largest per-trade risk any tier may use (the T3 cap)."""
        return self.t3_risk_pct

    def tier_risk_pct(self, tier: str) -> float:
        return {"T1": self.t1_risk_pct, "T2": self.t2_risk_pct, "T3": self.t3_risk_pct}.get(tier, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def effective_limits(cfg: Any, is_real: bool) -> EffectiveLimits:
    """Profile numbers, tightened by any user cap in config.json, clamped to the hard ceilings."""
    p = PROFILES[cfg.risk_profile]
    cap = cfg.capital
    user_trade = cfg.risk_per_trade_pct_real if is_real else cfg.risk_per_trade_pct_demo
    t3 = _cap(p.t3_risk_pct, user_trade, limits.MAX_RISK_PER_TRADE_PCT)
    t2 = min(p.t2_risk_pct, t3)
    t1_profile = p.t1_risk_pct_real if is_real else p.t1_risk_pct_demo
    t1 = _cap(t1_profile, cfg.tiers.t1_risk_pct, t2, limits.MAX_EXPLORATION_TRADE_PCT)
    user_daily = cfg.max_daily_loss_pct_real if is_real else cfg.max_daily_loss_pct_demo
    daily = _cap(p.daily_loss_pct, user_daily, limits.MAX_DAILY_LOSS_PCT)
    open_risk = _cap(p.max_open_risk_pct, cfg.max_total_open_risk_pct, limits.MAX_TOTAL_OPEN_RISK_PCT)
    breaker = _cap(p.drawdown_breaker_pct, cfg.max_drawdown_from_peak_pct, limits.MAX_DRAWDOWN_FROM_PEAK_PCT)
    floor_dd = _cap(p.floor_drawdown_pct, cap.floor_drawdown_pct, breaker * 0.9)
    lock = p.lock_fraction if cap.lock_fraction is None else cap.lock_fraction
    house = _cap(p.house_money_max, cap.house_money_max, limits.MAX_HOUSE_MONEY)
    kelly = _cap(p.kelly_fraction, cap.kelly_fraction, limits.MAX_KELLY_FRACTION)
    explore_day = p.explore_day_pct_real if is_real else p.explore_day_pct_demo
    explore_week = p.explore_week_pct_real if is_real else p.explore_week_pct_demo
    t = cfg.tiers
    explore_day = _cap(explore_day, t.exploration_day_pct, limits.MAX_EXPLORATION_DAY_PCT)
    explore_week = _cap(explore_week, t.exploration_week_pct, limits.MAX_EXPLORATION_WEEK_PCT)
    explore_trade = _cap(explore_day, limits.MAX_EXPLORATION_TRADE_PCT)
    positions = min(p.max_positions, cfg.max_open_positions_total or p.max_positions, limits.MAX_OPEN_POSITIONS)
    afford = _cap(t3 if cap.affordability_cap_pct is None else cap.affordability_cap_pct, t3,
                  limits.MAX_RISK_PER_TRADE_PCT)
    return EffectiveLimits(p.name, is_real, t1, t2, t3, daily, open_risk, breaker, floor_dd,
                           min(max(lock, 0.0), limits.MAX_LOCK_FRACTION), max(house, 1.0), kelly, explore_day,
                           explore_week, explore_trade, positions, afford)
