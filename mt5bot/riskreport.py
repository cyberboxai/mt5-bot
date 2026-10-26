"""``python mt5_bot.py --risk-report``: Monte Carlo of a risk profile, so the owner chooses risk
with numbers in front of them. Everything printed is an ESTIMATE from a simplified model:

* trades per day ~ Poisson (from recent activity, or a default);
* each trade is T1, T2 or T3 in the proportions of the current arms (or a default mix);
* R per trade is a two-point outcome (stop = -1R, target = +payoff R) whose mean is the edge
  assumed for that scenario, costs included;
* sizing follows the profile: tier risk scaled by the profit-floor cushion, the daily loss
  limit stops the day, the exploration budget caps T1 losses, the hard drawdown breaker ends
  the path ("ruin" here means the breaker tripped: trading stops and a human must review).

Scenarios always include ZERO edge (only costs), because nothing guarantees an edge exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from .profiles import EffectiveLimits

DEFAULT_MIX = {"T1": 0.7, "T2": 0.25, "T3": 0.05}
SCENARIOS = (("sem vantagem (só custos)", -0.05), ("vantagem pequena", 0.05), ("vantagem moderada", 0.15))


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    edge_r: float
    ruin_prob: float
    floor_prob: float
    dd_mean: float
    dd_p95: float
    dd_worst: float
    final_median: float
    final_p05: float
    loss_prob: float
    recovery_days_median: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def simulate(lim: EffectiveLimits, edge_r: float, days: int = 250, paths: int = 2000, trades_per_day: float = 3.0,
             mix: Optional[Mapping[str, float]] = None, payoff: float = 1.5, seed: int = 7) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    mix = dict(mix or DEFAULT_MIX)
    tiers = list(mix)
    probs = np.array([mix[t] for t in tiers], dtype=float)
    probs = probs / probs.sum()
    tier_risk = np.array([lim.tier_risk_pct(t) / 100.0 for t in tiers])
    is_t1 = np.array([t == "T1" for t in tiers])
    p_win = min(max((edge_r + 1.0) / (payoff + 1.0), 0.0), 1.0)
    equity = np.ones(paths)
    c0 = np.ones(paths)
    hwm = np.ones(paths)
    peak = np.ones(paths)
    alive = np.ones(paths, dtype=bool)
    floor_hit = np.zeros(paths, dtype=bool)
    max_dd = np.zeros(paths)
    dd_start = np.full(paths, -1)
    recovery: list[float] = []
    fdd = lim.floor_drawdown_pct / 100.0
    week_used = np.zeros(paths)
    for day in range(days):
        if day % 5 == 0:
            week_used[:] = 0.0
        start = equity.copy()
        day_used = np.zeros(paths)
        n_trades = rng.poisson(trades_per_day, paths)
        for k in range(int(n_trades.max(initial=0))):
            active = alive & (n_trades > k) & ((start - equity) / start < lim.daily_loss_pct / 100.0)
            if not active.any():
                break
            floor = hwm - (fdd * c0 + (1 - lim.lock_fraction) * np.maximum(0.0, hwm - c0))
            cushion = np.maximum(equity - floor, 0.0)
            factor = np.clip(cushion / np.maximum(fdd * hwm, 1e-12), 0.0, lim.house_money_max)
            floor_hit |= active & (cushion <= 0)
            active &= cushion > 0
            idx = rng.choice(len(tiers), size=paths, p=probs)
            risk = tier_risk[idx] * np.where(is_t1[idx], 1.0, factor)
            budget_day = lim.explore_day_pct / 100.0 - day_used
            budget_week = lim.explore_week_pct / 100.0 - week_used
            t1_ok = np.minimum(budget_day, budget_week) >= risk
            active &= ~is_t1[idx] | t1_ok
            risk = np.minimum(risk, cushion / np.maximum(equity, 1e-12))
            win = rng.random(paths) < p_win
            r = np.where(win, payoff, -1.0)
            pnl = np.where(active, equity * risk * r, 0.0)
            loss_t1 = np.where(active & is_t1[idx] & (pnl < 0), -pnl / np.maximum(equity, 1e-12), 0.0)
            day_used += loss_t1
            week_used += loss_t1
            equity = equity + pnl
            hwm = np.maximum(hwm, equity)
            peak = np.maximum(peak, equity)
            dd = (peak - equity) / peak
            new_dd = dd > max_dd
            max_dd = np.maximum(max_dd, dd)
            dd_start = np.where(new_dd & (dd_start < 0), day, dd_start)
            tripped = alive & (dd >= lim.drawdown_breaker_pct / 100.0)
            alive &= ~tripped
        recovered = (dd_start >= 0) & (equity >= peak - 1e-12)
        for i in np.flatnonzero(recovered):
            recovery.append(day - dd_start[i])
        dd_start = np.where(recovered, -1, dd_start)
    return {"equity": equity, "max_dd": max_dd, "ruined": ~alive, "floor": floor_hit,
            "recovery": np.array(recovery, dtype=float)}


def run_report(lim: EffectiveLimits, days: int = 250, paths: int = 2000, trades_per_day: float = 3.0,
               mix: Optional[Mapping[str, float]] = None, scenarios: Sequence[tuple[str, float]] = SCENARIOS,
               seed: int = 7) -> list[ScenarioResult]:
    out = []
    for name, edge in scenarios:
        sim = simulate(lim, edge, days, paths, trades_per_day, mix, seed=seed)
        final = sim["equity"] - 1.0
        rec = sim["recovery"]
        out.append(ScenarioResult(name, edge, float(sim["ruined"].mean()), float(sim["floor"].mean()),
                                  float(sim["max_dd"].mean()), float(np.percentile(sim["max_dd"], 95)),
                                  float(sim["max_dd"].max()), float(np.median(final)), float(np.percentile(final, 5)),
                                  float((final < 0).mean()), float(np.median(rec)) if rec.size else None))
    return out


def format_report_pt(lim: EffectiveLimits, results: Sequence[ScenarioResult], days: int, paths: int,
                     trades_per_day: float, mix: Mapping[str, float]) -> str:
    lines = [f"Relatório de risco do perfil '{lim.profile}' ({'conta REAL' if lim.is_real else 'conta DEMO'})",
             "ATENÇÃO: são ESTIMATIVAS de um modelo simplificado. Não são previsões nem garantias.", "",
             f"Regras: risco por trade T1 {lim.t1_risk_pct:.2f}% / T2 {lim.t2_risk_pct:.2f}% / T3 até "
             f"{lim.t3_risk_pct:.2f}%; perda diária máx. {lim.daily_loss_pct:.2f}%; disjuntor {lim.drawdown_breaker_pct:.1f}% "
             f"do pico; piso de lucros a {lim.floor_drawdown_pct:.1f}% abaixo do capital inicial, bloqueia "
             f"{lim.lock_fraction:.0%} de cada novo máximo; orçamento de exploração {lim.explore_day_pct:.2f}%/dia e "
             f"{lim.explore_week_pct:.2f}%/semana.",
             f"Simulação: {paths} trajectórias de {days} dias úteis, {trades_per_day:.1f} trades/dia em média, mistura "
             f"de níveis {', '.join(f'{k} {v:.0%}' for k, v in mix.items())}.", ""]
    for r in results:
        rec = "n/d" if r.recovery_days_median is None else f"{r.recovery_days_median:.0f} dias"
        lines += [f"Cenário: {r.name} (expectativa {r.edge_r:+.2f}R por trade, custos incluídos)",
                  f"  Probabilidade de o disjuntor disparar (ruína operacional): {r.ruin_prob:.1%}",
                  f"  Probabilidade de tocar o piso de protecção: {r.floor_prob:.1%}",
                  f"  Drawdown máximo: médio {r.dd_mean:.1%}, percentil 95 {r.dd_p95:.1%}, pior {r.dd_worst:.1%}",
                  f"  Resultado ao fim do período: mediana {r.final_median:+.1%}, percentil 5 {r.final_p05:+.1%}, "
                  f"probabilidade de perda {r.loss_prob:.1%}",
                  f"  Tempo mediano para recuperar um drawdown: {rec}", ""]
    lines.append("Sem vantagem real, o resultado esperado é uma pequena perda limitada pelo orçamento de exploração e "
                 "pelos custos. O bot não pode garantir lucro.")
    return "\n".join(lines)


def estimate_inputs(registry: Any) -> tuple[float, dict[str, float]]:
    """Trades per day and tier mix from the journal (defaults when there is no history)."""
    try:
        rows = registry.trades(mode="live", status="closed", limit=500)
    except Exception:  # noqa: BLE001
        rows = []
    if len(rows) < 20:
        return 3.0, dict(DEFAULT_MIX)
    span = max((rows[-1].close_ts or 0) - (rows[0].open_ts or 0), 86400.0) / 86400.0
    counts: dict[str, float] = {}
    for r in rows:
        tier = r.tier if r.tier in DEFAULT_MIX else "T1"
        counts[tier] = counts.get(tier, 0) + 1
    total = sum(counts.values())
    return max(0.2, len(rows) / span * 5 / 7), {k: v / total for k, v in counts.items()} or dict(DEFAULT_MIX)


def recovery_text(days: Optional[float]) -> str:
    return "n/d" if days is None or not math.isfinite(days) else f"{days:.0f}"
