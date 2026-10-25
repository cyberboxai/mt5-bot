"""``AdvisorHealth``: the resilience state machine of the optional Claude advisor.

========================  ==================================================================
DISABLED                  ``claude.enabled`` is false (or the SDK is missing): never call.
NO_KEY                    enabled but no key: one info line, re-check the key periodically.
ONLINE                    calls allowed within the local budget.
DEGRADED                  transient failures: exponential backoff with jitter, then a probe.
OFFLINE_NO_CREDIT         sticky; probed rarely (30 min doubling to 6 h); notified once.
OFFLINE_AUTH              sticky; same probing; notified once; never hammers the API.
BUDGET_CAPPED             local hourly call cap or daily cost cap reached; resumes at the
                          next window without any probe.
========================  ==================================================================

Only ONLINE allows decision-time calls. Every other state answers "unavailable" at once,
so an outage can only ever make the pipeline behave as if the advisor did not exist.
Thread safe: worker threads report outcomes while the trading thread reads the state.
"""

from __future__ import annotations

import random
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .types import CONFIG_KINDS, TRANSIENT_KINDS, AdvisorState, ErrorKind

OFFLINE_STATES = frozenset({AdvisorState.OFFLINE_NO_CREDIT, AdvisorState.OFFLINE_AUTH})
PROBING_STATES = frozenset({AdvisorState.DEGRADED, AdvisorState.OFFLINE_NO_CREDIT, AdvisorState.OFFLINE_AUTH})

Transition = Callable[[AdvisorState, AdvisorState, str], None]


@dataclass(frozen=True)
class HealthSettings:
    degraded_base_seconds: float = 30.0
    degraded_max_seconds: float = 900.0
    offline_base_seconds: float = 1800.0
    offline_max_seconds: float = 21600.0
    key_recheck_seconds: float = 300.0
    invalid_response_limit: int = 3

    @classmethod
    def from_config(cls, cfg: Any) -> "HealthSettings":
        return cls(cfg.probe_degraded_base_seconds, cfg.probe_degraded_max_seconds, cfg.probe_offline_seconds,
                   cfg.probe_offline_max_seconds, cfg.key_recheck_seconds)


class AdvisorHealth:
    def __init__(self, settings: HealthSettings = HealthSettings(), clock: Callable[[], float] = time.time,
                 rng: Optional[random.Random] = None, on_transition: Optional[Transition] = None) -> None:
        self.settings = settings
        self._clock = clock
        self._rng = rng or random.Random()
        self._on_transition = on_transition
        self._lock = threading.RLock()
        self.state = AdvisorState.DISABLED
        self.reason = "claude.enabled is false"
        self.since = clock()
        self.next_probe_at: Optional[float] = None
        self.next_key_check_at: Optional[float] = None
        self.budget_resume_at: Optional[float] = None
        self.consecutive_failures = 0
        self.consecutive_invalid = 0
        self.offline_interval = settings.offline_base_seconds
        self.last_error: Optional[ErrorKind] = None
        self.error_counts: Counter[str] = Counter()
        self.history: deque[dict] = deque(maxlen=200)
        self._key_fingerprint: Optional[str] = None
        self._probe_in_flight = False

    # ------------------------------------------------------------------ helpers

    def _set(self, state: AdvisorState, reason: str) -> None:
        old = self.state
        self.reason = reason
        if state == old:
            return
        self.state = state
        self.since = self._clock()
        self.history.append({"ts": self.since, "from": old.value, "to": state.value, "reason": reason})
        if self._on_transition is not None:
            try:
                self._on_transition(old, state, reason)
            except Exception:  # noqa: BLE001 - observers must never break the state machine
                pass

    def _jittered(self, seconds: float) -> float:
        return seconds * (0.5 + 0.5 * self._rng.random())

    # ---------------------------------------------------------------- configure

    def configure(self, enabled: bool, key_fingerprint: Optional[str], sdk_available: bool = True) -> None:
        """Apply config and key presence. A NEW key clears a sticky auth/credit state."""
        with self._lock:
            now = self._clock()
            if not enabled:
                self._key_fingerprint = key_fingerprint
                self._set(AdvisorState.DISABLED, "claude.enabled is false")
                return
            if not sdk_available:
                self._set(AdvisorState.DISABLED, "the optional 'anthropic' package is not installed")
                return
            if not key_fingerprint:
                self._key_fingerprint = None
                self.next_key_check_at = now + self.settings.key_recheck_seconds
                self._set(AdvisorState.NO_KEY, "ANTHROPIC_API_KEY is not set")
                return
            changed = key_fingerprint != self._key_fingerprint
            self._key_fingerprint = key_fingerprint
            if self.state in (AdvisorState.DISABLED, AdvisorState.NO_KEY) or (changed and self.state in OFFLINE_STATES):
                self._reset_failures()
                self._set(AdvisorState.ONLINE, "key present" if not changed else "new key loaded")

    def _reset_failures(self) -> None:
        self.consecutive_failures = 0
        self.consecutive_invalid = 0
        self.offline_interval = self.settings.offline_base_seconds
        self.next_probe_at = None

    # -------------------------------------------------------------------- queries

    def can_call(self) -> bool:
        with self._lock:
            self._maybe_resume_budget()
            return self.state == AdvisorState.ONLINE

    def probe_due(self) -> bool:
        with self._lock:
            return (self.state in PROBING_STATES and not self._probe_in_flight and self.next_probe_at is not None
                    and self._clock() >= self.next_probe_at)

    def key_recheck_due(self) -> bool:
        with self._lock:
            return (self.state == AdvisorState.NO_KEY and self.next_key_check_at is not None
                    and self._clock() >= self.next_key_check_at)

    def mark_key_checked(self) -> None:
        with self._lock:
            self.next_key_check_at = self._clock() + self.settings.key_recheck_seconds

    def begin_probe(self) -> bool:
        """Claim the single probe slot. Returns False if a probe is not due or already running."""
        with self._lock:
            if not self.probe_due():
                return False
            self._probe_in_flight = True
            return True

    def end_probe(self) -> None:
        with self._lock:
            self._probe_in_flight = False

    # ------------------------------------------------------------------- outcomes

    def record_success(self) -> None:
        with self._lock:
            self._reset_failures()
            self.last_error = None
            if self.state in PROBING_STATES:
                self._set(AdvisorState.ONLINE, "probe succeeded")

    def record_invalid_response(self, detail: str = "") -> None:
        """The API answered but the content did not validate. Repeated garbage costs money: back off."""
        with self._lock:
            self.error_counts[ErrorKind.INVALID_RESPONSE.value] += 1
            self.consecutive_invalid += 1
            if self.consecutive_invalid >= self.settings.invalid_response_limit and self.state == AdvisorState.ONLINE:
                self._enter_degraded(f"{self.consecutive_invalid} invalid responses in a row {detail}".strip(), None)

    def record_failure(self, kind: ErrorKind, detail: str = "", retry_after: Optional[float] = None) -> None:
        with self._lock:
            self.error_counts[kind.value] += 1
            self.last_error = kind
            now = self._clock()
            if self.state in (AdvisorState.DISABLED, AdvisorState.NO_KEY, AdvisorState.BUDGET_CAPPED):
                return  # a late result of a call started before the advisor was switched off or capped
            if kind == ErrorKind.NO_CREDIT:
                self._enter_offline(AdvisorState.OFFLINE_NO_CREDIT, "credit balance too low or billing problem")
            elif kind in (ErrorKind.AUTH, ErrorKind.PERMISSION):
                self._enter_offline(AdvisorState.OFFLINE_AUTH,
                                    "API key invalid or revoked (401)" if kind == ErrorKind.AUTH
                                    else "API key not permitted (403)")
            elif self.state in OFFLINE_STATES:
                # Sticky: a transient error while probing keeps the offline state and its slow cadence.
                self.next_probe_at = now + self.offline_interval
            elif kind in CONFIG_KINDS:
                self.consecutive_failures += 1
                self._enter_degraded(f"request rejected ({kind.value}): check claude.model_* in config.json {detail}"
                                     .strip(), self.settings.degraded_max_seconds)
            else:
                self.consecutive_failures += 1
                delay = min(self.settings.degraded_base_seconds * 2 ** (self.consecutive_failures - 1),
                            self.settings.degraded_max_seconds)
                delay = self._jittered(delay)
                if retry_after is not None:
                    delay = max(delay, min(retry_after, self.settings.degraded_max_seconds))
                self._enter_degraded(f"transient failure ({kind.value}) {detail}".strip(), delay)

    def _enter_degraded(self, reason: str, delay: Optional[float]) -> None:
        delay = self.settings.degraded_base_seconds if delay is None else delay
        self.next_probe_at = self._clock() + delay
        self._set(AdvisorState.DEGRADED, reason)

    def _enter_offline(self, state: AdvisorState, reason: str) -> None:
        if self.state == state:
            self.offline_interval = min(self.offline_interval * 2, self.settings.offline_max_seconds)
        else:
            self.offline_interval = self.settings.offline_base_seconds
        self.next_probe_at = self._clock() + self.offline_interval
        self._set(state, reason)

    # --------------------------------------------------------------------- budget

    def budget_capped(self, resume_at: float, reason: str) -> None:
        with self._lock:
            if self.state != AdvisorState.ONLINE:
                return
            self.budget_resume_at = resume_at
            self._set(AdvisorState.BUDGET_CAPPED, reason)

    def _maybe_resume_budget(self) -> None:
        if self.state == AdvisorState.BUDGET_CAPPED and self.budget_resume_at is not None \
                and self._clock() >= self.budget_resume_at:
            self.budget_resume_at = None
            self._set(AdvisorState.ONLINE, "budget window reset")

    def tick(self) -> None:
        with self._lock:
            self._maybe_resume_budget()

    # ------------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._maybe_resume_budget()
            return {
                "state": self.state.value,
                "reason": self.reason,
                "since": self.since,
                "next_probe_at": self.next_probe_at if self.state in PROBING_STATES else None,
                "budget_resume_at": self.budget_resume_at,
                "consecutive_failures": self.consecutive_failures,
                "last_error": self.last_error.value if self.last_error else None,
                "error_counts": dict(self.error_counts),
                "history": list(self.history)[-50:],
            }


__all__ = ["AdvisorHealth", "HealthSettings", "OFFLINE_STATES", "PROBING_STATES", "TRANSIENT_KINDS"]
