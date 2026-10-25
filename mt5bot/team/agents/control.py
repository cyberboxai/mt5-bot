"""Risk and Control: the Risk Manager (wraps the hard risk layer) and the Guardian.

The Risk Manager's veto is final: the CIO's request goes through ``RiskManager.approve_entry``
in ``mt5bot.risk`` (the same code v2 uses), and nothing in the team can outvote, tilt or
bench it. Its ``analyze`` is an early, cheap pre-check so obviously blocked entries do not
cost an advisor call; the authoritative check always runs again at the gate.

The Guardian is an INDEPENDENT code path: the bot calls ``Guardian.check`` from its own
housekeeping, not through the CIO, so it keeps watching even if the team fails. It can
freeze new entries (all team modes) or request the kill switch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ... import limits
from ..agent import Agent, AgentState, Opinion
from ..context import DecisionContext
from ..events import GuardianAlert, RiskDecision

KILL_AFTER_UNPROTECTED_CHECKS = 3


class RiskManagerAgent(Agent):
    id = "risk_manager"

    def analyze(self, ctx: DecisionContext) -> Opinion:
        a, m = ctx.account, ctx.market
        reasons: list[tuple[str, str]] = []
        max_spread = float(self.cfg.max_spread_points)
        if a.halted:
            reasons.append(("HALTED", a.halted))
        if m.spread_points > max_spread:
            reasons.append(("SPREAD_LIMIT", f"spread {m.spread_points:.1f} > max {max_spread:.1f} points"))
        if a.open_risk_pct + a.risk_pct > a.max_open_risk_pct + 1e-9:
            reasons.append(("OPEN_RISK_LIMIT", f"open risk {a.open_risk_pct:.2f}% + {a.risk_pct:.2f}% > "
                            f"{a.max_open_risk_pct:.2f}%"))
        if any(p.symbol == m.symbol for p in a.positions):
            reasons.append(("ONE_PER_SYMBOL", "already holding a position on this symbol"))
        gauges = {"daily_loss_used_pct_of_limit": round(a.daily_loss_used_pct_of_limit, 2),
                  "open_risk_pct": round(a.open_risk_pct, 3), "max_open_risk_pct": a.max_open_risk_pct,
                  "drawdown_pct": round(a.drawdown_pct, 3), "drawdown_limit_pct": a.drawdown_limit_pct,
                  "spread_points": round(m.spread_points, 2), "max_spread_points": max_spread,
                  "risk_pct": a.risk_pct}
        self.stats["gauges"] = gauges
        if reasons:
            self.set_state(AgentState.DECIDING, task="pré-verificação de risco",
                           message=f"Veto: {reasons[0][1]}", symbol=m.symbol, correlation_id=ctx.decision_id)
            return self.opinion(ctx, "veto", 1.0, reasons, size_hint=0.0, details=gauges)
        self.set_state(AgentState.MONITORING, task="limites dentro do orçamento",
                       message=f"Perda diária usada {a.daily_loss_used_pct_of_limit:.0f}% do limite, risco aberto "
                               f"{a.open_risk_pct:.2f}%")
        return self.opinion(ctx, "abstain", 0.0, [("WITHIN_LIMITS", "pre-check passed")], details=gauges)

    def record(self, allowed: bool, reason: str, symbol: str, correlation_id: str, stage: str = "gate") -> None:
        """Called with the authoritative result of the hard risk layer."""
        self.bus.publish(RiskDecision(allowed, reason, stage), agent=self.id, symbol=symbol,
                         correlation_id=correlation_id, severity="info" if allowed else "notice")
        if allowed:
            self.set_state(AgentState.DECIDING, task="aprovado", message=f"{symbol}: aprovado pela camada de risco",
                           symbol=symbol, correlation_id=correlation_id)
        else:
            self.set_state(AgentState.DECIDING, task="veto", message=f"{symbol}: VETO: {reason}", symbol=symbol,
                           correlation_id=correlation_id)


# ---------------------------------------------------------------------- Guardian


@dataclass(frozen=True)
class GuardianSnapshot:
    positions: tuple[dict, ...]          # bot positions: ticket, symbol, sl, tp, volume
    journal_open_tickets: frozenset
    equity: float
    open_risk_money: float
    max_total_open_risk_pct: float
    risk_pct: float
    max_positions: int
    entries_while_halted: int = 0


@dataclass
class GuardianReport:
    freeze_reason: Optional[str] = None
    kill_reason: Optional[str] = None
    violations: list[str] = field(default_factory=list)


class Guardian(Agent):
    id = "guardian"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self.frozen: Optional[str] = None
        self._clean_checks = 0
        self._unprotected_checks = 0

    def check(self, snap: GuardianSnapshot) -> GuardianReport:
        """Evaluate every invariant. Pure apart from the freeze/clear bookkeeping."""
        team = self.cfg.team
        report = GuardianReport()
        unprotected = [p for p in snap.positions if not p.get("sl") or not p.get("tp")]
        per_symbol: dict[str, int] = {}
        for p in snap.positions:
            per_symbol[p["symbol"]] = per_symbol.get(p["symbol"], 0) + 1
        if unprotected:
            self._unprotected_checks += 1
            report.violations.append(f"{len(unprotected)} bot position(s) without SL/TP after repair")
            if self._unprotected_checks >= KILL_AFTER_UNPROTECTED_CHECKS and team.guardian_kill_enabled:
                report.kill_reason = "a bot position stayed without SL/TP for several checks"
        else:
            self._unprotected_checks = 0
        if len(snap.positions) > min(snap.max_positions, limits.MAX_OPEN_POSITIONS) or \
                any(n > limits.MAX_POSITIONS_PER_SYMBOL for n in per_symbol.values()):
            report.violations.append(f"{len(snap.positions)} bot positions (per symbol {per_symbol}) exceed the "
                                     "hard ceilings")
            if team.guardian_kill_enabled:
                report.kill_reason = report.kill_reason or "position count beyond the hard ceilings"
        missing = [p["ticket"] for p in snap.positions if int(p["ticket"]) not in snap.journal_open_tickets]
        if missing:
            report.violations.append(f"broker positions not in the journal: {missing[:5]}")
        if snap.equity > 0 and snap.open_risk_money > snap.equity * snap.max_total_open_risk_pct / 100.0 * 1.10:
            report.violations.append(f"open risk {snap.open_risk_money / snap.equity * 100:.2f}% of equity > "
                                     f"{snap.max_total_open_risk_pct:.2f}% (+10% tolerance)")
        if snap.risk_pct > limits.MAX_RISK_PER_TRADE_PCT + 1e-12:
            report.violations.append(f"effective risk per trade {snap.risk_pct}% above the hard ceiling")
        if snap.entries_while_halted:
            report.violations.append(f"{snap.entries_while_halted} entry(ies) sent while trading was halted")
            if team.guardian_kill_enabled:
                report.kill_reason = report.kill_reason or "orders were sent while trading was halted"
        if report.violations:
            self._clean_checks = 0
            reason = "; ".join(report.violations)
            if self.frozen is None:
                self.bus.publish(GuardianAlert("invariants", reason, "kill" if report.kill_reason else "freeze"),
                                 agent=self.id, severity="critical" if report.kill_reason else "warning")
                if self.cfg.notifications.guardian:
                    self.services.notify(f"Guardian froze new entries: {reason}")
            self.frozen = reason
            self.set_state(AgentState.MONITORING, task="entradas congeladas", message=f"Congelado: {reason}"[:200])
        else:
            self._clean_checks += 1
            if self.frozen is not None and self._clean_checks >= team.guardian_freeze_clear_checks:
                self.bus.publish(GuardianAlert("invariants", "all invariants hold again", "clear"), agent=self.id,
                                 severity="notice")
                self.frozen = None
            if self.frozen is None:
                self.set_state(AgentState.MONITORING, task="invariantes ok",
                               message=f"Invariantes ok ({len(snap.positions)} posições protegidas)")
        report.freeze_reason = self.frozen
        if report.kill_reason:
            self.bus.publish(GuardianAlert("kill_switch", report.kill_reason, "kill"), agent=self.id,
                             severity="critical")
        self.stats.update(frozen=self.frozen, violations=list(report.violations),
                          unprotected_checks=self._unprotected_checks)
        return report

    def analyze(self, ctx: DecisionContext) -> Opinion:
        if self.frozen:
            return self.opinion(ctx, "veto", 1.0, [("GUARDIAN_FREEZE", self.frozen)], size_hint=0.0)
        return self.opinion(ctx, "abstain", 0.0, [("INVARIANTS_OK", "no freeze")])
