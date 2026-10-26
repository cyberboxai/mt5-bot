"""Capital that adapts to any account size: risk capital, the profit ratchet, house money,
anti-martingale sizing, affordability of minimum lots and order splitting.

No money amount is hard-coded: everything derives from current equity, the cash-flow
adjusted starting capital and high-water mark, and the symbol's contract specification.

Profit ratchet (a CPPI-style floor):

    C0  starting capital (cash-flow adjusted: deposits add, withdrawals subtract)
    H   high-water mark of equity (cash-flow adjusted)
    G   allowed give-back = floor_drawdown x C0 + (1 - lock_fraction) x max(0, H - C0)
    F   protected floor  = H - G         (starts floor_drawdown below C0, then rises by
                                          lock_fraction of every new high)
    cushion = equity - F                 (the risk capital)

The cushion factor ``cushion / (floor_drawdown x H)`` scales tier risk: 1 at the start, below
1 after losses (anti-martingale), up to ``house_money_max`` when profits made the cushion
comfortable. At the floor (cushion 0) new entries stop for the day (soft breaker); the hard
peak-drawdown breaker in ``risk.EquityGuard`` stays as the last line of defence.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Optional, Sequence

from . import limits
from .profiles import EffectiveLimits

LARGE_ACCOUNT_REF = 100_000.0   # account-currency units above which aggressiveness is damped
LARGE_ACCOUNT_DAMPING = 0.15    # per decade above the reference


@dataclass
class CapitalState:
    c0: Optional[float] = None
    hwm: Optional[float] = None
    cash_flow_total: float = 0.0
    soft_stop_day: Optional[str] = None
    epoch_started: Optional[float] = None
    last_equity: Optional[float] = None

    @classmethod
    def from_json(cls, text: Optional[str]) -> "CapitalState":
        if not text:
            return cls()
        try:
            data = json.loads(text)
            fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
            state = cls(**fields)
        except (ValueError, TypeError):
            return cls()
        for name in ("c0", "hwm", "last_equity"):
            value = getattr(state, name)
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                setattr(state, name, None)
        return state

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    # ------------------------------------------------------------------ updates

    def observe(self, equity: float, now: float) -> None:
        if not math.isfinite(equity) or equity <= 0:
            return
        if self.c0 is None or self.hwm is None:
            self.c0, self.hwm, self.epoch_started = float(equity), float(equity), float(now)
        self.hwm = max(self.hwm, float(equity))
        self.last_equity = float(equity)

    def apply_cash_flow(self, amount: float) -> None:
        """Deposits and withdrawals shift the starting capital and the high-water mark, so they
        never look like profit, loss, a new high or a drawdown."""
        if not amount or not math.isfinite(amount):
            return
        self.cash_flow_total += float(amount)
        if self.c0 is not None:
            self.c0 = max(self.c0 + amount, 0.0)
        if self.hwm is not None:
            self.hwm = max(self.hwm + amount, 0.0)

    def rebase(self, equity: float, now: float) -> None:
        """New risk epoch (``--rebase-floor``): an explicit human decision after a floor stop."""
        self.c0 = self.hwm = float(equity)
        self.epoch_started = float(now)
        self.soft_stop_day = None

    # ----------------------------------------------------------------- queries

    def floor(self, lim: EffectiveLimits) -> float:
        if self.c0 is None or self.hwm is None:
            return 0.0
        give_back = lim.floor_drawdown_pct / 100.0 * self.c0 + (1.0 - lim.lock_fraction) * max(0.0, self.hwm - self.c0)
        return max(0.0, self.hwm - give_back)

    def cushion(self, equity: float, lim: EffectiveLimits) -> float:
        return max(0.0, float(equity) - self.floor(lim))

    def cushion_factor(self, equity: float, lim: EffectiveLimits) -> float:
        if self.hwm is None or self.hwm <= 0:
            return 1.0
        reference = lim.floor_drawdown_pct / 100.0 * self.hwm
        if reference <= 0:
            return 0.0
        return min(max(self.cushion(equity, lim) / reference, 0.0), lim.house_money_max)

    def locked_profit(self, lim: EffectiveLimits) -> float:
        if self.c0 is None:
            return 0.0
        return max(0.0, self.floor(lim) - self.c0 * (1 - lim.floor_drawdown_pct / 100.0))

    def snapshot(self, equity: float, lim: EffectiveLimits) -> dict[str, Any]:
        return {"starting_capital": _r(self.c0), "high_water_mark": _r(self.hwm), "floor": _r(self.floor(lim)),
                "cushion": _r(self.cushion(equity, lim)), "cushion_factor": round(self.cushion_factor(equity, lim), 4),
                "locked_profit": _r(self.locked_profit(lim)), "cash_flow_total": _r(self.cash_flow_total),
                "soft_stop_day": self.soft_stop_day, "lock_fraction": lim.lock_fraction,
                "floor_drawdown_pct": lim.floor_drawdown_pct, "house_money_max": lim.house_money_max}


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


# ----------------------------------------------------------------------- sizing


@dataclass(frozen=True)
class SizingInputs:
    tier: str                       # T1 | T2 | T3 | EXT (an external, test-only signal source)
    equity: float
    exploration: bool = False
    posterior_mean: Optional[float] = None
    kelly_fraction_full: Optional[float] = None   # Posterior.kelly_fraction
    open_risk_money: float = 0.0
    budget_room_money: Optional[float] = None     # exploration budget left (T1 only)
    last_loss_risk_pct: Optional[float] = None    # anti-martingale: risk of this arm's last losing trade
    correlation_factor: float = 1.0               # 0..1 from the Correlation and Exposure Analyst
    liquidity_factor: float = 1.0                 # 0..1 from realised slippage / liquidity


@dataclass(frozen=True)
class SizingDecision:
    risk_pct: float
    binding: str
    constraints: dict[str, float] = field(default_factory=dict)
    blocked: Optional[str] = None   # reason code when no trade is possible

    @property
    def ok(self) -> bool:
        return self.blocked is None and self.risk_pct > 0


def large_account_factor(equity: float) -> float:
    """Lower aggressiveness as size grows: 1 below the reference, -15% per decade above it."""
    if equity <= LARGE_ACCOUNT_REF:
        return 1.0
    return max(0.5, 1.0 - LARGE_ACCOUNT_DAMPING * math.log10(equity / LARGE_ACCOUNT_REF))


def size_risk(inp: SizingInputs, lim: EffectiveLimits, capital: CapitalState) -> SizingDecision:
    """Risk percent for one entry: the MINIMUM of every constraint, all under the hard ceiling.

    The result never depends on whether the previous trade won or lost in a way that could
    increase size after a loss: equity falls, the cushion falls, and the anti-martingale
    constraint caps the arm at the risk of its last losing trade."""
    equity = float(inp.equity)
    if not math.isfinite(equity) or equity <= 0:
        return SizingDecision(0.0, "equity", {}, "NO_EQUITY")
    c: dict[str, float] = {"hard_ceiling": limits.MAX_RISK_PER_TRADE_PCT, "profile_cap": lim.t3_risk_pct}
    factor = capital.cushion_factor(equity, lim)
    if inp.tier == "T1":
        c["tier"] = lim.t1_risk_pct
        c["exploration_trade_cap"] = lim.explore_trade_pct
        if inp.budget_room_money is not None:
            c["exploration_budget"] = max(inp.budget_room_money, 0.0) / equity * 100.0
    elif inp.tier in ("T2", "T3"):
        base = lim.t2_risk_pct if inp.tier == "T2" else lim.t3_risk_pct
        c["tier"] = base * factor * large_account_factor(equity)
        if inp.kelly_fraction_full is not None:
            c["kelly"] = max(inp.kelly_fraction_full, 0.0) * lim.kelly_fraction * 100.0
    elif inp.tier == "EXT":
        c["tier"] = lim.t3_risk_pct * min(factor, 1.0)
    else:
        return SizingDecision(0.0, "tier", c, "TIER_T0_SHADOW")
    cushion_pct = capital.cushion(equity, lim) / equity * 100.0
    c["floor_room"] = cushion_pct
    c["portfolio_heat"] = max(0.0, lim.max_open_risk_pct - inp.open_risk_money / equity * 100.0)
    if inp.last_loss_risk_pct is not None:
        c["anti_martingale"] = max(inp.last_loss_risk_pct, 0.0)
    if inp.correlation_factor < 1.0:
        c["correlation"] = c["tier"] * max(inp.correlation_factor, 0.0)
    if inp.liquidity_factor < 1.0:
        c["liquidity"] = c["tier"] * max(inp.liquidity_factor, 0.0)
    binding = min(c, key=lambda k: c[k])
    risk = min(c.values())
    blocked = None
    if cushion_pct <= 1e-9:
        blocked = "FLOOR_SOFT_STOP"
    elif c["portfolio_heat"] <= 1e-9:
        blocked = "RISK_OPEN_RISK"
    elif inp.tier == "T1" and c.get("exploration_budget", 1.0) <= 1e-9:
        blocked = "EXPLORATION_BUDGET_SPENT"
    elif risk <= 0:
        blocked = {"kelly": "KELLY_ZERO", "anti_martingale": "ANTI_MARTINGALE"}.get(binding, "SIZE_ZERO")
    return SizingDecision(round(max(risk, 0.0), 6), binding, {k: round(v, 6) for k, v in c.items()}, blocked)


# --------------------------------------------------------------- affordability


def floor_volume(volume: float, step: float) -> float:
    if step <= 0 or volume <= 0:
        return 0.0
    d = (Decimal(str(volume)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(step))
    return float(d)


@dataclass(frozen=True)
class Affordability:
    symbol: str
    timeframe: str
    stop_distance: float          # price units
    loss_per_lot: float           # account money at the stop for 1.0 lot, costs included
    min_lot_risk_money: float
    min_lot_risk_pct: float
    tier_risk_pct: float
    feasible: bool
    exception: bool               # only feasible through the min-lot exception
    min_equity: float             # equity needed for the minimum lot at the tier risk
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for k, v in out.items():
            if isinstance(v, float):
                out[k] = round(v, 6) if math.isfinite(v) else None
        return out


def affordability(symbol: str, timeframe: str, stop_distance: float, value_per_price: float, volume_min: float,
                  commission_per_lot: float, equity: float, tier_risk_pct: float,
                  exception_cap_pct: float) -> Affordability:
    """Can the broker's minimum lot be traded at this stop distance and tier risk?"""
    loss_per_lot = abs(stop_distance) * value_per_price + max(commission_per_lot, 0.0)
    min_money = loss_per_lot * volume_min
    pct = min_money / equity * 100.0 if equity > 0 else math.inf
    risk_money = equity * tier_risk_pct / 100.0
    min_equity = min_money / (tier_risk_pct / 100.0) if tier_risk_pct > 0 else math.inf
    if loss_per_lot <= 0 or not math.isfinite(loss_per_lot):
        return Affordability(symbol, timeframe, stop_distance, loss_per_lot, min_money, pct, tier_risk_pct, False,
                             False, math.inf, "LOSS_PER_LOT_UNKNOWN")
    if min_money <= risk_money + 1e-12:
        return Affordability(symbol, timeframe, stop_distance, loss_per_lot, min_money, pct, tier_risk_pct, True,
                             False, min_equity)
    if pct <= exception_cap_pct + 1e-12:
        return Affordability(symbol, timeframe, stop_distance, loss_per_lot, min_money, pct, tier_risk_pct, True,
                             True, min_equity, "MIN_LOT_EXCEPTION")
    return Affordability(symbol, timeframe, stop_distance, loss_per_lot, min_money, pct, tier_risk_pct, False, False,
                         min_equity, "MIN_LOT_EXCEEDS_RISK")


def split_volume(volume: float, volume_max: float, volume_step: float, volume_min: float,
                 max_parts: int = limits.MAX_SPLIT_ORDERS) -> list[float]:
    """Split ``volume`` into at most ``max_parts`` orders of at most ``volume_max`` each.

    The total never exceeds ``volume`` (anything that cannot be placed is dropped, never
    rounded up). Returns [] when not even one valid order fits."""
    if volume <= 0 or volume_max <= 0:
        return []
    cap = floor_volume(volume_max, volume_step)
    parts: list[float] = []
    remaining = Decimal(str(volume))
    while remaining > 0 and len(parts) < max(1, max_parts):
        part = floor_volume(min(float(remaining), cap), volume_step)
        if part < volume_min - 1e-12:
            break
        parts.append(part)
        remaining -= Decimal(str(part))
    return parts


def anti_martingale_cap(arm_trades: Sequence[Any]) -> Optional[float]:
    """Risk percent of the arm's most recent CLOSED trade if it lost, else None.

    After a loss the next trade of the same arm may risk at most what the losing trade risked;
    the cap lifts after a winning trade. Rows need ``status``, ``pnl`` and ``extra['risk_pct']``."""
    closed = [r for r in arm_trades if getattr(r, "status", None) == "closed" and getattr(r, "pnl", None) is not None]
    if not closed:
        return None
    last = max(closed, key=lambda r: float(r.close_ts or 0.0))
    if float(last.pnl) >= 0:
        return None
    risk_pct = (getattr(last, "extra", None) or {}).get("risk_pct")
    return float(risk_pct) if risk_pct is not None else None
