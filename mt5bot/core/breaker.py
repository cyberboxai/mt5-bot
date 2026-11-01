"""Broker circuit breaker: stops NEW entries while the broker connection is unreliable.

Only infrastructure failures count (no answer, timeouts, connection errors). A permanent
rejection or a blocked duplicate is the system working correctly, not an outage. The breaker
never blocks a close or a stop change: reducing risk is always allowed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class BreakerSettings:
    failure_threshold: int = 5
    open_seconds: float = 300.0
    half_open_probes: int = 1


@dataclass
class BrokerBreaker:
    state: str = CLOSED
    failures: int = 0
    opened_at: Optional[float] = None
    probes: int = 0

    def allow(self, now: float, settings: BreakerSettings) -> bool:
        """May a new entry be sent now? Counts a half-open probe when it says yes."""
        if self.state == OPEN and self.opened_at is not None and now - self.opened_at >= settings.open_seconds:
            self.state, self.probes = HALF_OPEN, 0
        if self.state == CLOSED:
            return True
        if self.state == HALF_OPEN and self.probes < settings.half_open_probes:
            self.probes += 1
            return True
        return False

    def on_success(self) -> None:
        self.state, self.failures, self.opened_at, self.probes = CLOSED, 0, None, 0

    def on_failure(self, now: float, settings: BreakerSettings) -> bool:
        """Record an infrastructure failure. Returns True when this failure opened the breaker."""
        self.failures += 1
        if self.state == HALF_OPEN or (self.state == CLOSED and self.failures >= settings.failure_threshold):
            self.state, self.opened_at, self.probes = OPEN, now, 0
            return True
        return False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: Optional[str]) -> "BrokerBreaker":
        if not text:
            return cls()
        try:
            data = json.loads(text)
            state = data["state"]
            if state not in (CLOSED, OPEN, HALF_OPEN):
                raise ValueError(state)
            opened = data.get("opened_at")
            return cls(state, int(data.get("failures", 0)), None if opened is None else float(opened),
                       int(data.get("probes", 0)))
        except (ValueError, TypeError, KeyError):
            # A corrupt state file must not silently re-enable entries: start half-open-safe.
            return cls(OPEN, 0, 0.0, 0)
