"""Research and Review: Quant Researcher, Validation Auditor, Performance Analyst, Reporter.

* The Quant Researcher reports the v2 walk-forward optimiser, champion/challenger and
  shadow-trading state, and hands vetted hypotheses to the NEXT optimisation cycle (they
  go through every gate; there is no shortcut to live trading).
* The Validation Auditor re-checks champions for look-ahead (signals on a prefix must
  equal the prefix of the signals) and audits that live, shadow and backtest share code.
  A look-ahead failure blocks entries for that symbol in every team mode.
* The Performance Analyst resolves decision outcomes from the champion's shadow trades
  (or the live trade) and drives weights, calibration, benching and the ablation.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping, Optional

import numpy as np

from ..agent import Agent, AgentState
from ..events import Event

AUDIT_EVERY_SECONDS = 24 * 3600.0
RESEARCH_EVERY_SECONDS = 600.0
MAX_OUTCOME_DAYS = 10.0


class QuantResearcher(Agent):
    id = "quant_researcher"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._last = -math.inf
        self._last_run_id: Optional[int] = None

    def tick(self, now: float) -> None:
        if now - self._last < RESEARCH_EVERY_SECONDS or self.services.registry is None:
            return
        self._last = now
        registry = self.services.registry
        runs = [dict(r) for r in registry.runs(10)]
        self.stats["runs"] = [{k: r.get(k) for k in ("id", "symbol", "trigger", "started_at", "finished_at", "status",
                                                      "trials", "notes")} for r in runs]
        self.stats["promotions"] = [{k: r[k] for k in ("id", "ts", "symbol", "action", "old_desc", "new_desc", "reason")}
                                    for r in registry.promotions(30)]
        if runs and runs[0]["id"] != self._last_run_id and runs[0]["status"] in ("done", "failed"):
            self._last_run_id = runs[0]["id"]
            self.say(f"Optimização #{runs[0]['id']} ({runs[0]['symbol']}): {runs[0]['status']}, "
                     f"{runs[0]['trials']} ensaios. {runs[0].get('notes') or ''}".strip())
        self._submit_hypotheses()
        champions = {}
        for symbol in json.loads(registry.get_kv("resolved_symbols") or "{}").values():
            champ = registry.champion(symbol)
            champions[symbol] = {"champion": champ.describe(), "strategy": champ.strategy,
                                 "challengers": [c.describe() for c, _ in registry.challengers(symbol)]}
        self.stats["champions"] = champions
        busy = any(r["status"] == "running" for r in runs)
        self.set_state(AgentState.REVIEWING if busy else AgentState.IDLE,
                       task="optimização walk-forward a correr" if busy else "à espera do próximo ciclo")

    def _submit_hypotheses(self) -> None:
        store, registry = self.services.store, self.services.registry
        if store is None:
            return
        queued = store.hypotheses(status="queued")
        if not queued:
            return
        pending = json.loads(registry.get_kv("research_hypotheses") or "[]")
        for h in queued:
            pending.append({"id": h["id"], "strategy": h["strategy"], "param": h["param"], "value": h["value"]})
            store.mark_hypothesis(h["id"], "submitted", "added to the next walk-forward cycle (all gates apply)")
        registry.set_kv("research_hypotheses", json.dumps(pending[-50:]))
        self.say(f"{len(queued)} hipótese(s) enviada(s) para o próximo ciclo de optimização (sem atalhos)")


class ValidationAuditor(Agent):
    id = "validation_auditor"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._last = -math.inf
        self.flags: dict[str, str] = {}

    def tick(self, now: float) -> None:
        if now - self._last < AUDIT_EVERY_SECONDS or self.services.registry is None:
            return
        self._last = now
        self.set_state(AgentState.REVIEWING, task="auditoria de validação")
        self.stats["code_paths"] = code_path_audit()
        registry = self.services.registry
        results = {}
        flags: dict[str, str] = {}
        for symbol in json.loads(registry.get_kv("resolved_symbols") or "{}").values():
            champ = registry.champion(symbol)
            if champ.is_notrade:
                continue
            bars = registry.load_bars(symbol, self.cfg.timeframe)
            leak = lookahead_audit(champ.strategy, champ.params, bars[-1500:]) if len(bars) > 400 else None
            gates = gate_audit(champ.metrics, self.cfg)
            results[symbol] = {"champion": champ.describe(), "lookahead_ok": leak is None, "lookahead": leak,
                               "gates": gates}
            if leak:
                flags[symbol] = f"look-ahead detected in {champ.describe()}: {leak}"
        self.flags = flags
        self.stats["audits"] = results
        if not all(self.stats["code_paths"].values()):
            self.say("Auditoria: os caminhos de código de backtest e live divergem!", severity="warning")
        if flags:
            self.say(f"Auditoria: {len(flags)} campeão(ões) bloqueado(s) por fuga de dados", severity="warning")
        self.set_state(AgentState.IDLE, task="auditoria concluída",
                       message=f"Auditoria: {len(results)} campeão(ões) verificados, {len(flags)} bloqueado(s)")


def code_path_audit() -> dict[str, bool]:
    """Live, shadow and backtest must share the same sizing and fill functions."""
    from ... import backtest, bot, engine, risk

    return {
        "live_uses_risk_plan_trade": bot.plan_trade is risk.plan_trade,
        "backtest_uses_risk_plan_trade": backtest.plan_trade is risk.plan_trade,
        "shadow_uses_backtest_open": engine.open_position is backtest.open_position,
        "shadow_uses_backtest_exits": engine.exit_for_bar is backtest.exit_for_bar,
        "shadow_uses_backtest_close": engine.close_position is backtest.close_position,
    }


def lookahead_audit(strategy: str, params: Mapping[str, Any], bars: np.ndarray, checks: int = 5,
                    seed: int = 1) -> Optional[str]:
    """None if signals on every tested prefix equal the prefix of the full-sample signals."""
    from ... import strategies as strategy_lib

    strat = strategy_lib.get(strategy)
    full = strat.signals(bars, params).side
    rng = np.random.default_rng(seed)
    n = len(bars)
    for k in sorted(set(int(x) for x in rng.integers(max(50, n // 3), n, checks))):
        prefix = strat.signals(bars[:k], params).side
        if not np.array_equal(prefix, full[:k]):
            first = int(np.flatnonzero(prefix != full[:k])[0])
            return f"signals differ at bar {first} when the data ends at bar {k}"
    return None


def gate_audit(metrics: Mapping[str, Any], cfg: Any) -> dict[str, Any]:
    if not metrics:
        return {"status": "no stored validation metrics (seeded or manual champion)"}
    checks = {
        "deflated_sharpe": (metrics.get("deflated_sharpe"), cfg.min_deflated_sharpe),
        "neighbor_pass_ratio": (metrics.get("neighbor_pass_ratio"), cfg.min_neighbor_pass_ratio),
        "trades": (metrics.get("trades"), cfg.min_oos_trades),
        "profit_factor": (metrics.get("profit_factor"), cfg.min_profit_factor),
    }
    out: dict[str, Any] = {}
    for name, (value, minimum) in checks.items():
        out[name] = {"value": value, "minimum": minimum,
                     "ok": value is not None and float(value) >= float(minimum)}
    out["multiple_testing_trials"] = metrics.get("total_trials")
    return out


class PerformanceAnalyst(Agent):
    id = "performance_analyst"
    subscriptions = ("trade.closed",)

    def observe(self, event: Event) -> None:
        if event.payload.get("mode") == "live":
            self.stats["last_close"] = {"symbol": event.symbol, "r": event.payload.get("r"), "ts": event.ts}

    def report_resolution(self, resolved: int, pending: int, regime_counts: Mapping[str, int]) -> None:
        self.stats.update(resolved_total=self.stats.get("resolved_total", 0) + resolved, pending=pending,
                          regime_counts=dict(regime_counts))
        if resolved:
            self.set_state(AgentState.REVIEWING, task="a actualizar pesos e calibração",
                           message=f"{resolved} decisão(ões) resolvida(s); pesos actualizados")
        else:
            self.set_state(AgentState.MONITORING, task=f"{pending} decisão(ões) à espera de resultado")


def is_next_bar(decision_bar: int, open_bar: int, bar_seconds: int) -> bool:
    """True if ``open_bar`` is the first bar after ``decision_bar`` (allowing a weekend gap)."""
    gap = int(open_bar) - int(decision_bar)
    if gap <= 0:
        return False
    if gap <= bar_seconds:
        return True
    weekday = ((int(decision_bar) // 86400) + 3) % 7  # Monday = 0
    return weekday == 4 and gap <= 3 * 86400  # Friday close to the Sunday/Monday open


def find_outcome(registry: Any, dossier: Mapping[str, Any], bar_seconds: int, now: float) -> Optional[tuple[str, float]]:
    """('shadow'|'live', R) when resolved, ('unresolvable', nan) after a timeout, else None (pending)."""
    ps, symbol, bar_time = dossier.get("param_set_id"), dossier.get("symbol"), dossier.get("bar_time")
    if ps is not None and bar_time is not None:
        # The shadow trade of this proposal opens on the next bar, i.e. after the dossier was written.
        since = float(dossier.get("ts") or 0.0) - 2 * bar_seconds
        rows = [r for r in registry.trades(mode="shadow", param_set_id=int(ps), symbol=symbol, since=since)
                if r.open_bar_time is not None and int(r.open_bar_time) > int(bar_time)]
        if rows:
            first = min(rows, key=lambda r: int(r.open_bar_time))
            if is_next_bar(int(bar_time), int(first.open_bar_time), bar_seconds):
                if first.status == "closed" and first.r is not None:
                    return "shadow", float(first.r)
                return None
    ticket = (dossier.get("execution") or {}).get("ticket")
    if ticket:
        row = registry.trade_by_ticket(int(ticket))
        if row is not None and row.status == "closed" and row.r is not None:
            return "live", float(row.r)
        if row is not None and row.status == "open":
            return None
    if now - float(dossier.get("ts") or now) > MAX_OUTCOME_DAYS * 86400:
        return "unresolvable", float("nan")
    return None


class Reporter(Agent):
    id = "reporter"

    def reported(self, label: str, path: str) -> None:
        self.set_state(AgentState.REVIEWING, task=f"relatório {label}", message=f"Relatório {label} escrito: {path}")
