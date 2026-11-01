"""Risk profiles: the documented numbers behind ``risk_profile`` (conservative, balanced,
aggressive) and the single function that turns profile + optional user caps + hard ceilings
into the effective limits every other module uses.

All values are percent of equity (risk capital for sizing). A user cap in config.json can
only make a limit STRICTER than the profile; nothing can exceed ``limits.py``.

=====================  ============  ========  ==========
number                 conservative  balanced  aggressive
=====================  ============  ========  ==========
T1 risk                0.10          0.25      0.50
T2 risk                0.25          0.50      1.00
T3 risk                0.50          1.00      2.00
minimum viable cap     0.75          1.25      2.00
daily loss limit       2.50          3.50      5.00
weekly soft limit      5.00          7.00      10.00
total open risk        1.50          2.50      4.00
exploration trades/d   2             3         4
exploration trades/w   6             9         12
profit ratchet lock    0.50          0.40      0.30
=====================  ============  ========  ==========
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from . import limits

PROFILE_NAMES = ("conservative", "balanced", "aggressive")


@dataclass(frozen=True)
class Profile:
    name: str
    t1_risk_pct: float           # probation and exploration trades
    t2_risk_pct: float           # active tier
    t3_risk_pct: float           # proven tier: the per-trade cap of the profile
    min_viable_cap_pct: float    # largest minimum-lot risk accepted when it exceeds the tier risk
    daily_loss_pct: float
    weekly_loss_pct: float       # soft limit: new entries pause until next week
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
    explore_trades_day: int      # the exploration budget pays for at least this many trades
    explore_trades_week: int
    max_positions: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PROFILES: dict[str, Profile] = {
    "conservative": Profile("conservative", t1_risk_pct=0.10, t2_risk_pct=0.25, t3_risk_pct=0.50,
                            min_viable_cap_pct=0.75, daily_loss_pct=2.5, weekly_loss_pct=5.0, max_open_risk_pct=1.5,
                            drawdown_breaker_pct=8.0, floor_drawdown_pct=5.0, lock_fraction=0.50, house_money_max=1.00,
                            kelly_fraction=0.15, explore_day_pct_demo=0.15, explore_week_pct_demo=0.40,
                            explore_day_pct_real=0.07, explore_week_pct_real=0.20, explore_trades_day=2,
                            explore_trades_week=6, max_positions=3),
    "balanced": Profile("balanced", t1_risk_pct=0.25, t2_risk_pct=0.50, t3_risk_pct=1.00, min_viable_cap_pct=1.25,
                        daily_loss_pct=3.5, weekly_loss_pct=7.0, max_open_risk_pct=2.5, drawdown_breaker_pct=12.0,
                        floor_drawdown_pct=8.0, lock_fraction=0.40, house_money_max=1.25, kelly_fraction=0.25,
                        explore_day_pct_demo=0.20, explore_week_pct_demo=0.50, explore_day_pct_real=0.10,
                        explore_week_pct_real=0.30, explore_trades_day=3, explore_trades_week=9, max_positions=4),
    "aggressive": Profile("aggressive", t1_risk_pct=0.50, t2_risk_pct=1.00, t3_risk_pct=2.00, min_viable_cap_pct=2.00,
                          daily_loss_pct=5.0, weekly_loss_pct=10.0, max_open_risk_pct=4.0, drawdown_breaker_pct=18.0,
                          floor_drawdown_pct=12.0, lock_fraction=0.30, house_money_max=1.50, kelly_fraction=0.35,
                          explore_day_pct_demo=0.30, explore_week_pct_demo=0.80, explore_day_pct_real=0.15,
                          explore_week_pct_real=0.50, explore_trades_day=4, explore_trades_week=12, max_positions=5),
}


def _check_profile(p: Profile) -> None:
    """Defence in depth: a profile that exceeds a hard ceiling is a code bug."""
    assert p.t3_risk_pct <= limits.MAX_RISK_PER_TRADE_PCT, p
    assert p.min_viable_cap_pct <= limits.MAX_RISK_PER_TRADE_PCT, p
    assert p.min_viable_cap_pct <= p.max_open_risk_pct, p
    assert p.t1_risk_pct <= limits.MAX_EXPLORATION_TRADE_PCT, p
    assert p.t1_risk_pct <= p.t2_risk_pct <= p.t3_risk_pct, p
    assert p.daily_loss_pct <= limits.MAX_DAILY_LOSS_PCT, p
    assert p.daily_loss_pct <= p.weekly_loss_pct <= limits.MAX_WEEKLY_SOFT_LOSS_PCT, p
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
    min_viable_cap_pct: float = 0.0     # 0 means minimum-viable sizing is off
    weekly_loss_pct: float = limits.MAX_WEEKLY_SOFT_LOSS_PCT
    explore_trades_day: int = 0
    explore_trades_week: int = 0

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
    t1 = _cap(p.t1_risk_pct, cfg.tiers.t1_risk_pct, t2, limits.MAX_EXPLORATION_TRADE_PCT)
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
    positions = min(p.max_positions, cfg.max_open_positions_total or p.max_positions, limits.MAX_OPEN_POSITIONS)
    afford = _cap(t3 if cap.affordability_cap_pct is None else cap.affordability_cap_pct, t3,
                  limits.MAX_RISK_PER_TRADE_PCT)
    sizing, budget = cfg.sizing, cfg.budget
    # The minimum-viable cap is the one figure allowed above the tier risk: it can still never
    # exceed the hard ceiling, and a user trade cap binds it too.
    viable = 0.0
    if sizing.min_viable_enabled:
        viable = _cap(p.min_viable_cap_pct, sizing.min_viable_risk_cap_pct, user_trade, limits.MAX_RISK_PER_TRADE_PCT)
    weekly = _cap(p.weekly_loss_pct, budget.weekly_soft_limit_pct, limits.MAX_WEEKLY_SOFT_LOSS_PCT)
    trades_day = p.explore_trades_day if budget.min_trades_per_day is None else budget.min_trades_per_day
    trades_week = p.explore_trades_week if budget.min_trades_per_week is None else budget.min_trades_per_week
    return EffectiveLimits(p.name, is_real, t1, t2, t3, daily, open_risk, breaker, floor_dd,
                           min(max(lock, 0.0), limits.MAX_LOCK_FRACTION), max(house, 1.0), kelly, explore_day,
                           explore_week, t1, positions, afford, viable, weekly, trades_day, trades_week)
