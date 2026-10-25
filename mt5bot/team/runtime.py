"""``TeamRuntime``: builds the office (store, bus, supervisor, 21 agents, learner) and is
the single object the bot talks to. Every entry point is defensive: the bot catches any
exception from here and falls back to the deterministic v2 path (see ``Bot``)."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np

from ..officedb import OfficeStore, office_path
from .ablation import ablation_report, select_mode
from .agent import Agent, AgentState, TeamServices
from .agents import AGENT_CLASSES
from .agents.cio import TeamDecision, TradeGateway
from .agents.control import GuardianReport, GuardianSnapshot
from .agents.execution import ManagementAction
from .agents.ops import journal_summary
from .agents.research import find_outcome
from .context import DecisionContext, PositionView
from .ensemble import EnsembleSettings
from .events import AgentBenched, AgentRestored, EventBus, MarketSnapshot, TeamModeChanged
from .learning import Learner, votes_from_dossier
from .roster import DESK, GATES, NEVER_BENCHED, VOTERS
from .scheduler import AgentSupervisor

log = logging.getLogger("mt5_bot")

OBSERVE_STAGE = ("data_clerk", "regime_detector", "session_analyst", "news_sentinel") + DESK
LEARN_EVERY_SECONDS = 60.0
RETENTION_EVERY_SECONDS = 6 * 3600.0
VACUUM_EVERY_SECONDS = 7 * 86400.0
CALIBRATE_EVERY_OUTCOMES = 25
UNBENCH_MARGIN = 0.02
TICK_TIMEOUT_MS = 2000


class TeamRuntime:
    def __init__(self, cfg: Any, state_dir: Path, registry: Any = None, advisor: Any = None,
                 notify: Callable[[str], None] = lambda text: None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, store: Optional[OfficeStore] = None) -> None:
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.registry = registry
        self.advisor = advisor
        self.notify = notify
        self.clock = clock
        self.store = store if store is not None else OfficeStore(office_path(self.state_dir), clock)
        self.bus = EventBus(self.store, clock)
        self.learner = Learner(cfg.team, self.store)
        self.services = TeamServices(bus=self.bus, cfg=cfg, clock=clock, registry=registry, store=self.store,
                                     advisor=advisor, notify=notify, state_dir=self.state_dir)
        team = cfg.team
        self.supervisor = AgentSupervisor(self.bus, team.agent_timeout_ms, team.agent_max_failures,
                                          team.agent_failure_window_seconds, team.agent_restart_backoff_seconds,
                                          monotonic=monotonic)
        self.agents: dict[str, Agent] = {}
        for cls in AGENT_CLASSES:
            if team.agent_enabled(cls.id):
                self.agents[cls.id] = self.supervisor.register(cls(self.services))
        self.cio = self.agents["cio"]
        self.cio.runtime = self  # type: ignore[attr-defined]
        self.perf_benched: set[str] = set(json.loads(self.store.get_kv("perf_benched") or "[]"))
        for agent_id in self.perf_benched:
            if agent_id in self.agents:
                self.agents[agent_id].benched = "no value added out of sample"
        latest = self.store.latest_ablation()
        self.last_report: Optional[dict] = latest["payload"] if latest else None
        self._last_ablation = float(latest["ts"]) if latest else -math.inf
        self.effective_mode, self.mode_reason = select_mode(team.team_mode, self.last_report, team.ablation_min_samples)
        self.regime_stats: dict[tuple[str, str], tuple[int, float, float]] = {}
        self._last_learn = -math.inf
        self._last_retention = -math.inf
        self._last_vacuum = float(self.store.get_kv("last_vacuum") or "0")
        self._since_calibration = 0
        self._closed = False
        self.bus.publish(TeamModeChanged(team.team_mode, self.effective_mode, self.mode_reason), agent="cio")
        log.info("Team ready: %d agents, mode %s (%s)", len(self.agents), self.effective_mode, self.mode_reason)

    # ----------------------------------------------------------------- queries

    def active_tilts(self) -> dict[str, float]:
        try:
            return self.store.active_tilts(self.clock())
        except Exception:  # noqa: BLE001
            return {}

    def regime_ineligible(self, strategy: str, regime: str) -> Optional[str]:
        """The CIO consults a strategy only where it has not demonstrated a NEGATIVE edge."""
        stats = self.regime_stats.get((strategy, regime))
        if stats is None:
            return None
        n, mean, sd = stats
        if n < self.cfg.team.regime_min_trades:
            return None
        ucb = mean + 1.645 * sd / math.sqrt(n)
        if ucb < 0:
            return f"{strategy} has no edge in the {regime} regime ({n} outcomes, mean {mean:+.2f}R, UCB {ucb:+.2f}R)"
        return None

    # ---------------------------------------------------------------- decision

    def decide(self, ctx: DecisionContext, gateway: TradeGateway) -> TeamDecision:
        return self.cio.decide(ctx, gateway)  # type: ignore[attr-defined]

    def observe_bar(self, ctx: DecisionContext) -> None:
        """Every new bar: context and desk agents scan the market (keeps the office live)."""
        m = ctx.market
        self.bus.publish(MarketSnapshot(m.bid, m.ask, round(m.spread_points, 2), m.bar_time), agent="data_clerk",
                         symbol=m.symbol)
        ids = [a for a in OBSERVE_STAGE if a in self.agents]
        out = self.supervisor.call_many(ids, "analyze", ctx)
        view = {}
        for agent_id in ids:
            outcome = out.get(agent_id)
            if outcome is not None and outcome.ok and outcome.value is not None:
                op = outcome.value
                view[agent_id] = {"stance": op.stance, "confidence": round(op.confidence, 4),
                                  "details": {k: v for k, v in op.details.items() if k not in ("spread_by_hour",
                                                                                               "detail")}}
        strategist = self.agents.get("claude_strategist")
        regime = (view.get("regime_detector") or {}).get("details", {})
        if strategist is not None and regime:
            strategist.briefing_features = {  # type: ignore[attr-defined]
                "symbol": m.symbol, "timeframe": m.timeframe, "regime": regime.get("regime"),
                "regime_probs": regime.get("probs", {}), "atr_pct_rank": regime.get("atr_pct"),
                "sessions": (view.get("session_analyst") or {}).get("details", {}).get("sessions", []),
                "purpose": "briefing"}
        try:
            self.store.set_kv(f"market_view:{m.symbol}", json.dumps({"ts": self.clock(), "bar_time": m.bar_time,
                                                                     "bid": m.bid, "ask": m.ask,
                                                                     "spread_points": m.spread_points,
                                                                     "views": view}, default=str))
        except Exception:  # noqa: BLE001
            log.debug("market view not stored", exc_info=True)

    def guardian_check(self, snapshot: GuardianSnapshot) -> GuardianReport:
        guardian = self.agents["guardian"]
        return guardian.check(snapshot)  # type: ignore[attr-defined]

    def plan_management(self, positions: Sequence[PositionView], bars: Mapping[str, np.ndarray],
                        points: Mapping[str, float], minutes_to_news: Mapping[str, Optional[float]],
                        advisor_features: Callable[[PositionView], Mapping[str, Any]]) -> list[ManagementAction]:
        pm = self.agents.get("position_manager")
        if pm is None:
            return []
        actions = list(pm.plan(positions, dict(bars), dict(points), dict(minutes_to_news)))  # type: ignore[attr-defined]
        strategist = self.agents.get("claude_strategist")
        if strategist is not None and self.cfg.claude.manage_positions and self.effective_mode == "full":
            planned = {a.ticket for a in actions}
            for p in positions:
                if p.ticket in planned or p.unrealized_r < 1.0:
                    continue  # checkpoints: only positions that are at least 1R in profit
                suggestion = strategist.manage(advisor_features(p))  # type: ignore[attr-defined]
                action = pm.from_advisor(p, suggestion)  # type: ignore[attr-defined]
                if action is not None:
                    actions.append(action)
        return actions

    # ------------------------------------------------------------ housekeeping

    def housekeeping(self, now: float, bot_status: Mapping[str, Any]) -> None:
        ops = self.agents.get("ops_engineer")
        if ops is not None:
            ops.update_bot_status(bot_status)  # type: ignore[attr-defined]
        strategist = self.agents.get("claude_strategist")
        if strategist is not None and self.registry is not None:
            rows = self.registry.trades(mode="live", status="closed")[-200:]
            strategist.journal_summary = journal_summary(rows)  # type: ignore[attr-defined]
        self.supervisor.call_many(list(self.agents), "tick", now, timeout_ms=TICK_TIMEOUT_MS)
        self.learn_tick(now)
        if now - self._last_retention >= RETENTION_EVERY_SECONDS:
            self._last_retention = now
            vacuum = now - self._last_vacuum >= VACUUM_EVERY_SECONDS and bool(bot_status.get("weekend"))
            team = self.cfg.team
            try:
                self.store.apply_retention(team.events_retention_days, team.max_event_rows,
                                           team.dossier_retention_days, vacuum=vacuum)
                if vacuum:
                    self._last_vacuum = now
                    self.store.set_kv("last_vacuum", str(now))
            except Exception:  # noqa: BLE001
                log.warning("office.sqlite retention failed", exc_info=True)
        self.persist_agent_states()

    def persist_agent_states(self) -> None:
        health = self.supervisor.snapshot()
        weights = self.learner.snapshot()["weights"]
        rows = []
        for agent_id, agent in self.agents.items():
            snap = agent.snapshot()
            errors_bench = health[agent_id]["benched_for_errors"]
            rows.append({"agent": agent_id, "state": snap["state"], "task": snap["task"],
                         "last_message": snap["last_message"], "benched": bool(agent.benched) or errors_bench,
                         "bench_reason": agent.benched or ("repeated errors" if errors_bench else None),
                         "errors": int(health[agent_id]["failures"]),
                         "stats": {"agent": _small(snap["stats"]), "supervisor": health[agent_id],
                                   "weights": weights.get(agent_id, {})}})
        try:
            self.store.set_agent_states(rows)
        except Exception:  # noqa: BLE001
            log.debug("agent states not stored", exc_info=True)

    # ---------------------------------------------------------------- learning

    def learn_tick(self, now: float, force: bool = False) -> int:
        if not force and now - self._last_learn < LEARN_EVERY_SECONDS:
            return 0
        self._last_learn = now
        if self.registry is None:
            return 0
        resolved = 0
        pending = self.store.unresolved_dossiers(500)
        for d in pending:
            try:
                outcome = find_outcome(self.registry, d, self.cfg.bar_seconds, now)
            except Exception:  # noqa: BLE001
                log.debug("outcome lookup failed for %s", d["id"], exc_info=True)
                continue
            if outcome is None:
                continue
            kind, r = outcome
            if kind == "unresolvable":
                self.store.update_dossier(d["id"], status="unresolvable", resolved_ts=now, outcome={"kind": kind})
                continue
            team_r = float(d.get("final_multiplier") or 0.0) * r
            self.store.update_dossier(d["id"], r_base=r, resolved_ts=now,
                                      outcome={"kind": kind, "r_base": r, "won": r > 0, "taken_r": team_r})
            self.learner.update(d.get("regime") or "unknown", votes_from_dossier(d), won=r > 0)
            resolved += 1
        if resolved:
            self.learner.save()
            self._since_calibration += resolved
        if self._since_calibration >= CALIBRATE_EVERY_OUTCOMES:
            self._since_calibration = 0
            for agent_id in VOTERS:
                self.learner.fit_calibration(agent_id)
        if resolved or force:
            self._refresh_from_resolved(now, force)
        analyst = self.agents.get("performance_analyst")
        if analyst is not None:
            analyst.report_resolution(resolved, len(pending) - resolved,  # type: ignore[attr-defined]
                                      self.learner.regime_counts)
        return resolved

    def _refresh_from_resolved(self, now: float, force: bool) -> None:
        rows = self.store.resolved_dossiers()
        stats: dict[tuple[str, str], list[float]] = defaultdict(list)
        for d in rows:
            stats[(d.get("strategy") or "", d.get("regime") or "unknown")].append(float(d["r_base"]))
        self.regime_stats = {k: (len(v), float(np.mean(v)), float(np.std(v, ddof=1)) if len(v) > 1 else 0.0)
                             for k, v in stats.items()}
        meta = self.agents.get("meta_labeler")
        if meta is not None:
            training = [((d.get("inputs") or {}).get("meta_features") or {}, int(float(d["r_base"]) > 0))
                        for d in rows]
            meta.maybe_retrain([t for t in training if t[0]])  # type: ignore[attr-defined]
        hours = self.cfg.team.ablation_every_hours
        if force or now - self._last_ablation >= hours * 3600:
            self.run_ablation(now, rows)

    def run_ablation(self, now: float, rows: Optional[list[dict]] = None) -> dict:
        team = self.cfg.team
        rows = self.store.resolved_dossiers() if rows is None else rows
        report = ablation_report(rows, EnsembleSettings.from_config(team), list(VOTERS) + list(GATES),
                                 team.ablation_confidence, team.ablation_min_samples)
        self.store.add_ablation_report(report["n"], report)
        self._last_ablation = now
        self.last_report = report
        mode, reason = select_mode(team.team_mode, report, team.ablation_min_samples)
        if mode != self.effective_mode:
            log.warning("Team mode %s -> %s: %s", self.effective_mode, mode, reason)
            if self.cfg.notifications.team_mode_changes:
                self.notify(f"Team mode {self.effective_mode} -> {mode}: {reason}")
            self.bus.publish(TeamModeChanged(team.team_mode, mode, reason), agent="cio")
        self.effective_mode, self.mode_reason = mode, reason
        self._bench_by_performance(report)
        return report

    def _bench_by_performance(self, report: Mapping[str, Any]) -> None:
        team = self.cfg.team
        changed = False
        for agent_id, entry in (report.get("agents") or {}).items():
            if agent_id not in VOTERS or agent_id in NEVER_BENCHED or agent_id not in self.agents:
                continue
            value = float(entry.get("value_added_sharpe", 0.0))
            agent = self.agents[agent_id]
            if agent_id not in self.perf_benched and entry.get("n", 0) >= team.bench_min_samples and value <= 0:
                self.perf_benched.add(agent_id)
                agent.benched = f"no value added out of sample ({value:+.3f} Sharpe over {entry['n']} decisions)"
                agent.set_state(AgentState.RESTING, task="no banco: sem valor acrescentado",
                                message="Vou para o banco; continuo a observar em sombra.")
                self.bus.publish(AgentBenched(agent.benched, "performance"), agent=agent_id)
                changed = True
            elif agent_id in self.perf_benched and value > UNBENCH_MARGIN:
                self.perf_benched.discard(agent_id)
                agent.benched = None
                agent.set_state(AgentState.IDLE, task="regressou do banco", message="A evidência voltou a favor.")
                self.bus.publish(AgentRestored(f"value added {value:+.3f} Sharpe"), agent=agent_id)
                changed = True
        if changed:
            self.store.set_kv("perf_benched", json.dumps(sorted(self.perf_benched)))

    def reset_learning(self) -> None:
        self.learner.reset()
        self.perf_benched.clear()
        self.store.set_kv("perf_benched", "[]")
        for agent in self.agents.values():
            agent.benched = None

    # ----------------------------------------------------------------- config

    def update_config(self, cfg: Any) -> None:
        self.cfg = cfg
        self.services.cfg = cfg
        self.learner.update_settings(cfg.team)
        team = cfg.team
        self.supervisor.update_settings(team.agent_timeout_ms, team.agent_max_failures,
                                        team.agent_failure_window_seconds, team.agent_restart_backoff_seconds)
        mode, reason = select_mode(team.team_mode, self.last_report, team.ablation_min_samples)
        if mode != self.effective_mode:
            self.bus.publish(TeamModeChanged(team.team_mode, mode, reason), agent="cio")
        self.effective_mode, self.mode_reason = mode, reason

    def snapshot(self) -> dict[str, Any]:
        guardian = self.agents.get("guardian")
        return {"configured_mode": self.cfg.team.team_mode, "effective_mode": self.effective_mode,
                "mode_reason": self.mode_reason, "agents": len(self.agents),
                "benched_performance": sorted(self.perf_benched),
                "benched_errors": [a for a in self.agents if self.supervisor.benched_for_errors(a)],
                "guardian_freeze": getattr(guardian, "frozen", None),
                "advisor_state": self.advisor.state.value if self.advisor is not None else "DISABLED",
                "event_bus": self.bus.stats()}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.persist_agent_states()
        except Exception:  # noqa: BLE001
            pass
        for agent in self.agents.values():
            agent.close()
        self.supervisor.close()
        self.bus.close()
        self.store.close()


def _small(stats: Mapping[str, Any], depth: int = 0) -> Any:
    """Bound the size of agent stats stored for the panel."""
    if isinstance(stats, dict):
        items = list(stats.items())[:40]
        return {str(k): _small(v, depth + 1) for k, v in items} if depth < 4 else {}
    if isinstance(stats, (list, tuple)):
        return [_small(v, depth + 1) for v in list(stats)[:40]]
    if isinstance(stats, float):
        return round(stats, 6) if math.isfinite(stats) else None
    if isinstance(stats, (int, str, bool)) or stats is None:
        return stats
    return str(stats)[:200]
