"""Helpers for the team tests: a deterministic decision context, a recording gateway and a
runtime factory with an isolated office database."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Optional

import numpy as np

from conftest import synthetic_bars
from mt5bot.config import Config
from mt5bot.officedb import OfficeStore
from mt5bot.team.agent import Opinion
from mt5bot.team.agents.cio import ExecutionResult, GateResult
from mt5bot.team.context import AccountView, DecisionContext, MarketView, Proposal, read_only
from mt5bot.team.runtime import TeamRuntime

UTC_NOW = 1_700_400_000.0


def bars(n: int = 700, seed: int = 3, trend: float = 0.0) -> np.ndarray:
    out = synthetic_bars(n, seed=seed, bar_seconds=900, trend=trend, start_time=int(UTC_NOW) + 7200 - n * 900)
    return out


def context(closed: Optional[np.ndarray] = None, side: str = "BUY", strategy: str = "donchian",
            spread: float = 9.0, positions: tuple = (), halted: Optional[str] = None, tick_age: float = 0.0,
            params: Optional[dict] = None, decision_id: str = "EURUSD-test") -> DecisionContext:
    closed = bars() if closed is None else closed
    last = float(closed["close"][-1])
    point = 0.00001
    market = MarketView("EURUSD", "M15", 900, int(closed["time"][-1]), read_only(closed), last, last + spread * point,
                        point, 5, spread, int(closed["time"][-1]) + 900 + 60, UTC_NOW, 7200, "EUR", "USD", tick_age)
    account = AccountView(False, 0.5, 0.0, 10.0, 1.0, 12.0, sum(p.risk_pct for p in positions), 2.0, halted,
                          positions)
    proposal = Proposal(side, strategy, 7, f"{strategy}(test)", 0.0012, 2.0, 3.0, 0.12, 0.45,
                        params if params is not None else {"channel": 40, "sl_mult": 2.0, "tp_mult": 3.0})
    return DecisionContext(decision_id, market, account, proposal)


class RecordingGateway:
    def __init__(self, allowed: bool = True, submit_allowed: Optional[bool] = None, reason: str = "ok") -> None:
        self.allowed = allowed
        self.submit_allowed = allowed if submit_allowed is None else submit_allowed
        self.reason = reason
        self.previews: list[float] = []
        self.submits: list[float] = []

    def preview(self, multiplier: float) -> GateResult:
        self.previews.append(multiplier)
        return GateResult(self.allowed, self.reason if not self.allowed else "ok", 0.1 * multiplier, 0.1)

    def submit(self, multiplier: float, dossier_id: str) -> ExecutionResult:
        self.submits.append(multiplier)
        if not self.submit_allowed:
            return ExecutionResult(False, False, f"entry rejected by the risk layer: {self.reason}",
                                   risk_allowed=False, risk_reason=self.reason)
        return ExecutionResult(True, True, "opened", 0.1 * multiplier, 1.1, 1.09, 1.12, 0.0, 1234, False, True, "ok",
                               0.1)


def team_config(mode: str = "full", **team: Any) -> Config:
    values = {"team_mode": mode}
    values.update(team)
    return Config.from_dict({"team": values, "trading_hours_server": [0, 24]})


def runtime(tmp_path: Path, cfg: Optional[Config] = None, advisor: Any = None, registry: Any = None,
            name: str = "office.sqlite") -> TeamRuntime:
    cfg = cfg or team_config()
    store = OfficeStore(tmp_path / name, clock=lambda: UTC_NOW)
    return TeamRuntime(cfg, tmp_path, registry, advisor, clock=lambda: UTC_NOW, store=store)


def fixed(agent_id: str, stance: str, confidence: float, size_hint: float = 1.0, details: Optional[dict] = None):
    """An ``analyze`` replacement returning a fixed opinion for the context's symbol and bar."""

    def analyze(ctx: DecisionContext) -> Opinion:
        return Opinion(agent_id, ctx.market.symbol, ctx.market.timeframe, stance, confidence, size_hint,
                       timestamp=ctx.market.utc_now, ttl=900, bar_time=ctx.market.bar_time,
                       details=dict(details or {"healthy": True}))

    return analyze


def pin_votes(rt: TeamRuntime, **stances: tuple) -> None:
    """Replace desk/meta analyze() with fixed opinions: name=(stance, confidence[, details])."""
    for agent_id, spec in stances.items():
        stance, conf = spec[0], spec[1]
        details = spec[2] if len(spec) > 2 else {}
        rt.agents[agent_id].analyze = fixed(agent_id, stance, conf, details=details)  # type: ignore[method-assign]


def decision_signature(decision: Any, gateway: RecordingGateway) -> tuple:
    return (decision.action, round(decision.multiplier, 9), tuple(round(x, 9) for x in gateway.submits))


def replace_cfg(cfg: Config, **changes: Any) -> Config:
    return dataclasses.replace(cfg, **changes)
