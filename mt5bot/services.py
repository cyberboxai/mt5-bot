"""Seven core services, and the team as their presentation.

The agents stay as the organizational and visual model, but what runs is a small set of services.
Each agent is a role inside one service, and the office animates real service activity. The Risk
Manager and the Guardian stay final: a service map changes names, never powers.

The complexity budget lives here too: every optional component has to show value within its
evaluation window, or it is disabled by default and documented in docs/DECISIONS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

SERVICES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "data": ("Dados", "Market data, health checks, snapshots and quality",
             ("data_clerk", "regime_detector", "session_analyst", "news_sentinel")),
    "signals": ("Sinais", "Arm signals and the optional desk votes",
                ("trend_analyst", "mean_reversion_analyst", "breakout_analyst", "mtf_analyst", "meta_labeler")),
    "allocation": ("Alocação", "Tiers, exploration, portfolio construction and sizing", ("cio", "flow_doctor")),
    "risk": ("Risco", "Hard ceilings, gates, margin guard and the final vetoes",
             ("risk_manager", "guardian", "correlation_analyst")),
    "execution": ("Execução", "Order state machine, reconciliation, protection and TCA",
                  ("execution_trader", "position_manager")),
    "research": ("Investigação", "CPCV, trial ledger, deflated Sharpe, PBO and evidence",
                 ("quant_researcher", "validation_auditor", "performance_analyst")),
    "ops": ("Operações", "Health, reports, backups, alerts and the optional advisor",
            ("ops_engineer", "reporter", "claude_strategist")),
}
SERVICE_OF: dict[str, str] = {agent: name for name, (_pt, _en, agents) in SERVICES.items() for agent in agents}
# Funnel stages each service is responsible for, so the panel can show what it really did.
STAGES: dict[str, tuple[str, ...]] = {
    "data": ("data", "scan"), "signals": ("signal",), "allocation": ("tier", "exploration", "budget", "sizing", "team"),
    "risk": ("risk", "affordability"), "execution": ("execution", "management"), "research": (), "ops": (),
}


@dataclass(frozen=True)
class Component:
    id: str
    measure: str                     # how its value is measured
    evaluation_days: int
    default_on: bool
    value: str                       # what is known today


COMPONENTS: tuple[Component, ...] = (
    Component("specialist_team_votes", "ablation: team against baseline, paired bootstrap", 90, False,
              "auto mode keeps the baseline until it proves itself"),
    Component("meta_labeler", "ablation: marginal Sharpe of the agent", 90, True, "abstains without validated data"),
    Component("flow_doctor", "counterfactual replay of every relaxation", 90, True, "rate limited and shadowed"),
    Component("claude_per_trade", "ablation: advisor value after its cost", 90, False, "off until ablation shows value"),
    Component("management_policies", "paired replays per arm", 90, True, "fixed unless evidence chooses otherwise"),
    Component("regime_routing", "arm evidence per regime", 90, True, "only blocks clearly negative regimes"),
    Component("opportunity_scan", "universe selection against a fixed universe", 90, True, "not ablated yet"),
)


def complexity_budget(ablation: Optional[Mapping[str, Any]], first_ts: Optional[float], now: float) -> list[dict[str, Any]]:
    """Status of each component. ``ablation`` is the latest report; without data nothing is claimed."""
    report = ablation or {}
    agents: Mapping[str, Any] = report.get("agents") or {}
    days = (now - first_ts) / 86400.0 if first_ts is not None else 0.0
    out = []
    for c in COMPONENTS:
        status, evidence = "proving" if days < c.evaluation_days else "no_value_shown", c.value
        if c.id == "specialist_team_votes" and report.get("n"):
            evidence = str(report.get("verdict"))
            if report.get("team_beats_baseline"):
                status = "kept"
        elif c.id == "meta_labeler" and "meta_labeler" in agents:
            value = agents["meta_labeler"]
            evidence = f"marginal Sharpe {value.get('value_added_sharpe')} over {value.get('n')} decisions"
            low = (value.get("ci") or [None])[0]
            if low is not None and low > 0:
                status = "kept"
        elif c.id == "claude_per_trade" and (report.get("advisor") or {}).get("reviews"):
            evidence = f"value added {report['advisor'].get('value_added_sharpe')} Sharpe"
        out.append({"id": c.id, "measure": c.measure, "default_on": c.default_on, "status": status, "evidence": evidence,
                    "evaluation_days": c.evaluation_days, "days_observed": round(days, 1)})
    return out


def service_activity(funnel_rows: Mapping[str, int]) -> dict[str, dict[str, Any]]:
    """Events per service in a funnel window: ``funnel_rows`` maps stage to count."""
    return {name: {"title": title, "description": text, "agents": list(agents),
                   "events": sum(int(funnel_rows.get(s, 0)) for s in STAGES[name])}
            for name, (title, text, agents) in SERVICES.items()}
