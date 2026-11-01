"""Tail risk: what a stop loss does not protect against.

Stops do not protect against gaps, rollover spreads or a margin stop-out. These rules size and
schedule for that. Every output can only make the system smaller or stop it.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class TailRules:
    margin_stop_out_multiple: float = 3.0
    margin_call_multiple: float = 1.5
    gap_quantile: float = 0.99
    overnight_hours: float = 6.0
    small_account_equity: float = 25_000.0
    ladder_steps: tuple[tuple[float, float], ...] = ((3.0, 0.75), (6.0, 0.5), (10.0, 0.25))
    ladder_recovery_days: float = 3.0
    cvar_alpha: float = 0.95


# ---------------------------------------------------------------------- margin guard


def projected_margin_level(equity: float, margin_used: float, new_margin: float) -> float:
    """Margin level percent after opening. ``inf`` when nothing would be on margin."""
    total = max(margin_used, 0.0) + max(new_margin, 0.0)
    return math.inf if total <= 0 else equity / total * 100.0


def margin_guard(equity: float, margin_used: float, new_margin: float, stop_out_level: float,
                 rules: TailRules = TailRules()) -> Optional[str]:
    """Reason to refuse an entry, or None. The stop-out level comes from ``account_info``."""
    level = projected_margin_level(equity, margin_used, new_margin)
    floor = max(stop_out_level, 0.0) * rules.margin_stop_out_multiple
    if level < floor:
        return f"projected margin level {level:.0f}% < {floor:.0f}% ({rules.margin_stop_out_multiple:g}x stop-out)"
    return None


def margin_pressure(margin_level: float, call_level: float, rules: TailRules = TailRules()) -> bool:
    """True when the live margin level is trending toward the call level."""
    return 0 < margin_level < call_level * rules.margin_call_multiple


# ---------------------------------------------------------------- gap-aware sizing


def open_gaps(opens: np.ndarray, closes: np.ndarray, times: np.ndarray, min_seconds: float) -> np.ndarray:
    """Absolute relative gaps between a close and the next open across a break in trading."""
    o, c = np.asarray(opens, dtype=float), np.asarray(closes, dtype=float)
    t = np.asarray(times, dtype=np.int64)
    if len(o) < 2:
        return np.zeros(0)
    jumped = np.diff(t) >= min_seconds
    with np.errstate(divide="ignore", invalid="ignore"):
        gaps = np.abs(o[1:] / c[:-1] - 1.0)
    selected: np.ndarray = gaps[jumped]
    finite: np.ndarray = selected[np.isfinite(selected)]
    return finite


def gap_allowance_points(gaps: np.ndarray, price: float, point: float, rules: TailRules = TailRules()) -> float:
    """Extra adverse distance, in points, to size for when a position may stay over a break."""
    if gaps.size == 0 or point <= 0:
        return 0.0
    return float(np.quantile(gaps, rules.gap_quantile)) * price / point


def may_cross_break(timeframe_seconds: int, expected_hold_bars: int, hours_to_break: float,
                    rules: TailRules = TailRules()) -> bool:
    """Could a trade from this arm still be open when trading next stops?"""
    hold_hours = timeframe_seconds * expected_hold_bars / 3600.0
    return hold_hours >= min(hours_to_break, rules.overnight_hours)


# ------------------------------------------------------------------------- CVaR


def cvar(returns: Sequence[float], alpha: float = 0.95) -> Optional[float]:
    """Mean of the worst (1 - alpha) outcomes, as a positive loss. An estimate from past trades:
    the true tail can be worse than anything in the sample."""
    x = np.sort(np.asarray(returns, dtype=float))
    if x.size < 20:
        return None
    k = max(1, int(math.ceil(round(x.size * (1.0 - alpha), 9))))
    return float(-x[:k].mean())


def portfolio_cvar_pct(open_risk_pct: Sequence[float], trade_r: Sequence[float], alpha: float = 0.95, paths: int = 4000,
                       seed: int = 11) -> Optional[float]:
    """CVaR of the open book in percent of equity, from resampled trade outcomes (an estimate)."""
    outcomes = np.asarray(trade_r, dtype=float)
    risks = np.asarray(open_risk_pct, dtype=float)
    if outcomes.size < 20 or risks.size == 0:
        return None
    rng = np.random.default_rng(seed)
    draws = rng.choice(outcomes, size=(paths, risks.size))
    return cvar((draws * risks).sum(axis=1).tolist(), alpha)


# ------------------------------------------------------------------ de-risk ladder


@dataclass
class LadderState:
    multiplier: float = 1.0
    last_step_up: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: Optional[str]) -> "LadderState":
        if not text:
            return cls()
        try:
            data = json.loads(text)
            return cls(float(data["multiplier"]), float(data.get("last_step_up", 0.0)))
        except (ValueError, TypeError, KeyError):
            return cls(min(s[1] for s in TailRules().ladder_steps), 0.0)   # corrupt: assume the worst step


def target_multiplier(drawdown_pct: float, rules: TailRules = TailRules()) -> float:
    multiplier = 1.0
    for depth, value in rules.ladder_steps:
        if drawdown_pct >= depth:
            multiplier = value
    return multiplier


def update_ladder(state: LadderState, drawdown_pct: float, now: float, rules: TailRules = TailRules()) -> LadderState:
    """Steps down at once as drawdown deepens; steps back up one rung at a time, slowly."""
    target = target_multiplier(drawdown_pct, rules)
    if target < state.multiplier:
        return LadderState(target, now)
    if target > state.multiplier and now - state.last_step_up >= rules.ladder_recovery_days * 86400:
        rungs = sorted({1.0, *(s[1] for s in rules.ladder_steps)})
        above = next((r for r in rungs if r > state.multiplier), 1.0)
        return LadderState(min(above, target), now)
    return state
