"""Typed in-process event bus with ordered, bounded, asynchronous persistence.

* Messages are frozen dataclasses with a ``TOPIC``; the bus wraps each in an ``Event``
  envelope (sequence number, timestamp, agent, symbol, severity, correlation id).
* ``publish`` assigns the sequence under a lock and enqueues for the writer in the same
  critical section, so database ids follow publish order.
* Subscribers run synchronously on the publisher's thread, each isolated: a failing
  subscriber is counted and logged, never propagated.
* Persistence runs on one background thread in batches. The queue is bounded; when it is
  full, low-severity events are dropped (and counted) while warnings and above are
  written synchronously, so the panel never misses an alert and memory never grows.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Mapping, Optional, Union

log = logging.getLogger("mt5_bot")

SEVERITIES = ("debug", "info", "notice", "warning", "critical")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Event:
    topic: str
    ts: float
    seq: int = 0
    agent: Optional[str] = None
    symbol: Optional[str] = None
    severity: str = "info"
    correlation_id: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {"ts": self.ts, "topic": self.topic, "agent": self.agent, "symbol": self.symbol,
                "severity": self.severity, "correlation_id": self.correlation_id, "payload": dict(self.payload)}


# ----------------------------------------------------------------- typed messages


@dataclass(frozen=True)
class Message:
    TOPIC: ClassVar[str] = "message"
    SEVERITY: ClassVar[str] = "info"

    def payload(self) -> dict[str, Any]:
        # Shallow on purpose: asdict() deep-copies every nested value on the hot path.
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


@dataclass(frozen=True)
class AgentStateChanged(Message):
    TOPIC: ClassVar[str] = "agent.state"
    state: str
    task: str = ""
    message: str = ""


@dataclass(frozen=True)
class AgentSaid(Message):
    TOPIC: ClassVar[str] = "agent.message"
    text: str
    kind: str = "speech"  # speech | thought


@dataclass(frozen=True)
class AgentFailed(Message):
    TOPIC: ClassVar[str] = "agent.error"
    SEVERITY: ClassVar[str] = "warning"
    error: str
    failures: int
    slow: bool = False


@dataclass(frozen=True)
class AgentBenched(Message):
    TOPIC: ClassVar[str] = "agent.benched"
    SEVERITY: ClassVar[str] = "notice"
    reason: str
    kind: str  # errors | performance
    until: Optional[float] = None


@dataclass(frozen=True)
class AgentRestored(Message):
    TOPIC: ClassVar[str] = "agent.restored"
    SEVERITY: ClassVar[str] = "notice"
    reason: str


@dataclass(frozen=True)
class OpinionPublished(Message):
    TOPIC: ClassVar[str] = "opinion"
    opinion: Mapping[str, Any]


@dataclass(frozen=True)
class DecisionStarted(Message):
    TOPIC: ClassVar[str] = "decision.started"
    side: str
    strategy: str
    participants: tuple[str, ...]
    mode: str


@dataclass(frozen=True)
class DecisionFinished(Message):
    TOPIC: ClassVar[str] = "decision.finished"
    action: str  # enter | no_trade
    multiplier: float
    reasons: tuple[str, ...]
    mode: str
    side: str = ""
    counterfactual_multiplier: Optional[float] = None


@dataclass(frozen=True)
class RiskDecision(Message):
    TOPIC: ClassVar[str] = "risk.decision"
    allowed: bool
    reason: str
    stage: str = "gate"  # preview | gate


@dataclass(frozen=True)
class OrderExecuted(Message):
    TOPIC: ClassVar[str] = "order.executed"
    side: str
    volume: float
    price: float
    sl: float
    tp: float
    slippage_points: float
    dry_run: bool = False
    session: str = ""


@dataclass(frozen=True)
class OrderFailed(Message):
    TOPIC: ClassVar[str] = "order.failed"
    SEVERITY: ClassVar[str] = "warning"
    reason: str


@dataclass(frozen=True)
class TradeClosed(Message):
    TOPIC: ClassVar[str] = "trade.closed"
    mode: str
    r: float
    pnl: float
    win: bool
    reason: str = ""


@dataclass(frozen=True)
class PositionManaged(Message):
    TOPIC: ClassVar[str] = "position.managed"
    action: str
    detail: str
    applied: bool


@dataclass(frozen=True)
class MarketSnapshot(Message):
    TOPIC: ClassVar[str] = "market.snapshot"
    SEVERITY: ClassVar[str] = "debug"
    bid: float
    ask: float
    spread_points: float
    bar_time: int


@dataclass(frozen=True)
class RegimeChanged(Message):
    TOPIC: ClassVar[str] = "market.regime"
    regime: str
    probs: Mapping[str, float]


@dataclass(frozen=True)
class GuardianAlert(Message):
    TOPIC: ClassVar[str] = "guardian.alert"
    SEVERITY: ClassVar[str] = "warning"
    invariant: str
    detail: str
    action: str  # freeze | kill | clear


@dataclass(frozen=True)
class AdvisorStateChanged(Message):
    TOPIC: ClassVar[str] = "advisor.state"
    SEVERITY: ClassVar[str] = "notice"
    old: str
    new: str
    reason: str


@dataclass(frozen=True)
class AdvisorVerdict(Message):
    TOPIC: ClassVar[str] = "advisor.verdict"
    available: bool
    verdict: str
    multiplier: float
    reason: str = ""


@dataclass(frozen=True)
class TeamModeChanged(Message):
    TOPIC: ClassVar[str] = "team.mode"
    SEVERITY: ClassVar[str] = "notice"
    configured: str
    effective: str
    reason: str


@dataclass(frozen=True)
class ControlAction(Message):
    TOPIC: ClassVar[str] = "control.action"
    SEVERITY: ClassVar[str] = "warning"
    action: str
    source: str
    detail: str


@dataclass(frozen=True)
class ArmTierChanged(Message):
    TOPIC: ClassVar[str] = "arm.tier"
    SEVERITY: ClassVar[str] = "notice"
    arm_id: int
    arm: str
    old: str
    new: str
    reason: str
    p_positive: Optional[float] = None


@dataclass(frozen=True)
class FlowAdjusted(Message):
    TOPIC: ClassVar[str] = "flow.adjustment"
    SEVERITY: ClassVar[str] = "notice"
    knob: str
    old: float
    new: float
    direction: str
    reason: str


@dataclass(frozen=True)
class SystemHealth(Message):
    TOPIC: ClassVar[str] = "system.health"
    SEVERITY: ClassVar[str] = "debug"
    metrics: Mapping[str, Any]


@dataclass(frozen=True)
class Alert(Message):
    TOPIC: ClassVar[str] = "system.alert"
    SEVERITY: ClassVar[str] = "warning"
    text: str


# --------------------------------------------------------------------------- bus

Handler = Callable[[Event], None]


class EventBus:
    def __init__(self, store: Any = None, clock: Callable[[], float] = time.time, queue_size: int = 10_000,
                 batch_size: int = 200, flush_interval: float = 0.25, recent_size: int = 1000) -> None:
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._subs: dict[int, tuple[str, Handler]] = {}
        self._sub_ids = itertools.count(1)
        self._queue: "queue.Queue[Optional[Event]]" = queue.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self.recent: deque[Event] = deque(maxlen=recent_size)
        self.dropped = 0
        self.subscriber_errors = 0
        self.write_errors = 0
        self._writer: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._pending = 0
        self._done = threading.Condition()
        if store is not None:
            self._writer = threading.Thread(target=self._write_loop, name="event-writer", daemon=True)
            self._writer.start()

    # ---------------------------------------------------------------- subscribe

    def subscribe(self, topic: str, handler: Handler) -> int:
        """``topic`` is exact ("opinion"), a prefix ending in ".*" ("agent.*") or "*" (everything)."""
        token = next(self._sub_ids)
        with self._lock:
            self._subs[token] = (topic, handler)
        return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subs.pop(token, None)

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        if pattern == "*" or pattern == topic:
            return True
        return pattern.endswith(".*") and topic.startswith(pattern[:-1])

    # ------------------------------------------------------------------ publish

    def publish(self, message: Union[Message, Event], agent: Optional[str] = None, symbol: Optional[str] = None,
                severity: Optional[str] = None, correlation_id: Optional[str] = None) -> Event:
        """Stamp, persist (asynchronously) and deliver one event.

        The database and ``recent`` are in sequence order. Subscribers run in the publishing
        thread, outside the lock: each publisher's events reach them in order, but events
        from different threads may interleave out of sequence order."""
        if isinstance(message, Event):
            base = message
        else:
            base = Event(topic=message.TOPIC, ts=0.0, agent=agent, symbol=symbol,
                         severity=severity or message.SEVERITY, correlation_id=correlation_id,
                         payload=message.payload())
        if base.severity not in SEVERITY_RANK:
            base = dataclasses.replace(base, severity="info")
        with self._lock:
            event = dataclasses.replace(base, seq=next(self._seq), ts=base.ts or float(self._clock()))
            self.recent.append(event)
            subscribers = [h for pattern, h in self._subs.values() if self._matches(pattern, event.topic)]
            if self._writer is not None:
                self._enqueue(event)
        for handler in subscribers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - one subscriber must never break the publisher
                self.subscriber_errors += 1
                log.debug("event subscriber failed for %s", event.topic, exc_info=True)
        return event

    def _enqueue(self, event: Event) -> None:
        with self._done:
            self._pending += 1  # before put: the writer may finish the item before we return
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            self._written(1)
        if SEVERITY_RANK[event.severity] >= SEVERITY_RANK["warning"]:
            try:
                self._store.insert_events([event.row()])
            except Exception:  # noqa: BLE001
                self.write_errors += 1
        else:
            self.dropped += 1

    # ------------------------------------------------------------------- writer

    def _written(self, count: int) -> None:
        with self._done:
            self._pending = max(0, self._pending - count)
            if self._pending == 0:
                self._done.notify_all()

    def _write_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if first is None:
                return
            batch = [first]
            stop = False
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    break
                batch.append(item)
            try:
                self._store.insert_events([e.row() for e in batch])
            except Exception:  # noqa: BLE001 - a locked or full disk must not crash the trading loop
                self.write_errors += 1
                log.warning("Could not persist %d event(s) to the database", len(batch), exc_info=True)
            self._written(len(batch))
            if stop:
                return

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued event is written (tests, shutdown)."""
        if self._writer is None:
            return True
        with self._done:
            return self._done.wait_for(lambda: self._pending == 0, timeout)

    def close(self, timeout: float = 5.0) -> None:
        if self._writer is None:
            return
        self.flush(timeout)
        self._stopping.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._writer.join(timeout)
        self._writer = None

    def stats(self) -> dict[str, int]:
        return {"queued": self._queue.qsize(), "dropped": self.dropped, "subscriber_errors": self.subscriber_errors,
                "write_errors": self.write_errors}
