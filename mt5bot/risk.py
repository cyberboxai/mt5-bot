"""Hard safety layer between ANY strategy and the broker.

Strategies only propose a direction and a volatility estimate. This module decides
whether a trade may open and how big it is, and it is shared by live trading,
shadow trading and the backtester so they can never diverge.

Forbidden behaviours, each enforced here and covered by tests:

* martingale or any size increase after a loss: the risk fraction comes from the sizer
  (``capital.size_risk``: tier, evidence, cushion, heat, anti-martingale cap), never from
  the last trade's result, and a plan whose risk exceeds that fraction, the profile cap or
  the hard ceiling is rejected here again;
* averaging down, pyramiding and grids: at most one bot position per symbol;
* orders without SL and TP, or with SL/TP on the wrong side of the price;
* removing or widening an SL after entry (only adding a missing SL or tightening);
* trading when the spread or recent slippage exceeds its limit;
* trading inside a news blackout window when the calendar filter is enabled;
* exceeding the hard ceilings in ``limits.py`` whatever the config says.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Sequence

from . import limits
from .persistence import atomic_write_json, atomic_write_text
from .profiles import effective_limits

log = logging.getLogger("mt5_bot")

Side = Literal["BUY", "SELL"]

# MQL5 SYMBOL_SWAP_MODE_* values that are modelled. Others are logged and treated as 0.
SWAP_MODE_DISABLED = 0
SWAP_MODE_POINTS = 1
SWAP_MODE_CURRENCY_DEPOSIT = 4

RESUME_MARKER = "RESUME_REQUIRED.txt"


class RiskViolation(ValueError):
    """A request broke a hard safety rule. It must never reach the broker."""


# --------------------------------------------------------------------------- sizing


def lots_for_risk(
    risk_money: float,
    loss_per_lot: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> float:
    """Volume whose loss at SL is <= ``risk_money``. Returns 0.0 if no valid volume.

    Always rounds DOWN to ``volume_step`` and caps at ``volume_max``. If the result is
    below ``volume_min`` it returns 0.0 (never rounds up into higher risk). Decimal
    arithmetic avoids float artifacts such as 0.3 / 0.1 == 2.9999999999999996.
    """
    try:
        risk = Decimal(str(risk_money))
        loss = Decimal(str(loss_per_lot))
        vmin = Decimal(str(volume_min))
        vmax = Decimal(str(volume_max))
        step = Decimal(str(volume_step))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    numbers = (risk, loss, vmin, vmax, step)
    if not all(n.is_finite() and n > 0 for n in numbers) or vmax < vmin:
        return 0.0
    lots = (risk / loss / step).to_integral_value(rounding=ROUND_FLOOR) * step
    cap = (vmax / step).to_integral_value(rounding=ROUND_FLOOR) * step
    lots = min(lots, cap)
    if lots < vmin:
        return 0.0
    return float(lots)


@dataclass(frozen=True)
class SymbolSpec:
    """Everything sizing and cost modelling needs about a symbol (auto-detected from MT5)."""

    name: str
    point: float
    digits: int
    tick_value: float
    tick_size: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0
    freeze_level: int = 0
    filling_mode: int = 0
    trade_mode: int = 4
    swap_long: float = 0.0
    swap_short: float = 0.0
    swap_mode: int = SWAP_MODE_POINTS
    swap_3day: int = 3
    currency_base: str = ""
    currency_profit: str = ""

    def __post_init__(self) -> None:
        for name in ("point", "tick_value", "tick_size", "contract_size", "volume_min", "volume_max",
                     "volume_step"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f"{self.name}: symbol {name} must be positive, got {value!r}")

    @property
    def value_per_price(self) -> float:
        """Account-currency value of a 1.0 price move for 1 lot."""
        return self.tick_value / self.tick_size

    def money(self, price_distance: float, volume: float) -> float:
        return price_distance * self.value_per_price * volume

    def swap_cost_per_lot_night(self, side: Side) -> float:
        """Swap cost (>= 0, in account currency) per lot per night. Positive carry is ignored."""
        swap = self.swap_long if side == "BUY" else self.swap_short
        if self.swap_mode == SWAP_MODE_POINTS:
            money = swap * self.point * self.value_per_price
        elif self.swap_mode == SWAP_MODE_CURRENCY_DEPOSIT:
            money = swap
        else:
            return 0.0
        return max(0.0, -money)

    @classmethod
    def from_info(cls, info: Any) -> "SymbolSpec":
        tick_value = getattr(info, "trade_tick_value_loss", 0) or getattr(info, "trade_tick_value", 0)
        tick_size = getattr(info, "trade_tick_size", 0) or info.point
        return cls(
            name=info.name,
            point=float(info.point),
            digits=int(info.digits),
            tick_value=float(tick_value),
            tick_size=float(tick_size),
            contract_size=float(getattr(info, "trade_contract_size", 0) or getattr(info, "contract_size", 0)),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            stops_level=int(getattr(info, "trade_stops_level", 0) or 0),
            freeze_level=int(getattr(info, "trade_freeze_level", 0) or 0),
            filling_mode=int(getattr(info, "filling_mode", 0) or 0),
            trade_mode=int(getattr(info, "trade_mode", 4)),
            swap_long=float(getattr(info, "swap_long", 0.0) or 0.0),
            swap_short=float(getattr(info, "swap_short", 0.0) or 0.0),
            swap_mode=int(getattr(info, "swap_mode", SWAP_MODE_POINTS) or 0),
            swap_3day=int(getattr(info, "swap_rollover3days", 3) or 3),
            currency_base=str(getattr(info, "currency_base", "") or ""),
            currency_profit=str(getattr(info, "currency_profit", "") or ""),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "SymbolSpec":
        return cls(**json.loads(text))


@dataclass(frozen=True)
class TradePlan:
    side: Side
    price: float
    sl: float
    tp: float
    sl_points: int
    tp_points: int
    volume: float
    loss_per_lot: float
    risk_money: float
    risk_pct: float
    exception: bool = False   # minimum lot accepted above the tier risk (conscious, logged exception)

    @property
    def planned_risk(self) -> float:
        return self.volume * self.loss_per_lot


@dataclass(frozen=True)
class PlanResult:
    plan: Optional[TradePlan]
    reason: str = ""


LossPerLotFn = Callable[[Side, float, float, float], Optional[float]]
"""(side, worst_entry_price, sl_price, volume=1.0) -> price loss in account money, or None."""


def plan_trade(
    side: Side,
    atr_value: float,
    sl_mult: float,
    tp_mult: float,
    price: float,
    spread_points: float,
    spec: SymbolSpec,
    equity: float,
    risk_pct: float,
    commission_per_lot: float,
    adverse_points: float,
    loss_per_lot_fn: Optional[LossPerLotFn] = None,
    min_lot_cap_pct: Optional[float] = None,
    max_volume: Optional[float] = None,
) -> PlanResult:
    """Turn a direction and volatility into SL, TP and a risk-capped volume.

    ``price`` is the quote the order would use (ask for BUY, bid for SELL). The loss
    per lot is measured from the worst fill ``adverse_points`` away from it, plus the
    round-turn commission, so slippage within the limit and costs cannot exceed the
    configured risk. Used verbatim by live, shadow and backtest code.

    ``min_lot_cap_pct``: when the minimum lot risks more than ``risk_pct`` but no more than
    this cap (never above the hard ceiling), plan exactly the minimum lot and flag the plan as
    an exception. Without it, such a trade is refused (the default for backtests and shadow).
    """
    if side not in ("BUY", "SELL"):
        return PlanResult(None, f"invalid side {side!r}")
    if not (limits.MIN_SL_ATR_MULT <= sl_mult <= limits.MAX_SL_ATR_MULT
            and limits.MIN_TP_ATR_MULT <= tp_mult <= limits.MAX_TP_ATR_MULT):
        return PlanResult(None, f"SL/TP multiples {sl_mult}/{tp_mult} outside hard bounds")
    if not (math.isfinite(atr_value) and atr_value > 0 and math.isfinite(price) and price > 0):
        return PlanResult(None, "invalid ATR or price")
    if not (math.isfinite(equity) and equity > 0):
        return PlanResult(None, "equity must be positive")
    risk_pct = min(float(risk_pct), limits.MAX_RISK_PER_TRADE_PCT)
    if risk_pct <= 0:
        return PlanResult(None, "risk percentage must be positive")
    point = spec.point
    # Whole points keep SL/TP on the price grid, so rounding never widens the stop.
    sl_points = int(math.floor(atr_value * sl_mult / point + 1e-9))
    tp_points = int(math.floor(atr_value * tp_mult / point + 1e-9))
    min_points = spec.stops_level + max(spread_points, 0.0)
    if sl_points < min_points or tp_points < min_points or sl_points <= 0 or tp_points <= 0:
        return PlanResult(None, f"SL {sl_points} / TP {tp_points} points below broker minimum {min_points:.1f}")
    sign = 1 if side == "BUY" else -1
    sl = round(price - sign * sl_points * point, spec.digits)
    tp = round(price + sign * tp_points * point, spec.digits)
    worst_entry = price + sign * adverse_points * point
    if loss_per_lot_fn is not None:
        price_loss = loss_per_lot_fn(side, worst_entry, sl, 1.0)
    else:
        price_loss = spec.money(abs(worst_entry - sl), 1.0)
    if price_loss is None or not math.isfinite(price_loss) or price_loss <= 0:
        return PlanResult(None, f"cannot compute loss per lot ({price_loss!r})")
    loss_per_lot = price_loss + max(commission_per_lot, 0.0)
    risk_money = equity * risk_pct / 100.0
    # ``max_volume`` above the broker's volume_max lets a large account split one entry into a few
    # orders (see capital.split_volume); the total risk is still capped by ``risk_money``.
    volume = lots_for_risk(risk_money, loss_per_lot, spec.volume_min, max_volume or spec.volume_max, spec.volume_step)
    if volume <= 0:
        min_money = spec.volume_min * loss_per_lot
        min_pct = min_money / equity * 100.0
        cap = min(min_lot_cap_pct, limits.MAX_RISK_PER_TRADE_PCT) if min_lot_cap_pct is not None else None
        if cap is not None and min_pct <= cap + 1e-12:
            return PlanResult(TradePlan(side, price, sl, tp, sl_points, tp_points, spec.volume_min, loss_per_lot,
                                        min_money, min_pct, exception=True))
        return PlanResult(None, (f"minimum volume {spec.volume_min} would risk "
                                 f"{min_money:.2f} ({min_pct:.3f}%) > allowed {risk_money:.2f}"))
    return PlanResult(TradePlan(side, price, sl, tp, sl_points, tp_points, volume, loss_per_lot,
                                risk_money, risk_pct))


# ------------------------------------------------------------------ entry approval


@dataclass(frozen=True)
class OpenRisk:
    """Risk still at stake on an open bot position (money lost if its SL is hit).

    ``group`` identifies one entry: an order larger than the broker's volume_max may be split
    into a few positions with the same group; they count as ONE entry for the per-symbol and
    position-count limits (their risks still add up)."""

    symbol: str
    risk_money: float
    group: Optional[str] = None


def entry_groups(positions: Sequence[OpenRisk]) -> set[tuple[str, str]]:
    return {(p.symbol, p.group or f"#{i}") for i, p in enumerate(positions)}


@dataclass(frozen=True)
class EntryContext:
    symbol: str
    plan: TradePlan
    equity: float
    spread_points: float
    bot_positions: Sequence[OpenRisk]
    symbol_has_foreign_netting_position: bool = False
    notional_money: Optional[float] = None
    halted_reason: Optional[str] = None
    news_blocked: Optional[str] = None
    slippage_paused: bool = False
    allowed_risk_pct: Optional[float] = None       # from the sizer (already x the team multiplier)
    spread_limit_points: Optional[float] = None    # per symbol: absolute cap, cost-to-stop and spike filters
    spread_limit_reason: str = ""
    exception_cap_pct: Optional[float] = None      # tier-specific cap for a minimum-lot exception (None: refuse)


class RiskManager:
    """Applies the effective limits (profile, tightened by user caps), clamped to ``limits``."""

    def __init__(self, cfg: Any, is_real: bool) -> None:
        self.is_real = is_real
        self.update_config(cfg)

    def update_config(self, cfg: Any) -> None:
        self.limits = effective_limits(cfg, self.is_real)
        # The largest per-trade risk any tier may use (the T3 cap of the profile).
        self.risk_pct = min(self.limits.t3_risk_pct, limits.MAX_RISK_PER_TRADE_PCT)
        self.max_daily_loss_pct = min(self.limits.daily_loss_pct, limits.MAX_DAILY_LOSS_PCT)
        self.max_positions = min(self.limits.max_positions, limits.MAX_OPEN_POSITIONS)
        self.max_total_open_risk_pct = min(self.limits.max_open_risk_pct, limits.MAX_TOTAL_OPEN_RISK_PCT)
        self.max_symbol_exposure_x = min(cfg.max_symbol_exposure_x, limits.MAX_SYMBOL_EXPOSURE_X)
        self.max_drawdown_pct = min(self.limits.drawdown_breaker_pct, limits.MAX_DRAWDOWN_FROM_PEAK_PCT)

    def allowed_risk_pct(self, ctx: EntryContext) -> float:
        """The per-trade risk this entry may use: the sizer's figure (or, for a minimum-lot
        exception, the affordability cap), never above the profile cap or the hard ceiling."""
        cap = min(self.risk_pct, limits.MAX_RISK_PER_TRADE_PCT)
        if ctx.plan.exception:
            return min(cap, ctx.exception_cap_pct) if ctx.exception_cap_pct is not None else 0.0
        if ctx.allowed_risk_pct is not None:
            cap = min(cap, ctx.allowed_risk_pct)
        return cap

    def approve_entry(self, ctx: EntryContext) -> tuple[bool, str]:
        """Every check an entry must pass. Returns (allowed, reason)."""
        plan = ctx.plan
        if ctx.halted_reason:
            return False, f"trading halted: {ctx.halted_reason}"
        try:
            validate_plan_stops(plan)
        except RiskViolation as exc:
            return False, str(exc)
        if ctx.spread_limit_points is not None and ctx.spread_points > ctx.spread_limit_points:
            why = f" ({ctx.spread_limit_reason})" if ctx.spread_limit_reason else ""
            return False, f"spread {ctx.spread_points:.1f} points > max {ctx.spread_limit_points:.1f}{why}"
        if ctx.slippage_paused:
            return False, "recent slippage exceeded max_slippage_points; entries paused on this symbol"
        if ctx.news_blocked:
            return False, f"news blackout: {ctx.news_blocked}"
        groups = entry_groups(ctx.bot_positions)
        same_symbol = [g for g in groups if g[0] == ctx.symbol]
        if len(same_symbol) >= limits.MAX_POSITIONS_PER_SYMBOL:
            return False, "bot already has a position on this symbol (no averaging down, pyramiding or grids)"
        if ctx.symbol_has_foreign_netting_position:
            return False, "netting account already holds a non-bot position on this symbol"
        if len(groups) >= self.max_positions:
            return False, f"{len(groups)} bot positions open (max {self.max_positions})"
        allowed_pct = self.allowed_risk_pct(ctx)
        allowed_risk = ctx.equity * allowed_pct / 100.0
        # Martingale guard: size can only come from the sizer's fraction of current equity,
        # itself capped by the profile and the hard ceiling.
        if plan.planned_risk > allowed_risk * (1 + 1e-9) or plan.risk_pct > allowed_pct + 1e-9:
            return False, (f"planned risk {plan.planned_risk:.2f} exceeds the allowed per-trade risk "
                           f"{allowed_risk:.2f} ({allowed_pct:.3f}%; size increases are forbidden)")
        open_risk = sum(max(p.risk_money, 0.0) for p in ctx.bot_positions)
        max_total = ctx.equity * self.max_total_open_risk_pct / 100.0
        if open_risk + plan.planned_risk > max_total * (1 + 1e-9):
            return False, (f"total open risk {open_risk + plan.planned_risk:.2f} would exceed "
                           f"{self.max_total_open_risk_pct:.2f}% of equity ({max_total:.2f})")
        if ctx.notional_money is not None:
            exposure = ctx.notional_money / ctx.equity
            if exposure > self.max_symbol_exposure_x:
                return False, f"exposure {exposure:.2f}x equity > max {self.max_symbol_exposure_x:.2f}x"
        return True, "ok"


def validate_plan_stops(plan: TradePlan) -> None:
    """Every order has a server-side SL and TP on the correct side of the price."""
    if not plan.sl or not plan.tp or plan.sl <= 0 or plan.tp <= 0:
        raise RiskViolation("order without SL and TP is forbidden")
    if plan.side == "BUY" and not (plan.sl < plan.price < plan.tp):
        raise RiskViolation(f"BUY needs SL < price < TP, got SL {plan.sl} price {plan.price} TP {plan.tp}")
    if plan.side == "SELL" and not (plan.tp < plan.price < plan.sl):
        raise RiskViolation(f"SELL needs TP < price < SL, got SL {plan.sl} price {plan.price} TP {plan.tp}")
    if plan.volume <= 0:
        raise RiskViolation("volume must be positive")


def validate_open_request(request: dict[str, Any], plan: TradePlan) -> None:
    """Final gate right before order_send for an opening deal. Raises RiskViolation."""
    sl, tp, price = request.get("sl") or 0.0, request.get("tp") or 0.0, request.get("price") or 0.0
    if sl <= 0 or tp <= 0:
        raise RiskViolation("order without SL and TP is forbidden")
    if plan.side == "BUY" and not (sl < price < tp):
        raise RiskViolation(f"BUY request needs SL < price < TP, got {sl} / {price} / {tp}")
    if plan.side == "SELL" and not (tp < price < sl):
        raise RiskViolation(f"SELL request needs TP < price < SL, got {tp} / {price} / {sl}")
    if request.get("volume", 0) > plan.volume + 1e-12:
        raise RiskViolation(f"request volume {request.get('volume')} exceeds the risk-sized {plan.volume}")
    if abs(abs(price - sl) - abs(plan.price - plan.sl)) > abs(plan.price - plan.sl) * 1e-6 + 1e-12:
        raise RiskViolation("SL distance differs from the risk plan")


def validate_sl_change(side: Side, current_sl: float, new_sl: float) -> None:
    """Allowed: adding a missing SL, or moving it closer to the price. Never removal or widening."""
    if not new_sl or new_sl <= 0:
        raise RiskViolation("removing a stop loss is forbidden")
    if not current_sl or current_sl <= 0:
        return  # repairing an unprotected position
    if side == "BUY" and new_sl < current_sl:
        raise RiskViolation(f"widening a BUY stop from {current_sl} to {new_sl} is forbidden")
    if side == "SELL" and new_sl > current_sl:
        raise RiskViolation(f"widening a SELL stop from {current_sl} to {new_sl} is forbidden")


# ------------------------------------------------------------ equity guard (state)


@dataclass
class GuardState:
    day: Optional[str] = None
    start_equity: Optional[float] = None
    daily_halted: bool = False
    peak_equity: Optional[float] = None
    dd_halted: bool = False
    dd_tripped_at: Optional[str] = None


class EquityGuard:
    """Daily loss limit and the equity drawdown circuit breaker, persisted atomically.

    The day key is the broker SERVER day. The drawdown breaker is the one place a human
    is deliberately required: once tripped it stays halted until the file
    ``RESUME_REQUIRED.txt`` in the account state folder is deleted (``--resume`` does it).
    """

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir)
        self.path = self.dir / "risk_state.json"
        self.marker = self.dir / RESUME_MARKER
        self.state = GuardState()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("not an object")
            state = GuardState(**data)
            if state.day is not None and not isinstance(state.day, str):
                raise TypeError("day must be text")
            for name in ("start_equity", "peak_equity"):
                value = getattr(state, name)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value)):
                    raise TypeError(f"{name} must be a number")
            if not isinstance(state.daily_halted, bool) or not isinstance(state.dd_halted, bool):
                raise TypeError("halt flags must be booleans")
        except (OSError, ValueError, TypeError) as exc:
            # Refusing to start is safer than silently resetting the loss limits.
            raise ValueError(
                f"State file {self.path} is unreadable or corrupt ({exc}). Inspect it; delete it only if you "
                "accept that the daily loss limit and drawdown peak restart from current equity."
            ) from exc
        self.state = state

    def save(self) -> None:
        atomic_write_json(self.path, asdict(self.state))

    @property
    def halted_reason(self) -> Optional[str]:
        if self.state.dd_halted:
            return "drawdown circuit breaker (delete RESUME_REQUIRED.txt or run --resume)"
        if self.state.daily_halted:
            return "daily loss limit (resets on the next server day)"
        return None

    def drawdown_pct(self, equity: float) -> float:
        peak = self.state.peak_equity
        if not peak or peak <= 0:
            return 0.0
        return max(0.0, (peak - equity) / peak * 100.0)

    def daily_loss_pct(self, equity: float) -> float:
        start = self.state.start_equity
        if not start or start <= 0:
            return 0.0
        return (start - equity) / start * 100.0

    def update(self, day: str, equity: float, max_daily_loss_pct: float, max_drawdown_pct: float) -> list[str]:
        """Advance the guard. Returns events: new_day, resumed, drawdown_trip, daily_limit."""
        max_daily_loss_pct = min(max_daily_loss_pct, limits.MAX_DAILY_LOSS_PCT)
        max_drawdown_pct = min(max_drawdown_pct, limits.MAX_DRAWDOWN_FROM_PEAK_PCT)
        events: list[str] = []
        s = self.state
        changed = False
        if s.day != day:
            s.day, s.start_equity, s.daily_halted = day, float(equity), False
            events.append("new_day")
            changed = True
        if s.dd_halted and not self.marker.exists():
            s.dd_halted, s.dd_tripped_at, s.peak_equity = False, None, float(equity)
            events.append("resumed")
            changed = True
        if not s.dd_halted:
            if s.peak_equity is None or equity > s.peak_equity:
                s.peak_equity = float(equity)
                changed = True
            if self.drawdown_pct(equity) >= max_drawdown_pct:
                s.dd_halted = True
                s.dd_tripped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._write_marker(equity, max_drawdown_pct)
                events.append("drawdown_trip")
                changed = True
        if not s.daily_halted and s.start_equity is not None and self.daily_loss_pct(equity) >= max_daily_loss_pct:
            s.daily_halted = True
            events.append("daily_limit")
            changed = True
        if changed:
            self.save()
        return events

    def adjust_for_cash_flow(self, amount: float) -> None:
        """Deposits and withdrawals must not look like profit or drawdown."""
        if not amount:
            return
        s = self.state
        if s.peak_equity is not None:
            s.peak_equity = max(s.peak_equity + amount, 0.0)
        if s.start_equity is not None:
            s.start_equity = max(s.start_equity + amount, 0.0)
        self.save()

    def _write_marker(self, equity: float, limit_pct: float) -> None:
        text = (
            "DRAWDOWN CIRCUIT BREAKER TRIPPED\n"
            f"time (UTC): {self.state.dd_tripped_at}\n"
            f"peak equity: {self.state.peak_equity:.2f}\n"
            f"equity at trip: {equity:.2f}\n"
            f"limit: {limit_pct:.2f}% from peak\n\n"
            "All bot positions were closed and trading is halted.\n"
            "Review what happened. To resume, delete this file or run: python mt5_bot.py --resume\n"
            "The drawdown peak then restarts from the equity at that moment.\n"
        )
        atomic_write_text(self.marker, text)


def request_resume(state_dir: Path) -> bool:
    """Used by ``--resume``. Returns True if a marker existed and was removed."""
    marker = Path(state_dir) / RESUME_MARKER
    if not marker.exists():
        return False
    marker.unlink()
    return True


# ---------------------------------------------------------------- news calendar


class NewsCalendar:
    """Optional blackout windows around events from a user-supplied CSV.

    Columns: ``time_utc`` (ISO 8601, e.g. 2026-10-30T12:30:00Z), ``currency`` (e.g. USD),
    optional ``impact`` and ``title``. The MT5 Python API has no economic calendar,
    so the file must be maintained by the user (or a separate tool).
    """

    def __init__(self, events: Iterable[tuple[float, str, str]] = ()) -> None:
        self.events = sorted(events)

    @classmethod
    def load(cls, path: Path) -> "NewsCalendar":
        events = []
        with open(path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                stamp = row.get("time_utc", "").strip().replace("Z", "+00:00")
                when = datetime.fromisoformat(stamp)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                events.append((when.timestamp(), row.get("currency", "").strip().upper(),
                               (row.get("title") or "").strip()))
        return cls(events)

    def blocked(self, currencies: Iterable[str], now_utc: Optional[float] = None,
                minutes_before: int = 30, minutes_after: int = 30) -> Optional[str]:
        now = time.time() if now_utc is None else now_utc
        wanted = {c.upper() for c in currencies if c}
        for stamp, currency, title in self.events:
            if currency in wanted and stamp - minutes_before * 60 <= now <= stamp + minutes_after * 60:
                when = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                return f"{currency} {title or 'event'} at {when}"
        return None
