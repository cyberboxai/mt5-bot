"""What can be traded at this balance, computed live from broker specifications.

Nothing here contains a money amount. For one candidate (symbol, timeframe, stop policy) the
feasibility floor is the smallest equity at which the broker's minimum lot satisfies:

* the minimum-viable risk cap of the profile;
* the margin guard (projected margin level above its multiple of the stop-out level);
* the notional cap for a minimum-lot trade.

The cost-to-edge condition does not depend on equity, so a candidate is feasible exactly when
``equity >= floor_equity`` and the cost is acceptable. That makes feasibility monotonic by
construction: more equity can never shrink the feasible set.

Two continuous ratios, recomputed on every decision, replace any money band:

* headroom = risk target / minimum-lot risk: below 1 the account trades the minimum lot;
* capacity ratio = intended volume / capacity lots: above 1 the order is sliced and the excess
  capital stays undeployed.

Volume arithmetic uses integer multiples of ``volume_step`` through ``Decimal``: floats are
only converted at the edge, so a volume is always an exact multiple of the step.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import limits

STOP_ATR_MULTS: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5)
STRUCTURE_STOP = "structure"
STOP_POLICIES: tuple[str, ...] = tuple(f"atr_{m:g}" for m in STOP_ATR_MULTS) + (STRUCTURE_STOP,)

MODE_BELOW_FLOOR = "below_floor"
MODE_MIN_VIABLE = "min_viable"
MODE_PERCENT = "percent"
MODE_CAPACITY = "capacity"
MODE_PT = {MODE_BELOW_FLOOR: "abaixo do piso", MODE_MIN_VIABLE: "tamanho mínimo viável",
           MODE_PERCENT: "dimensionamento por percentagem", MODE_CAPACITY: "capacidade"}

REASON_PT = {
    "NO_EQUITY": "capital zero, negativo ou desconhecido",
    "SIZING_OFF": "dimensionamento mínimo viável desligado",
    "MIN_LOT_ABOVE_CAP": "o lote mínimo arrisca mais do que o limite do perfil",
    "MARGIN_GUARD": "margem insuficiente para o lote mínimo",
    "NOTIONAL_CAP": "exposição do lote mínimo acima do limite",
    "COST_TO_EDGE": "custo demasiado alto para a distância do stop",
    "LOSS_PER_LOT_UNKNOWN": "não foi possível calcular a perda por lote",
}


def _dec(value: Any) -> Optional[Decimal]:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def steps_of(volume: float, step: float) -> int:
    """Whole ``step`` units that fit in ``volume`` (rounded down). 0 for invalid input."""
    v, s = _dec(volume), _dec(step)
    if v is None or s is None or v <= 0 or s <= 0:
        return 0
    return int((v / s).to_integral_value(rounding=ROUND_FLOOR))


def volume_from_steps(steps: int, step: float) -> float:
    """Exact multiple of ``step`` as a float (no binary rounding artefacts such as 0.30000000000000004)."""
    s = _dec(step)
    return float(Decimal(int(steps)) * s) if s is not None and steps > 0 else 0.0


def clamp_volume(volume: float, volume_min: float, volume_max: float, step: float) -> float:
    """Round DOWN to the step and cap at ``volume_max``. 0.0 when below ``volume_min``."""
    steps = min(steps_of(volume, step), steps_of(volume_max, step))
    out = volume_from_steps(steps, step)
    return out if out >= volume_min - 1e-12 and out > 0 else 0.0


def format_volume(volume: float, step: float) -> str:
    """Plain decimal text at the step's precision: never scientific notation."""
    s = _dec(step)
    places = max(-s.as_tuple().exponent, 0) if s is not None and s > 0 else 2
    return f"{volume:.{places}f}"


# ------------------------------------------------------------------ candidates


@dataclass(frozen=True)
class FeasibilityRules:
    min_viable_cap_pct: float
    risk_target_pct: float
    margin_floor_level_pct: float           # projected margin level must stay above this
    max_exposure_x: float = limits.MAX_MIN_LOT_EXPOSURE_X
    min_cost_to_edge: float = 1.2
    max_cost_r: float = 0.25                # cost as a fraction of 1R when no edge estimate exists yet


@dataclass(frozen=True)
class Candidate:
    """Everything feasibility needs about one (symbol, timeframe, stop policy)."""

    symbol: str
    timeframe: str
    stop_policy: str
    stop_distance: float
    loss_per_lot: float                     # money lost at the SL for 1 lot, costs included
    volume_min: float
    volume_step: float
    volume_max: float
    margin_per_lot: Optional[float] = None
    notional_per_lot: Optional[float] = None
    cost_per_lot: Optional[float] = None
    edge_r: Optional[float] = None          # best posterior mean of an arm here, None before research
    liquid: bool = True


@dataclass(frozen=True)
class Row:
    symbol: str
    timeframe: str
    stop_policy: str
    stop_distance: float
    min_lot_risk: float
    min_lot_risk_pct: Optional[float]
    target_risk: float
    headroom: Optional[float]
    floor_equity: float
    risk_floor: float
    margin_floor: float
    notional_floor: float
    cost_r: Optional[float]
    cost_ok: bool
    feasible: bool
    reason: str = ""

    @property
    def min_viable(self) -> bool:
        """Tradable only as a minimum-lot exception (the tier risk is below the minimum lot)."""
        return self.feasible and (self.headroom is None or self.headroom < 1.0)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in out.items():
            if isinstance(value, float):
                out[key] = round(value, 6) if math.isfinite(value) else None
        out["min_viable"] = self.min_viable
        out["reason_pt"] = REASON_PT.get(self.reason, "")
        return out


def evaluate(c: Candidate, equity: float, rules: FeasibilityRules) -> Row:
    """Feasibility of one candidate. Pure and total: any input gives a Row, never an exception."""
    valid_equity = isinstance(equity, (int, float)) and math.isfinite(equity) and equity > 0
    money = c.loss_per_lot * c.volume_min if math.isfinite(c.loss_per_lot) and c.loss_per_lot > 0 else math.inf
    if not math.isfinite(money):
        return Row(c.symbol, c.timeframe, c.stop_policy, c.stop_distance, math.inf, None, 0.0, None, math.inf,
                   math.inf, math.inf, math.inf, None, False, False, "LOSS_PER_LOT_UNKNOWN")
    cap = min(rules.min_viable_cap_pct, limits.MAX_RISK_PER_TRADE_PCT)
    risk_floor = money / (cap / 100.0) if cap > 0 else math.inf
    margin_floor = 0.0
    if c.margin_per_lot is not None and c.margin_per_lot > 0:
        margin_floor = c.margin_per_lot * c.volume_min * rules.margin_floor_level_pct / 100.0
    notional_floor = 0.0
    if c.notional_per_lot is not None and c.notional_per_lot > 0 and rules.max_exposure_x > 0:
        notional_floor = c.notional_per_lot * c.volume_min / rules.max_exposure_x
    # A tier risk above the cap would make the minimum lot affordable without the exception.
    if rules.risk_target_pct > cap:
        risk_floor = min(risk_floor, money / (rules.risk_target_pct / 100.0))
    floor = max(risk_floor, margin_floor, notional_floor)
    cost_r = c.cost_per_lot / c.loss_per_lot if c.cost_per_lot is not None and c.loss_per_lot > 0 else None
    cost_ok = True
    if cost_r is not None:
        if c.edge_r is None:
            cost_ok = cost_r <= rules.max_cost_r
        elif cost_r > 0:
            cost_ok = c.edge_r / cost_r >= rules.min_cost_to_edge
    target = equity * rules.risk_target_pct / 100.0 if valid_equity else 0.0
    pct = money / equity * 100.0 if valid_equity else None
    headroom = target / money if valid_equity and money > 0 else None
    reason = ""
    if not valid_equity:
        reason = "NO_EQUITY"
    elif equity < risk_floor:
        reason = "MIN_LOT_ABOVE_CAP" if cap > 0 else "SIZING_OFF"
    elif equity < margin_floor:
        reason = "MARGIN_GUARD"
    elif equity < notional_floor:
        reason = "NOTIONAL_CAP"
    elif not cost_ok:
        reason = "COST_TO_EDGE"
    return Row(c.symbol, c.timeframe, c.stop_policy, c.stop_distance, money, pct, target, headroom, floor, risk_floor,
               margin_floor, notional_floor, cost_r, cost_ok, reason == "", reason)


def feasibility_floor(rows: Iterable[Row], candidates: Optional[Mapping[tuple[str, str, str], Candidate]] = None,
                      liquid_only: bool = True) -> Optional[float]:
    """Smallest equity at which at least one liquid candidate with acceptable cost passes.

    With nothing liquid right now (a closed market, a weekend) the floor comes from every candidate:
    the minimum lot does not stop existing when the market closes."""
    usable = [r for r in rows if r.cost_ok and math.isfinite(r.floor_equity)]
    if candidates is not None and liquid_only:
        liquid = [r for r in usable if getattr(candidates.get((r.symbol, r.timeframe, r.stop_policy)), "liquid", True)]
        usable = liquid or usable
    return min((r.floor_equity for r in usable), default=None)


def floors_by_symbol(rows: Iterable[Row]) -> dict[str, float]:
    """Cheapest feasibility floor of each symbol, in account currency."""
    out: dict[str, float] = {}
    for r in rows:
        if math.isfinite(r.floor_equity) and r.cost_ok:
            out[r.symbol] = min(out.get(r.symbol, math.inf), r.floor_equity)
    return out


def typical_min_lot_risk(rows: Sequence[Row]) -> Optional[float]:
    """Median minimum-lot risk of the feasible candidates: the 'one trade' of the budget."""
    values = sorted(r.min_lot_risk for r in rows if r.feasible and math.isfinite(r.min_lot_risk))
    return values[len(values) // 2] if values else None


# -------------------------------------------------------------------- capacity


@dataclass(frozen=True)
class Capacity:
    lots: float                             # total the system will place for one entry
    binding: str
    child_lots: float                       # largest single order
    terms: dict[str, float] = field(default_factory=dict)


def liquidity_lots(ticks_per_minute: Optional[float], lots_per_tick: float, child_interval_seconds: float,
                   observed_slippage: Optional[float] = None, allowed_slippage: Optional[float] = None) -> Optional[float]:
    """Heuristic cap per child order from activity and observed slippage. None without data: no cap
    is claimed.

    Higher tick activity carries more size; realised slippage above the allowance shrinks the
    cap in proportion, so it follows what execution actually experiences."""
    if ticks_per_minute is None or not math.isfinite(ticks_per_minute) or ticks_per_minute <= 0:
        return None
    lots = ticks_per_minute * lots_per_tick * max(child_interval_seconds, 60.0) / 60.0
    if observed_slippage is not None and allowed_slippage and observed_slippage > allowed_slippage > 0:
        lots *= allowed_slippage / observed_slippage
    return lots


def capacity_lots(volume_max: float, max_children: int, volume_limit: float = 0.0,
                  liquidity: Optional[float] = None, margin_lots: Optional[float] = None) -> Capacity:
    """min(child size x children, broker position limit, margin cap). ``liquidity`` caps one child."""
    child = volume_max if liquidity is None or not math.isfinite(liquidity) else min(volume_max, max(liquidity, 0.0))
    terms = {"order_limit": child * max(1, max_children)}
    if volume_limit and volume_limit > 0:
        terms["broker_limit"] = float(volume_limit)
    if margin_lots is not None and math.isfinite(margin_lots):
        terms["margin"] = max(float(margin_lots), 0.0)
    binding = min(terms, key=lambda k: terms[k])
    return Capacity(terms[binding], binding, child, terms)


def margin_lots(equity: float, margin_used: float, margin_per_lot: Optional[float], floor_level_pct: float) -> Optional[float]:
    """Lots that keep the projected margin level at or above ``floor_level_pct``."""
    if margin_per_lot is None or margin_per_lot <= 0 or floor_level_pct <= 0:
        return None
    room = max(equity, 0.0) * 100.0 / floor_level_pct - max(margin_used, 0.0)
    return max(room, 0.0) / margin_per_lot


def capacity_ceiling(lots: float, loss_per_lot: float, risk_pct: float) -> Optional[float]:
    """Largest equity whose risk target this symbol can absorb. None when undefined."""
    if lots <= 0 or loss_per_lot <= 0 or risk_pct <= 0 or not math.isfinite(lots * loss_per_lot):
        return None
    return lots * loss_per_lot / (risk_pct / 100.0)


def total_ceiling(per_symbol: Mapping[str, float], max_positions: int) -> Optional[float]:
    """Capital deployable across the best ``max_positions`` symbols."""
    values = sorted((v for v in per_symbol.values() if math.isfinite(v) and v > 0), reverse=True)
    return sum(values[:max(1, max_positions)]) if values else None


def undeployed(equity: float, ceiling: Optional[float]) -> float:
    if ceiling is None or not math.isfinite(equity):
        return 0.0
    return max(equity - ceiling, 0.0)


def plan_slices(volume: float, capacity: float, volume_min: float, volume_max: float, step: float,
                max_children: int) -> tuple[list[float], float]:
    """(child volumes, volume left unplaced). Every child is a valid multiple of ``step``, at most
    ``volume_max``; the total never exceeds ``min(volume, capacity)`` and is never rounded up."""
    usable = steps_of(min(volume, capacity), step)
    per_child = steps_of(volume_max, step)
    minimum = steps_of(volume_min, step) or 1
    if usable < minimum or per_child < minimum or max_children < 1:
        return [], max(volume, 0.0)
    children: list[int] = []
    while usable >= minimum and len(children) < max_children:
        take = min(usable, per_child)
        children.append(take)
        usable -= take
    placed = sum(children)
    left = float(Decimal(str(volume)) - Decimal(placed) * Decimal(str(step)))
    return [volume_from_steps(c, step) for c in children], max(left, 0.0)


# ----------------------------------------------------------------------- modes


class ModeTracker:
    """Operating mode from the two ratios, with hysteresis so it does not flip around a threshold."""

    def __init__(self, hysteresis: float = 0.1, state: str = MODE_MIN_VIABLE) -> None:
        self.band = max(hysteresis, 0.0)
        self.state = state

    def update(self, feasible: bool, headroom: Optional[float], capacity_ratio: Optional[float]) -> str:
        if not feasible or headroom is None:
            self.state = MODE_BELOW_FLOOR
            return self.state
        ratio = capacity_ratio or 0.0
        if self.state == MODE_CAPACITY:
            if ratio > 1.0 - self.band:
                return self.state
            self.state = MODE_PERCENT
        elif ratio > 1.0 + self.band:
            self.state = MODE_CAPACITY
            return self.state
        if self.state == MODE_PERCENT:
            if headroom < 1.0 - self.band:
                self.state = MODE_MIN_VIABLE
        elif self.state == MODE_MIN_VIABLE:
            if headroom > 1.0 + self.band:
                self.state = MODE_PERCENT
        else:
            self.state = MODE_PERCENT if headroom >= 1.0 else MODE_MIN_VIABLE
        return self.state


def size_label(headroom: Optional[float], capacity_ratio: Optional[float]) -> str:
    """Display label only. No code path may branch on it, and it comes from ratios, never money."""
    if capacity_ratio is not None and capacity_ratio >= 0.5:
        return "Institutional-scale"
    if headroom is None:
        return "-"
    if headroom < 0.25:
        return "Nano"
    if headroom < 1.0:
        return "Micro"
    if headroom < 4.0:
        return "Small"
    if headroom < 40.0:
        return "Standard"
    return "Large"


def below_floor_message(equity: float, floor: Optional[float], currency: str,
                        by_symbol: Mapping[str, float]) -> str:
    """Plain PT-PT explanation for an account that cannot place any order yet."""
    lines = []
    if not math.isfinite(equity) or equity <= 0:
        lines.append("Capital zero, negativo ou desconhecido: não há novas entradas até o valor voltar a ser válido.")
    elif floor is None:
        lines.append(f"Com {equity:.2f} {currency} não há nenhum instrumento negociável: sem especificações ou custos "
                     "aceitáveis para calcular um piso.")
    else:
        lines.append(f"Com {equity:.2f} {currency} o bot não coloca ordens: o piso de viabilidade é {floor:.2f} "
                     f"{currency}.")
    if by_symbol:
        parts = ", ".join(f"{s} {v:.2f}" for s, v in sorted(by_symbol.items(), key=lambda kv: kv[1])[:8])
        lines.append(f"Capital mínimo por símbolo ({currency}): {parts}.")
    lines.append("Confirme com a corretora se existem contas micro ou cent: este sistema não sabe se existem.")
    lines.append("A aprendizagem em sombra continua. Acima do piso, por exemplo depois de um depósito, começa a "
                 "negociar sozinho, sem reiniciar nem mudar a configuração.")
    return " ".join(lines)
