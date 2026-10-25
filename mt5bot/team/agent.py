"""Agent base class, the standard ``Opinion`` and the panel-visible agent state.

An agent is a deterministic specialist. ``analyze`` receives an immutable
``DecisionContext`` and returns an ``Opinion`` (or None). Agents never call the
MetaTrader 5 API and never send orders: the bot gathers data, the CIO decides, the
risk layer gates and the bot executes. ``analyze`` runs on a supervised worker thread,
so anything it shares with ``observe``/``tick`` must be safe for that.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from .events import AgentSaid, AgentStateChanged, Event, EventBus, OpinionPublished
from .roster import BY_ID, AgentInfo

if TYPE_CHECKING:  # pragma: no cover
    from .context import DecisionContext

STANCES = ("long", "short", "flat", "veto", "abstain")
MAX_TEXT = 240


class AgentState(str, Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    ANALYZING = "analyzing"
    DEBATING = "debating"
    DECIDING = "deciding"
    EXECUTING = "executing"
    MONITORING = "monitoring"
    REVIEWING = "reviewing"
    RESTING = "resting"
    ERROR = "error"


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


@dataclass(frozen=True)
class Opinion:
    agent: str
    symbol: str
    timeframe: str
    stance: str
    confidence: float
    size_hint: float = 1.0
    invalidations: tuple[str, ...] = ()
    reasons: tuple[tuple[str, str], ...] = ()
    features_used: tuple[str, ...] = ()
    timestamp: float = 0.0
    ttl: float = 0.0
    bar_time: Optional[int] = None
    raw_confidence: Optional[float] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stance not in STANCES:
            raise ValueError(f"{self.agent}: stance must be one of {STANCES}, got {self.stance!r}")
        for name in ("confidence", "size_hint"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not 0.0 <= value <= 1.0:
                raise ValueError(f"{self.agent}: {name} must be in [0, 1], got {value!r}")

    def expired(self, now: float) -> bool:
        return self.ttl > 0 and now > self.timestamp + self.ttl

    def directional(self) -> int:
        return 1 if self.stance == "long" else -1 if self.stance == "short" else 0

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "symbol": self.symbol, "timeframe": self.timeframe, "stance": self.stance,
                "confidence": round(self.confidence, 4), "size_hint": round(self.size_hint, 4),
                "raw_confidence": None if self.raw_confidence is None else round(self.raw_confidence, 4),
                "invalidations": list(self.invalidations), "reasons": [list(r) for r in self.reasons],
                "features_used": list(self.features_used), "timestamp": self.timestamp, "ttl": self.ttl,
                "bar_time": self.bar_time, "details": dict(self.details)}


@dataclass
class TeamServices:
    """What agents may use. Deliberately no MT5 handle and no order function."""

    bus: EventBus
    cfg: Any                        # the full Config (agents read their block)
    clock: Callable[[], float] = time.time
    registry: Any = None            # read access to the journal / champions
    store: Any = None               # OfficeStore
    advisor: Any = None
    notify: Callable[[str], None] = lambda text: None
    state_dir: Any = None


class Agent:
    """Base class. Subclasses set ``id`` (from the roster) and override what they need."""

    id: str = ""
    subscriptions: Sequence[str] = ()

    def __init__(self, services: TeamServices) -> None:
        if self.id not in BY_ID:
            raise ValueError(f"agent id {self.id!r} is not in the roster")
        self.services = services
        self.info: AgentInfo = BY_ID[self.id]
        self.state = AgentState.IDLE
        self.task = ""
        self.last_message = ""
        self.benched: Optional[str] = None  # set when benched for performance: works in shadow, rests visibly
        self._lock = threading.Lock()
        self.stats: dict[str, Any] = {}
        self._tokens = [services.bus.subscribe(topic, self._safe_observe) for topic in self.subscriptions]

    # ------------------------------------------------------------------ helpers

    @property
    def bus(self) -> EventBus:
        return self.services.bus

    @property
    def cfg(self) -> Any:
        return self.services.cfg

    def now(self) -> float:
        return float(self.services.clock())

    def set_state(self, state: AgentState, task: Optional[str] = None, message: Optional[str] = None,
                  symbol: Optional[str] = None, correlation_id: Optional[str] = None) -> None:
        if self.benched and state != AgentState.ERROR:
            state = AgentState.RESTING
            task = f"(sombra) {task}" if task else task
        with self._lock:
            changed = state != self.state or (task is not None and task != self.task)
            self.state = state
            if task is not None:
                self.task = _short(task)
            if message is not None:
                self.last_message = _short(message)
        if changed or message is not None:
            self.bus.publish(AgentStateChanged(state.value, self.task, self.last_message if message else ""),
                             agent=self.id, symbol=symbol, correlation_id=correlation_id)

    def say(self, text: str, symbol: Optional[str] = None, correlation_id: Optional[str] = None,
            kind: str = "speech", severity: Optional[str] = None) -> None:
        with self._lock:
            self.last_message = _short(text)
        self.bus.publish(AgentSaid(self.last_message, kind), agent=self.id, symbol=symbol,
                         correlation_id=correlation_id, severity=severity)

    def publish_opinion(self, opinion: Opinion, correlation_id: Optional[str] = None) -> None:
        summary = {"agent": opinion.agent, "symbol": opinion.symbol, "stance": opinion.stance,
                   "confidence": round(opinion.confidence, 4), "size_hint": round(opinion.size_hint, 4),
                   "reasons": [list(r) for r in opinion.reasons[:3]], "bar_time": opinion.bar_time}
        self.bus.publish(OpinionPublished(summary), agent=self.id, symbol=opinion.symbol,
                         correlation_id=correlation_id)

    def opinion(self, ctx: "DecisionContext", stance: str, confidence: float, reasons: Sequence[tuple[str, str]] = (),
                size_hint: float = 1.0, features: Sequence[str] = (), invalidations: Sequence[str] = (),
                details: Optional[Mapping[str, Any]] = None, ttl_bars: float = 1.0) -> Opinion:
        """Build an Opinion with the context's symbol, bar and a TTL of ``ttl_bars`` bars."""
        m = ctx.market
        return Opinion(self.id, m.symbol, m.timeframe, stance, float(min(max(confidence, 0.0), 1.0)),
                       float(min(max(size_hint, 0.0), 1.0)), tuple(invalidations),
                       tuple((c, _short(t)) for c, t in reasons), tuple(features), self.now(),
                       ttl_bars * m.bar_seconds, m.bar_time, None, dict(details or {}))

    def _safe_observe(self, event: Event) -> None:
        if event.agent == self.id:
            return
        self.observe(event)

    # ------------------------------------------------------------- overridable

    def observe(self, event: Event) -> None:
        """React to bus events (keep it cheap and thread safe)."""

    def analyze(self, ctx: "DecisionContext") -> Optional[Opinion]:
        """Return an opinion for this decision, or None."""
        return None

    def tick(self, now: float) -> None:
        """Periodic housekeeping (once per loop at most)."""

    def reset(self) -> None:
        """Restart after being benched for errors: drop transient state."""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self.id, "state": self.state.value, "task": self.task, "last_message": self.last_message,
                    "benched": self.benched, "stats": dict(self.stats)}

    def close(self) -> None:
        for token in self._tokens:
            self.bus.unsubscribe(token)
        self._tokens = []
