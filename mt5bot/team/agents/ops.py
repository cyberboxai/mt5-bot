"""Operations and Leadership: the Ops Engineer and the Claude Strategist.

The Ops Engineer watches the MT5 connection, loop timings, CPU, memory, disk, log and
database sizes, the panel heartbeat and the advisor, using only the standard library.

The Claude Strategist is the ONLY path to the advisor. When the advisor is not ONLINE
it shows as "away" and the decision protocol continues exactly as without it.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ...db import DB_NAME
from ...advisor.types import AdvisorOpinion, AdvisorState, ManagementSuggestion, unavailable
from ..agent import Agent, AgentState
from ..events import AdvisorStateChanged, AdvisorVerdict, Event, SystemHealth

METRICS_EVERY_SECONDS = 60.0
PANEL_HEARTBEAT = "panel_heartbeat.json"
VERDICT_PT = {"confirm": "confirmar", "reduce": "reduzir", "veto": "vetar"}  # office speech is PT-PT


def process_memory_mb() -> Optional[float]:
    """Resident memory of this process, without third-party packages."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm", encoding="ascii") as handle:
                pages = int(handle.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE") / 1e6
        if os.name == "nt":  # pragma: no cover - Windows only
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / 1e6
    except (OSError, ValueError, AttributeError):
        return None
    return None


def file_size_mb(path: Path, rotated: bool = False) -> float:
    total = 0
    candidates = [path] + ([path.with_name(f"{path.name}.{i}") for i in range(1, 10)] if rotated else [])
    for p in candidates:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total / 1e6


class OpsEngineer(Agent):
    id = "ops_engineer"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._last = -math.inf
        self._cpu = (time.process_time(), time.monotonic())
        self.bot_status: dict[str, Any] = {}

    def update_bot_status(self, status: Mapping[str, Any]) -> None:
        self.bot_status = dict(status)

    def tick(self, now: float) -> None:
        if now - self._last < METRICS_EVERY_SECONDS:
            return
        self._last = now
        cpu_now = (time.process_time(), time.monotonic())
        wall = cpu_now[1] - self._cpu[1]
        cpu_pct = 100.0 * (cpu_now[0] - self._cpu[0]) / wall if wall > 0 else 0.0
        self._cpu = cpu_now
        state_dir = Path(self.services.state_dir) if self.services.state_dir else Path(".")
        metrics: dict[str, Any] = {"cpu_pct": round(cpu_pct, 2)}
        mem = process_memory_mb()
        if mem is not None:
            metrics["rss_mb"] = round(mem, 1)
        try:
            usage = shutil.disk_usage(state_dir)
            metrics["disk_free_gb"] = round(usage.free / 1e9, 2)
        except OSError:
            pass
        metrics["log_mb"] = round(file_size_mb(Path(self.cfg.log_file), rotated=True), 2)
        metrics["db_mb"] = round(file_size_mb(state_dir / DB_NAME) + file_size_mb(state_dir / f"{DB_NAME}-wal"), 2)
        heartbeat = state_dir / PANEL_HEARTBEAT
        try:
            metrics["panel_heartbeat_age_s"] = round(time.time() - heartbeat.stat().st_mtime, 1)
        except OSError:
            metrics["panel_heartbeat_age_s"] = None
        for key in ("loop_ms", "mt5_latency_ms", "tick_age_s", "connected", "open_positions"):
            if key in self.bot_status:
                metrics[key] = self.bot_status[key]
        metrics["event_bus"] = self.bus.stats()
        advisor = self.services.advisor
        if advisor is not None:
            metrics["advisor_state"] = advisor.state.value
        self.stats["metrics"] = metrics
        if self.services.store is not None:
            numeric = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            try:
                self.services.store.add_metrics(numeric)
            except Exception:  # noqa: BLE001 - metrics must never break the loop
                pass
        self.bus.publish(SystemHealth(metrics), agent=self.id)
        panel = metrics.get("panel_heartbeat_age_s")
        panel_text = "painel ligado" if panel is not None and panel < 60 else "painel desligado"
        connected = self.bot_status.get("connected", True)
        self.set_state(AgentState.MONITORING, task="saúde do sistema",
                       message=f"MT5 {'ligado' if connected else 'DESLIGADO'}, CPU {cpu_pct:.0f}%, "
                               f"{metrics.get('rss_mb', '?')} MB, {panel_text}")


class ClaudeStrategist(Agent):
    id = "claude_strategist"
    subscriptions = ("trade.closed",)
    BRIEFING_EVERY_SECONDS = 20 * 3600.0
    RESEARCH_EVERY_SECONDS = 7 * 86400.0

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._state: Optional[AdvisorState] = None
        self._last_briefing = -math.inf
        self._last_research = -math.inf
        self._closed_live: list[dict] = []
        self.journal_summary: dict[str, float] = {}
        self.briefing_features: dict[str, Any] = {}

    @property
    def advisor(self) -> Any:
        return self.services.advisor

    def away(self) -> bool:
        return self.advisor is None or self.advisor.state != AdvisorState.ONLINE

    def observe(self, event: Event) -> None:
        if event.payload.get("mode") == "live":
            self._closed_live.append({"symbol": event.symbol, "r": event.payload.get("r")})

    def review(self, features: Mapping[str, Any], headlines: Sequence[str], symbol: str,
               correlation_id: str) -> AdvisorOpinion:
        if self.advisor is None:
            return unavailable("no advisor")
        if self.away():
            self.set_state(AgentState.IDLE, task=f"ausente ({self.advisor.state.value})",
                           message="Conselheiro ausente; a equipa decide sem ele.", symbol=symbol,
                           correlation_id=correlation_id)
            opinion = self.advisor.review_trade(features, headlines)  # returns at once: unavailable
        else:
            self.set_state(AgentState.DEBATING, task=f"{symbol}: revisão pré-trade",
                           message=f"{symbol}: a pedir opinião ao conselheiro", symbol=symbol,
                           correlation_id=correlation_id)
            opinion = self.advisor.review_trade(features, headlines)
        if opinion.available:
            text = (f"{symbol}: conselheiro diz {VERDICT_PT.get(opinion.verdict, opinion.verdict)} "
                    f"(x{opinion.effective_multiplier:.2f})")
        else:
            text = f"{symbol}: conselheiro indisponível ({opinion.unavailable_reason})"
        self.bus.publish(AdvisorVerdict(opinion.available, opinion.verdict if opinion.available else "unavailable",
                                        opinion.effective_multiplier, opinion.unavailable_reason), agent=self.id,
                         symbol=symbol, correlation_id=correlation_id)
        self.say(text, symbol=symbol, correlation_id=correlation_id)
        return opinion

    def manage(self, features: Mapping[str, Any]) -> ManagementSuggestion:
        if self.advisor is None or self.away():
            return ManagementSuggestion(False, unavailable_reason="advisor away")
        return self.advisor.suggest_management(features)

    def tick(self, now: float) -> None:
        advisor = self.advisor
        if advisor is None:
            return
        advisor.tick()
        state = advisor.state
        if state != self._state:
            old = self._state.value if self._state else "START"
            self._state = state
            snapshot = advisor.snapshot()
            self.bus.publish(AdvisorStateChanged(old, state.value, str(snapshot.get("reason", ""))), agent=self.id)
            if state == AdvisorState.ONLINE:
                self.set_state(AgentState.IDLE, task="disponível", message="Conselheiro Claude disponível.")
            else:
                self.set_state(AgentState.IDLE, task=f"ausente ({state.value})",
                               message=f"Conselheiro ausente: {snapshot.get('reason', state.value)}")
        self.stats["advisor"] = advisor.snapshot()
        if state != AdvisorState.ONLINE:
            return
        store = self.services.store
        if now - self._last_briefing >= self.BRIEFING_EVERY_SECONDS and self.briefing_features:
            if advisor.request_briefing(self.briefing_features):
                self._last_briefing = now
        if self._closed_live:
            self._closed_live = []
            advisor.request_research("post_trade", {"journal": self.journal_summary, "purpose": "post_trade"})
        elif now - self._last_research >= self.RESEARCH_EVERY_SECONDS and self.journal_summary:
            if advisor.request_research("weekly", {"journal": self.journal_summary, "purpose": "weekly"}):
                self._last_research = now
        briefing = advisor.take_briefing()
        if briefing is not None and store is not None:
            store.add_briefing("daily", {"regime_view": briefing.regime_view, "narrative": briefing.narrative,
                                         "tilts": {t.agent: t.factor for t in briefing.tilts},
                                         "risk_flags": list(briefing.risk_flags), "label": "AI commentary"})
            store.set_tilts(briefing.tilts)
            self.say(f"Briefing diário: {briefing.regime_view}"[:200])
        note = advisor.take_research()
        if note is not None and store is not None:
            store.add_briefing("research", {"lesson": note.lesson, "label": "AI commentary",
                                            "hypotheses": [{"strategy": h.strategy, "param": h.param,
                                                            "value": h.value, "rationale": h.rationale}
                                                           for h in note.hypotheses]})
            for h in note.hypotheses:
                store.add_hypothesis("advisor", h.strategy, h.param, h.value, h.rationale)
        if store is not None:
            for dossier_id, text in advisor.take_explanations():
                store.update_dossier(dossier_id, explanation=text)


def journal_summary(rows: Sequence[Any]) -> dict[str, float]:
    rs = [float(r.r) for r in rows if getattr(r, "r", None) is not None]
    if not rs:
        return {"trades": 0.0}
    wins = [r for r in rs if r > 0]
    return {"trades": float(len(rs)), "win_rate": len(wins) / len(rs), "expectancy_r": sum(rs) / len(rs),
            "total_r": sum(rs), "worst_r": min(rs), "best_r": max(rs)}


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
