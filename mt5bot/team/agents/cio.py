"""Chief Investment Officer: runs the decision protocol and owns the decision dossier.

Protocol for one arm proposal (the ONLY thing that can originate a trade; the allocation
stage selected it and the capital rules sized it):

1. context   Data Clerk, Regime Detector, Session Analyst, News Sentinel, Correlation
             Analyst, Risk Manager pre-check, Guardian (all supervised, in parallel);
2. proposals the Strategy Desk votes (the analyst owning the arm's family proposes);
3. evaluation the Meta-Labeler scores the candidate (stage 2b, it uses the desk votes);
4. aggregation bounded ensemble with learned weights; advisor tilts can only shrink it;
5. risk gate the hard risk layer (``RiskManager.approve_entry``) previews and, at send
             time, re-checks; any veto ends the process;
6. advisor   only when the pipeline already wants to enter; bounded by the latency
             budget; "unavailable" changes nothing;
7. execution through the gateway the bot provides, which can only scale the base size
             DOWN (multiplier in [0, 1]) and always attaches SL/TP.

``team_mode``: in ``baseline`` the multiplier is 1 (simple rules: the sized proposal as is)
and the team and advisor are recorded as counterfactuals for the ablation; in ``full``
their reductions apply.
Safety gates (data unhealthy, look-ahead audit flag, Guardian freeze, panel pause,
risk layer) apply in EVERY mode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, Sequence

from ...advisor.types import AdvisorOpinion, unavailable
from ..agent import Agent, AgentState, Opinion
from ..context import DecisionContext
from ..ensemble import (Constraint, EnsembleResult, EnsembleSettings, Vote, aggregate, apply_weights, conservative)
from ..events import DecisionFinished, DecisionStarted
from ..roster import DESK, GATES
from .desk import family_for
from .metalabel import features_from

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import TeamRuntime

CONTEXT_STAGE = ("data_clerk", "regime_detector", "session_analyst", "news_sentinel", "correlation_analyst",
                 "risk_manager", "guardian")
META_STAGE = ("meta_labeler",)
MAX_DETAIL_ITEMS = 24


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    volume: float = 0.0
    base_volume: float = 0.0
    risk_pct: float = 0.0


@dataclass(frozen=True)
class ExecutionResult:
    sent: bool
    ok: bool
    reason: str
    volume: float = 0.0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    slippage_points: float = 0.0
    ticket: Optional[int] = None
    dry_run: bool = False
    risk_allowed: Optional[bool] = None
    risk_reason: str = ""
    base_volume: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class TradeGateway(Protocol):
    """Bound by the bot to ONE proposal: the CIO can only choose a multiplier in [0, 1]."""

    def preview(self, multiplier: float) -> GateResult: ...

    def submit(self, multiplier: float, dossier_id: str) -> ExecutionResult: ...


@dataclass(frozen=True)
class TeamDecision:
    dossier_id: str
    action: str
    multiplier: float
    mode: str
    reasons: tuple[str, ...]
    execution: Optional[ExecutionResult] = None
    submitted: bool = False
    counterfactual_full: Optional[float] = None
    advisor: Optional[AdvisorOpinion] = None


def compact(details: Mapping[str, Any]) -> dict[str, Any]:
    """Keep dossier details small: scalars, short lists and small dicts only."""
    out: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, (bool, int, str)) or value is None:
            out[key] = value
        elif isinstance(value, float):
            out[key] = round(value, 6) if math.isfinite(value) else None
        elif isinstance(value, (list, tuple)) and len(value) <= MAX_DETAIL_ITEMS:
            out[key] = list(value)
        elif isinstance(value, dict) and len(value) <= MAX_DETAIL_ITEMS:
            out[key] = {str(k): v for k, v in value.items()}
    return out


class ChiefInvestmentOfficer(Agent):
    id = "cio"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self.runtime: Optional["TeamRuntime"] = None

    # ----------------------------------------------------------------- helpers

    def _valid(self, outcome: Any, agent_id: str, ctx: DecisionContext) -> Optional[Opinion]:
        """Accept only well-formed, fresh opinions that the agent produced for THIS decision."""
        if outcome is None or not outcome.ok or not isinstance(outcome.value, Opinion):
            return None
        op = outcome.value
        if op.agent != agent_id or op.symbol != ctx.market.symbol or op.expired(self.now()):
            return None
        return op

    def _enabled(self, ids: Sequence[str]) -> list[str]:
        rt = self.runtime
        return [a for a in ids if rt is not None and a in rt.supervisor.agents]

    @staticmethod
    def _role(agent_id: str) -> str:
        if agent_id in GATES:
            return "gate"
        if agent_id in ("risk_manager", "guardian"):
            return "control"
        if agent_id == "regime_detector":
            return "context"
        return "voter"

    # ----------------------------------------------------------------- decide

    def decide(self, ctx: DecisionContext, gateway: TradeGateway) -> TeamDecision:
        rt = self.runtime
        assert rt is not None and ctx.proposal is not None
        team = self.cfg.team
        settings = EnsembleSettings.from_config(team)
        sup = rt.supervisor
        prop = ctx.proposal
        sym, did = ctx.market.symbol, ctx.decision_id
        side = 1 if prop.side == "BUY" else -1
        mode, mode_reason = rt.effective_mode, rt.mode_reason
        participants = tuple(self._enabled(CONTEXT_STAGE + DESK + META_STAGE))
        self.bus.publish(DecisionStarted(prop.side, prop.strategy, participants, mode), agent=self.id, symbol=sym,
                         correlation_id=did)
        self.set_state(AgentState.DECIDING, task=f"{sym}: reunião de decisão",
                       message=f"Reunião: {prop.side} {sym} proposto por {prop.strategy} (modo {mode})",
                       symbol=sym, correlation_id=did)

        stored: list[dict] = []
        reasons: list[str] = []
        all_mode_block: Optional[str] = None

        # 1. context
        out1 = sup.call_many(self._enabled(CONTEXT_STAGE), "analyze", ctx)
        views: dict[str, Any] = {}
        gate_ops: dict[str, Opinion] = {}
        for agent_id in self._enabled(CONTEXT_STAGE):
            op = self._valid(out1.get(agent_id), agent_id, ctx)
            if op is None:
                stored.append({"agent": agent_id, "role": self._role(agent_id), "stance": "unavailable",
                               "error": getattr(out1.get(agent_id), "error", "missing")})
                if agent_id == "data_clerk":
                    all_mode_block = "data clerk unavailable: entries blocked (fails closed)"
                continue
            views[agent_id] = op.details
            if agent_id in GATES:
                gate_ops[agent_id] = op
            self.publish_opinion(op, correlation_id=did)
            stored.append(self._store_op(op, self._role(agent_id)))
        if "data_clerk" in views and not views["data_clerk"].get("healthy", False):
            all_mode_block = "data unhealthy: " + "; ".join(views["data_clerk"].get("problems", []))[:200]
        auditor = rt.agents.get("validation_auditor")
        if auditor is not None and sym in getattr(auditor, "flags", {}):
            all_mode_block = auditor.flags[sym]
        ctx2 = ctx.with_views(views)

        # 2. proposals and votes from the desk
        regime = str(views.get("regime_detector", {}).get("regime", "unknown"))
        out2 = sup.call_many(self._enabled(DESK), "analyze", ctx2)
        voter_ops: dict[str, Opinion] = {}
        for agent_id in self._enabled(DESK):
            op = self._valid(out2.get(agent_id), agent_id, ctx2)
            if op is None:
                stored.append({"agent": agent_id, "role": "voter", "stance": "unavailable",
                               "error": getattr(out2.get(agent_id), "error", "missing")})
                continue
            voter_ops[agent_id] = op
        proposer = family_for(prop.strategy, prop.params)
        if proposer not in voter_ops:
            # The owning analyst is benched, disabled or failed: the arm still speaks for itself.
            voter_ops["champion"] = Opinion("champion", sym, ctx.market.timeframe, "long" if side > 0 else "short",
                                            0.6, timestamp=self.now(), reasons=(("PROPOSER", "arm signal"),),
                                            details={"proposer": True, "stand_in_for": proposer})

        # 2b. meta-labeler, fed with a preliminary read of the desk
        prelim = aggregate(side, [Vote(a, o.stance, o.confidence) for a, o in voter_ops.items()
                                  if o.stance in ("long", "short", "flat")], [], settings)
        proposer_conf = next((o.confidence for o in voter_ops.values() if o.details.get("proposer")), 0.5)
        meta_features = features_from(views, prop.side, {"agreement": prelim.agreement_count / max(1, len(voter_ops)),
                                                         "aggregate_confidence": prelim.aggregate_confidence,
                                                         "disagreement": prelim.disagreement,
                                                         "proposer_confidence": proposer_conf})
        ctx3 = ctx2.with_views({"meta_features": meta_features})
        out3 = sup.call_many(self._enabled(META_STAGE), "analyze", ctx3)
        for agent_id in self._enabled(META_STAGE):
            op = self._valid(out3.get(agent_id), agent_id, ctx3)
            if op is not None:
                voter_ops[agent_id] = op

        # 3-4. calibrated, weighted, bounded aggregation
        votes: list[Vote] = []
        weights: dict[str, float] = {}
        for agent_id in sorted(voter_ops):
            op = voter_ops[agent_id]
            calibrated = rt.learner.calibrate(agent_id, op.confidence)
            weight = rt.learner.weight(agent_id, regime)
            benched = agent_id in rt.perf_benched
            entry = self._store_op(op, "voter")
            entry.update(raw_confidence=round(op.confidence, 6), confidence=round(calibrated, 6),
                         weight=round(weight, 6), benched=benched)
            stored.append(entry)
            self.publish_opinion(op, correlation_id=did)
            if op.stance in ("long", "short", "flat") and not benched:
                votes.append(Vote(agent_id, op.stance, calibrated, weight))
                weights[agent_id] = weight
        constraints = []
        for agent_id, op in sorted(gate_ops.items()):
            if op.stance == "veto":
                constraints.append(Constraint(agent_id, "veto", 0.0, op.reasons[0][1] if op.reasons else ""))
            elif op.size_hint < 1.0:
                constraints.append(Constraint(agent_id, "size", op.size_hint, op.reasons[0][1] if op.reasons else ""))
        result = aggregate(side, votes, constraints, settings)
        tilts = rt.active_tilts()
        tilted: Optional[EnsembleResult] = None
        team_m = result.multiplier
        if tilts:
            tilted = aggregate(side, apply_weights(votes, {v.agent: v.weight for v in votes}, tilts), constraints,
                               settings)
            team_m = conservative(result.multiplier, tilted.multiplier)  # tilts can only shrink, never grow
        rule_m, rule_reason = 1.0, ""
        ineligible = rt.regime_ineligible(prop.strategy, regime)
        if ineligible:
            rule_m, rule_reason = 0.0, ineligible
        full_team_m = conservative(team_m, rule_m)
        reasons.extend(result.reasons)
        if rule_reason:
            reasons.append(rule_reason)
        m_mode = full_team_m if mode == "full" else 1.0
        if all_mode_block:
            m_mode = 0.0
            reasons.append(all_mode_block)

        # 5. risk preview (cheap, pure) before spending an advisor call
        preview: Optional[GateResult] = None
        risk_agent = rt.agents.get("risk_manager")
        if m_mode > 0:
            preview = gateway.preview(m_mode)
            if risk_agent is not None:
                risk_agent.record(preview.allowed, preview.reason, sym, did, stage="preview")
            if not preview.allowed:
                reasons.append(f"risk layer: {preview.reason}")

        # 6. advisor, only when the pipeline already wants to enter
        advisor_op: AdvisorOpinion = unavailable("not consulted: the deterministic pipeline does not want to enter")
        strategist = rt.agents.get("claude_strategist")
        if preview is not None and preview.allowed and strategist is not None and self.cfg.claude.review_trades:
            features = self._advisor_features(ctx2, prop, result, full_team_m, regime, votes)
            news_view = views.get("news_sentinel") or {}
            headlines = news_view.get("headlines", []) if self.cfg.news.advisor_headlines else []
            advisor_op = strategist.review(features, headlines, sym, did)
        m_adv = advisor_op.effective_multiplier
        final_m = conservative(m_mode, m_adv) if mode == "full" else m_mode
        counterfactual_full = 0.0 if all_mode_block else conservative(full_team_m, m_adv)

        # 7. execution through the gateway (risk gate re-checked at send time)
        execution: Optional[ExecutionResult] = None
        submitted = False
        action = "no_trade"
        if final_m > 0 and preview is not None and preview.allowed:
            exec_agent = rt.agents.get("execution_trader")
            if exec_agent is not None:
                exec_agent.executing(sym, prop.side, final_m, did)
            submitted = True
            execution = gateway.submit(final_m, did)
            if risk_agent is not None and execution.risk_allowed is not None:
                risk_agent.record(execution.risk_allowed, execution.risk_reason or "ok", sym, did, stage="gate")
            if exec_agent is not None:
                exec_agent.done(sym, execution.ok, execution.reason, did)
            action = "enter" if execution.ok else "no_trade"
            if not execution.ok:
                reasons.append(f"not executed: {execution.reason}")
        elif final_m <= 0 and not all_mode_block and mode == "full":
            reasons.append("team/advisor reduced the size to zero")

        dossier = {
            "id": did, "ts": self.now(), "symbol": sym, "timeframe": ctx.market.timeframe,
            "bar_time": ctx.market.bar_time, "side": prop.side, "mode": mode, "strategy": prop.strategy,
            "param_set_id": prop.param_set_id, "regime": regime,
            "session": ",".join((views.get("session_analyst") or {}).get("sessions", []) or []),
            "status": "executed" if action == "enter" else "skipped", "final_action": action,
            "team_multiplier": round(full_team_m, 6), "advisor_multiplier": round(m_adv, 6) if advisor_op.available
            else None, "final_multiplier": round(final_m if action == "enter" else 0.0, 6),
            "inputs": {"proposal": {"side": prop.side, "strategy": prop.strategy, "description": prop.description,
                                    "atr": prop.atr, "sl_mult": prop.sl_mult, "tp_mult": prop.tp_mult,
                                    "oos_expectancy_r": prop.oos_expectancy_r, "win_rate": prop.win_rate,
                                    "arm_id": prop.arm_id, "tier": prop.tier, "exploration": prop.exploration,
                                    "p_positive": prop.p_positive, "risk_pct": prop.risk_pct},
                       "market": {"bid": ctx.market.bid, "ask": ctx.market.ask,
                                  "spread_points": round(ctx.market.spread_points, 3)},
                       "account": {"risk_pct": ctx.account.risk_pct,
                                   "daily_loss_used_pct_of_limit": round(ctx.account.daily_loss_used_pct_of_limit, 3),
                                   "open_risk_pct": round(ctx.account.open_risk_pct, 4),
                                   "drawdown_pct": round(ctx.account.drawdown_pct, 4),
                                   "positions": len(ctx.account.positions)},
                       "meta_features": compact(meta_features), "mode_reason": mode_reason},
            "opinions": stored, "weights": weights,
            "ensemble": dict(result.to_dict(), tilted=tilted.to_dict() if tilted else None, tilts=tilts,
                             cio_rule_multiplier=rule_m, cio_rule_reason=rule_reason, all_mode_block=all_mode_block,
                             counterfactual_full=round(counterfactual_full, 6), mode=mode),
            "advisor": advisor_op.to_dict(),
            "risk": {"preview": preview.__dict__ if preview else None,
                     "gate": {"allowed": execution.risk_allowed, "reason": execution.risk_reason} if execution
                     else None},
            "execution": execution.to_dict() if execution else {},
            "outcome": {},
        }
        try:
            rt.store.save_dossier(dossier)
        except Exception:  # noqa: BLE001 - a dossier failure must never undo or repeat an order
            self.say("Não consegui gravar o dossier desta decisão (ver bot.log)", severity="warning")
        self.bus.publish(DecisionFinished(action, final_m if action == "enter" else 0.0, tuple(reasons[:6]), mode,
                                          prop.side, counterfactual_full), agent=self.id, symbol=sym,
                         correlation_id=did, severity="notice" if action == "enter" else "info")
        verdict = (f"ENTRADA {prop.side} x{final_m:.2f}" if action == "enter"
                   else f"SEM TRADE ({reasons[-1] if reasons else 'no'})")
        self.set_state(AgentState.IDLE, task=f"{sym}: {verdict}"[:120], message=f"Decisão {sym}: {verdict}"[:200],
                       symbol=sym, correlation_id=did)
        return TeamDecision(did, action, final_m if action == "enter" else 0.0, mode, tuple(reasons), execution,
                            submitted, counterfactual_full, advisor_op)

    # ---------------------------------------------------------------- helpers

    def _store_op(self, op: Opinion, role: str) -> dict[str, Any]:
        entry = op.to_dict()
        entry["details"] = compact(op.details)
        entry["role"] = role
        return entry

    def _advisor_features(self, ctx: DecisionContext, prop: Any, result: EnsembleResult, team_m: float,
                          regime: str, votes: Sequence[Vote]) -> dict[str, Any]:
        views = ctx.views
        session = views.get("session_analyst") or {}
        regime_view = views.get("regime_detector") or {}
        news = (views.get("news_sentinel") or {}).get("next") or {}
        a = ctx.account
        return {
            "symbol": ctx.market.symbol, "timeframe": ctx.market.timeframe, "side": prop.side,
            "strategy": prop.strategy, "regime": regime, "regime_probs": regime_view.get("probs", {}),
            "sessions": session.get("sessions", []), "spread_points": ctx.market.spread_points,
            "spread_vs_typical": session.get("spread_ratio"), "atr_pct_rank": regime_view.get("atr_pct"),
            "daily_loss_used_pct_of_limit": a.daily_loss_used_pct_of_limit, "open_risk_pct": a.open_risk_pct,
            "drawdown_pct": a.drawdown_pct, "drawdown_limit_pct": a.drawdown_limit_pct, "risk_pct": a.risk_pct,
            "equity_change_today_pct": a.equity_change_today_pct, "team_multiplier": team_m,
            "aggregate_confidence": result.aggregate_confidence, "disagreement": result.disagreement,
            "agreement_count": result.agreement_count, "arm_expectancy_r": prop.oos_expectancy_r, "tier": prop.tier,
            "p_positive": prop.p_positive,
            "news_minutes_to_next": news.get("minutes"),
            "votes": [{"agent": v.agent, "stance": v.stance, "confidence": v.confidence} for v in votes],
        }
