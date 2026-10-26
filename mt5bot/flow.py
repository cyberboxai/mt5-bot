"""Flow Doctor: the adaptive activity governor.

If markets are open, signals are being generated and no trade has happened for
``max_idle_hours`` (6 h on demo, 12 h on real by default), it finds the dominant blocker in
the funnel and relaxes THAT filter by one bounded step. Every adjustment is logged with its
evidence and gets an automatic review: if the trades made under the relaxed setting perform
poorly it is reverted. When the daily loss or the drawdown degrades, it tightens by reverting
the most recent relaxation.

Hard limits (code): every knob has a floor and a ceiling below. The Flow Doctor can never
touch the hard ceilings, SL/TP rules, the drawdown breaker, the daily loss limit, the
exploration budget or any Risk Manager / Guardian veto: none of them is a knob.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .funnel import describe
from .tiers import TIER_PT

# knob: (default delta or value, step, floor, ceiling, PT-PT label). Values are DELTAS applied to
# the configured setting, except t1_enter_p and min_agreement which are absolute overrides.
KNOBS: dict[str, tuple[float, float, float, float, str]] = {
    "hours_extend": (0.0, 1.0, 0.0, 4.0, "horas extra no horário de negociação (dentro de 1h-23h do servidor)"),
    "spread_mult": (0.0, 0.25, 0.0, 1.0, "tolerância extra a picos de spread"),
    "t1_entry_p": (0.0, -0.01, -0.03, 0.0, "redução do limiar P(E>0) de entrada em T1 (mínimo 0,52)"),
    "min_agreement": (0.0, -1.0, -20.0, 0.0, "redução da concordância mínima da equipa (mínimo 1)"),
    "min_aggregate_confidence": (0.0, -0.01, -0.02, 0.0, "redução da confiança agregada mínima (mínimo 0,50)"),
    "universe_extra": (0.0, 1.0, 0.0, 6.0, "símbolos extra no universo (os mais viáveis primeiro)"),
}


T1_ENTER_FLOOR = 0.52


@dataclass
class Adjustment:
    id: Optional[int]
    ts: float
    knob: str
    old: float
    new: float
    direction: str          # relax | tighten | revert
    reason: str
    review_after: float
    status: str = "active"  # active | kept | reverted
    evidence: dict = field(default_factory=dict)


@dataclass
class FlowState:
    knobs: dict[str, float] = field(default_factory=lambda: {k: v[0] for k, v in KNOBS.items()})
    last_adjustment_ts: float = 0.0
    status_text: str = ""

    def to_json(self) -> str:
        return json.dumps({"knobs": self.knobs, "last_adjustment_ts": self.last_adjustment_ts,
                           "status_text": self.status_text}, sort_keys=True)

    @classmethod
    def from_json(cls, text: Optional[str]) -> "FlowState":
        state = cls()
        if not text:
            return state
        try:
            data = json.loads(text)
        except ValueError:
            return state
        for k, v in (data.get("knobs") or {}).items():
            if k in KNOBS and isinstance(v, (int, float)) and math.isfinite(v):
                _d, _s, lo, hi, _l = KNOBS[k]
                state.knobs[k] = float(min(max(v, lo), hi))
        state.last_adjustment_ts = float(data.get("last_adjustment_ts") or 0.0)
        state.status_text = str(data.get("status_text") or "")
        return state


@dataclass(frozen=True)
class FlowInputs:
    now: float
    is_real: bool
    markets_open: bool
    last_order_ts: Optional[float]
    started_ts: float
    signals_in_window: int
    top_blockers: Sequence[Mapping[str, Any]]           # from funnel.report over the idle window
    counterfactuals: Mapping[str, Mapping[str, float]]
    trades_since: Mapping[int, Sequence[float]]         # adjustment id -> R of live trades opened since
    daily_loss_used_frac: float                         # 0..1 of the daily loss limit
    drawdown_frac: float                                # 0..1 of the drawdown breaker
    tier_counts: Mapping[str, int]
    min_agreement_cfg: int
    can_add_symbol: bool = True


# A relaxation needs a material blocker: seen at least this often and this share of all blocks.
MIN_BLOCKER_COUNT = 5
MIN_BLOCKER_SHARE = 0.10


def step(knob: str, value: float, direction: int) -> Optional[float]:
    """One bounded step (+1 relax, -1 tighten). None when already at the bound."""
    _default, unit, lo, hi, _label = KNOBS[knob]
    new = value + direction * unit
    new = round(min(max(new, lo), hi), 6)
    return None if math.isclose(new, value) else new


def evaluate(inp: FlowInputs, state: FlowState, active: Sequence[Adjustment], cfg: Any) -> list[Adjustment]:
    """Decide this cycle's adjustments (at most one relaxation per call). Pure."""
    out: list[Adjustment] = []
    fd = cfg
    # 1. reviews: revert relaxations whose trades did poorly; keep the rest after the review time.
    for adj in active:
        if adj.direction != "relax" or adj.status != "active":
            continue
        rs = list(inp.trades_since.get(adj.id or -1, ()))
        mean = sum(rs) / len(rs) if rs else None
        enough = len(rs) >= fd.review_after_trades
        if mean is not None and (enough or inp.now >= adj.review_after) and mean < fd.revert_mean_r:
            out.append(Adjustment(None, inp.now, adj.knob, adj.new, adj.old, "revert",
                                  f"revisão: {len(rs)} trade(s) com a regra relaxada, média {mean:+.2f}R < "
                                  f"{fd.revert_mean_r:+.2f}R; a reverter", inp.now, "active",
                                  {"reviewed": adj.id, "trades": len(rs), "mean_r": mean}))
            adj.status = "reverted"
        elif enough or inp.now >= adj.review_after:
            adj.status = "kept"
    # 2. tighten when the account degrades: undo the most recent active relaxation.
    degraded = inp.daily_loss_used_frac >= fd.tighten_daily_loss_used or inp.drawdown_frac >= 0.5
    if degraded:
        recent = [a for a in active if a.direction == "relax" and a.status == "active"]
        if recent:
            adj = max(recent, key=lambda a: a.ts)
            out.append(Adjustment(None, inp.now, adj.knob, adj.new, adj.old, "tighten",
                                  f"perda diária usada {inp.daily_loss_used_frac:.0%} do limite, drawdown "
                                  f"{inp.drawdown_frac:.0%} do disjuntor: a apertar", inp.now, "active",
                                  {"reverted": adj.id}))
            adj.status = "reverted"
        return out
    # 3. relax one bounded step when idle while signals flow.
    idle_hours = cfg.max_idle_hours_real if inp.is_real else cfg.max_idle_hours_demo
    since = inp.last_order_ts if inp.last_order_ts is not None else inp.started_ts
    idle = (inp.now - since) / 3600.0
    if not inp.markets_open or idle < idle_hours or inp.signals_in_window <= 0:
        return out
    if inp.now - state.last_adjustment_ts < cfg.min_hours_between_adjustments * 3600:
        return out
    for blocker in inp.top_blockers:
        knob = blocker.get("knob")
        reason = blocker.get("reason")
        if not knob or knob not in KNOBS:
            continue
        if int(blocker.get("count") or 0) < MIN_BLOCKER_COUNT or float(blocker.get("share") or 0.0) < MIN_BLOCKER_SHARE:
            continue  # a rare blocker is not why the bot is idle: never loosen a filter for it
        cf = inp.counterfactuals.get(reason) or {}
        if cf.get("n", 0) >= 10 and cf.get("virtual_mean_r", 0.0) < -0.10:
            continue  # this filter demonstrably protects value: never relax it
        if knob == "universe_extra" and not inp.can_add_symbol:
            continue
        current = state.knobs.get(knob, KNOBS[knob][0])
        if knob == "min_agreement" and inp.min_agreement_cfg + current <= 1:
            continue
        new = step(knob, current, +1)
        if new is None:
            continue
        out.append(Adjustment(None, inp.now, knob, current, new, "relax",
                              f"{idle:.1f} h sem trades com {inp.signals_in_window} sinal(is); bloqueio principal "
                              f"{reason} ({describe(reason)}, {blocker.get('count', 0)} vezes): {KNOBS[knob][4]} "
                              f"{current:g} -> {new:g}", inp.now + cfg.review_after_hours * 3600, "active",
                              {"blocker": reason, "count": blocker.get("count"), "idle_hours": round(idle, 2),
                               "counterfactual": cf}))
        break
    return out


def apply(state: FlowState, adjustments: Sequence[Adjustment]) -> None:
    for adj in adjustments:
        state.knobs[adj.knob] = adj.new
        state.last_adjustment_ts = max(state.last_adjustment_ts, adj.ts)


def overrides(cfg: Any, state: FlowState) -> dict[str, Any]:
    """Config overrides implied by the knobs (``config.apply_overrides`` re-validates them)."""
    from .config import widen_hours

    k = state.knobs
    out: dict[str, Any] = {}
    if k.get("hours_extend"):
        out["trading_hours_server"] = widen_hours(tuple(cfg.trading_hours_server), int(k["hours_extend"]))
    if k.get("spread_mult"):
        out["universe.spread_spike_mult"] = min(cfg.universe.spread_spike_mult + k["spread_mult"], 10.0)
    if k.get("t1_entry_p"):
        # Floor 0.52 so the exit threshold (at least 0.50) stays below the entry one (hysteresis).
        enter = max(T1_ENTER_FLOOR, cfg.tiers.t1_enter_p + k["t1_entry_p"])
        out["tiers.t1_enter_p"] = round(enter, 4)
        out["tiers.t1_exit_p"] = round(max(0.50, min(cfg.tiers.t1_exit_p, enter - 0.02)), 4)
    if k.get("min_agreement"):
        out["team.min_agreement"] = max(1, int(cfg.team.min_agreement + k["min_agreement"]))
    if k.get("min_aggregate_confidence"):
        lo = max(0.50, cfg.team.min_aggregate_confidence + k["min_aggregate_confidence"])
        out["team.min_aggregate_confidence"] = round(lo, 4)
    return out


def status_text(tier_counts: Mapping[str, int], top: Optional[Mapping[str, Any]], hours: float,
                trading: bool, halted: Optional[str]) -> str:
    """Plain-language PT-PT status for the panel and the log."""
    if halted:
        return f"Pausado: {halted}"
    active = {t: n for t, n in tier_counts.items() if t != "T0" and n}
    if active:
        parts = ", ".join(f"{n} braço{'s' if n != 1 else ''} {t}" for t, n in sorted(active.items()))
        mode = "A negociar" if trading else "Pronto a negociar"
        best = max(active, key=lambda t: int(t[1]))
        head = f"{mode} ({TIER_PT.get(best, best).lower()}): {parts}."
    else:
        head = "Em exploração: nenhum braço com evidência suficiente; só trades de exploração com micro-risco."
    if top:
        symbol = (top.get("example") or {}).get("symbol")
        where = f" em {symbol}" if symbol else ""
        head += f" Bloqueio principal nas últimas {hours:g}h: {describe(str(top.get('reason')))}{where}."
    return head


def adjustment_row(adj: Adjustment) -> dict[str, Any]:
    out = asdict(adj)
    out["label"] = KNOBS.get(adj.knob, (0, 0, 0, 0, adj.knob))[4]
    return out
