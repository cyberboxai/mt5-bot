"""Provider-agnostic advisor interface and the ``NullAdvisor``.

Every method returns a value and NEVER raises; an advisor that cannot answer returns an
"unavailable" object, and the decision pipeline then produces exactly the output it
produces with ``NullAdvisor``. The trading thread never waits longer than the latency
budget: implementations do their I/O on worker threads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

from .types import (AdvisorOpinion, AdvisorState, Briefing, ManagementSuggestion, ResearchNote, unavailable)


class AdvisorStore(Protocol):
    """Where an advisor audits its calls (implemented by ``mt5bot.officedb.OfficeStore``)."""

    def log_advisor_call(self, row: Mapping[str, Any]) -> Optional[int]: ...

    def log_advisor_state(self, ts: float, old: str, new: str, reason: str) -> None: ...

    def advisor_usage_since(self, calls_since: float, cost_since: float) -> tuple[list[float], float]: ...


class Advisor(ABC):
    name = "advisor"

    @abstractmethod
    def review_trade(self, features: Mapping[str, Any], untrusted: Sequence[str] = ()) -> AdvisorOpinion:
        """Pre-trade review within the latency budget."""

    def suggest_management(self, features: Mapping[str, Any]) -> ManagementSuggestion:
        return ManagementSuggestion(False, unavailable_reason="not supported")

    def request_briefing(self, features: Mapping[str, Any], untrusted: Sequence[str] = ()) -> bool:
        """Start a background daily briefing. Returns True if it was queued."""
        return False

    def request_research(self, kind: str, features: Mapping[str, Any]) -> bool:
        return False

    def request_explanation(self, dossier_id: str, features: Mapping[str, Any]) -> bool:
        return False

    def take_briefing(self) -> Optional[Briefing]:
        return None

    def take_research(self) -> Optional[ResearchNote]:
        return None

    def take_explanations(self) -> list[tuple[str, str]]:
        return []

    def tick(self) -> None:
        """Housekeeping from the trading loop: key re-check, budget window, probes. Never blocks."""

    def update_config(self, cfg: Any) -> None:
        """Hot reload of the ``claude`` block."""

    @property
    @abstractmethod
    def state(self) -> AdvisorState:
        ...

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state.value}

    def close(self) -> None:
        """Release worker threads."""


class NullAdvisor(Advisor):
    """Always unavailable. The reference behaviour every failure mode must reproduce."""

    name = "null"

    def __init__(self, reason: str = "advisor disabled") -> None:
        self.reason = reason

    def review_trade(self, features: Mapping[str, Any], untrusted: Iterable[str] = ()) -> AdvisorOpinion:
        return unavailable(self.reason)

    def suggest_management(self, features: Mapping[str, Any]) -> ManagementSuggestion:
        return ManagementSuggestion(False, unavailable_reason=self.reason)

    @property
    def state(self) -> AdvisorState:
        return AdvisorState.DISABLED

    def snapshot(self) -> dict[str, Any]:
        return {"state": AdvisorState.DISABLED.value, "reason": self.reason, "provider": "none"}


__all__ = ["Advisor", "AdvisorStore", "NullAdvisor", "Briefing", "ResearchNote"]
