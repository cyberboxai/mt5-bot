"""Supervision tree for agents: per-agent time budgets, isolation, benching and restart.

* Each call runs on a worker thread with a deadline. A slow agent is reported "slow"
  and its late answer is discarded; it is not called again until that call returns, so
  a stuck agent costs at most one thread and never blocks the trading loop.
* An exception is isolated, logged and counted. ``max_failures`` failures within the
  window bench the agent (state ``resting``) for a backoff that doubles on every
  repeat (capped at one hour); after the backoff it is reset and restored.
* Agents in ``NEVER_BENCHED`` (Risk Manager, Guardian, Data Clerk, Ops) are never
  benched: they show ``error`` and stay callable, and their callers fail CLOSED.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .agent import Agent, AgentState
from .events import AgentBenched, AgentFailed, AgentRestored, EventBus
from .roster import NEVER_BENCHED

log = logging.getLogger("mt5_bot")

MAX_BACKOFF_SECONDS = 3600.0


@dataclass
class CallOutcome:
    ok: bool
    value: Any = None
    error: str = ""
    slow: bool = False
    busy: bool = False
    skipped: bool = False
    elapsed_ms: float = 0.0


@dataclass
class AgentHealth:
    failures: deque = field(default_factory=deque)
    total_failures: int = 0
    slow_calls: int = 0
    calls: int = 0
    benched_until: Optional[float] = None
    bench_count: int = 0
    inflight: Optional[Future] = None
    last_ms: float = 0.0
    last_error: str = ""

    def to_dict(self, now: float) -> dict[str, Any]:
        return {"calls": self.calls, "failures": self.total_failures, "slow_calls": self.slow_calls,
                "benched_for_errors": self.benched_until is not None and now < self.benched_until,
                "benched_until_in": None if self.benched_until is None else max(0.0, self.benched_until - now),
                "bench_count": self.bench_count, "last_ms": round(self.last_ms, 2), "last_error": self.last_error}


class AgentSupervisor:
    def __init__(self, bus: EventBus, timeout_ms: int, max_failures: int, window_seconds: float,
                 backoff_seconds: float, monotonic: Callable[[], float] = time.monotonic, workers: int = 4) -> None:
        self.bus = bus
        self.timeout_ms = timeout_ms
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.backoff_seconds = backoff_seconds
        self._monotonic = monotonic
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent")
        self.agents: dict[str, Agent] = {}
        self.health: dict[str, AgentHealth] = {}

    def register(self, agent: Agent) -> Agent:
        self.agents[agent.id] = agent
        self.health[agent.id] = AgentHealth()
        return agent

    def update_settings(self, timeout_ms: int, max_failures: int, window_seconds: float, backoff_seconds: float) -> None:
        self.timeout_ms, self.max_failures = timeout_ms, max_failures
        self.window_seconds, self.backoff_seconds = window_seconds, backoff_seconds

    # ------------------------------------------------------------------ status

    def benched_for_errors(self, agent_id: str) -> bool:
        h = self.health.get(agent_id)
        return h is not None and h.benched_until is not None and self._monotonic() < h.benched_until

    def available(self, agent_id: str) -> bool:
        h = self.health[agent_id]
        if h.benched_until is None:
            return True
        if self._monotonic() < h.benched_until:
            return False
        agent = self.agents[agent_id]
        h.benched_until = None
        h.failures.clear()
        try:
            agent.reset()
        except Exception:  # noqa: BLE001
            log.debug("%s reset failed", agent_id, exc_info=True)
        agent.set_state(AgentState.IDLE, task="regressou do banco", message="De volta ao trabalho após reinício.")
        self.bus.publish(AgentRestored("restart after error backoff"), agent=agent_id)
        return True

    # ------------------------------------------------------------------- calls

    def call_many(self, agent_ids: Iterable[str], method: str, *args: Any,
                  timeout_ms: Optional[int] = None) -> dict[str, CallOutcome]:
        """Call ``agent.<method>(*args)`` on every available agent in parallel, bounded by the deadline."""
        timeout = (timeout_ms or self.timeout_ms) / 1000.0
        started = self._monotonic()
        futures: dict[str, Future] = {}
        out: dict[str, CallOutcome] = {}
        for agent_id in agent_ids:
            agent = self.agents.get(agent_id)
            if agent is None:
                continue
            h = self.health[agent_id]
            if not self.available(agent_id):
                out[agent_id] = CallOutcome(False, skipped=True, error="benched after repeated errors")
                continue
            if h.inflight is not None and not h.inflight.done():
                out[agent_id] = CallOutcome(False, busy=True, error="previous call still running")
                continue
            h.calls += 1
            try:
                fut = self._pool.submit(self._timed, getattr(agent, method), *args)
            except RuntimeError:
                out[agent_id] = CallOutcome(False, skipped=True, error="supervisor closed")
                continue
            h.inflight = fut
            futures[agent_id] = fut
        if futures:
            wait_futures(list(futures.values()), timeout=timeout)
        for agent_id, fut in futures.items():
            h = self.health[agent_id]
            if not fut.done():
                h.slow_calls += 1
                out[agent_id] = CallOutcome(False, slow=True, error=f"exceeded {timeout * 1000:.0f} ms time budget",
                                            elapsed_ms=(self._monotonic() - started) * 1000)
                self._failure(agent_id, out[agent_id].error, slow=True)
                continue
            exc = fut.exception()
            if exc is not None:
                out[agent_id] = CallOutcome(False, error=f"{type(exc).__name__}: {exc}"[:300])
                self._failure(agent_id, out[agent_id].error)
                continue
            value, elapsed = fut.result()
            h.last_ms = elapsed
            out[agent_id] = CallOutcome(True, value=value, elapsed_ms=elapsed)
        return out

    def call(self, agent_id: str, method: str, *args: Any, timeout_ms: Optional[int] = None) -> CallOutcome:
        return self.call_many([agent_id], method, *args, timeout_ms=timeout_ms).get(
            agent_id, CallOutcome(False, skipped=True, error="unknown agent"))

    def _timed(self, fn: Callable[..., Any], *args: Any) -> tuple[Any, float]:
        started = time.perf_counter()
        value = fn(*args)
        return value, (time.perf_counter() - started) * 1000.0

    def _failure(self, agent_id: str, error: str, slow: bool = False) -> None:
        agent = self.agents[agent_id]
        h = self.health[agent_id]
        now = self._monotonic()
        h.total_failures += 1
        h.last_error = error
        h.failures.append(now)
        while h.failures and now - h.failures[0] > self.window_seconds:
            h.failures.popleft()
        log.warning("Agent %s %s: %s", agent_id, "too slow" if slow else "failed", error)
        self.bus.publish(AgentFailed(error, len(h.failures), slow), agent=agent_id)
        if agent_id in NEVER_BENCHED:
            agent.set_state(AgentState.ERROR, task="erro (nunca vai para o banco; a decisão falha em segurança)",
                            message=f"Erro: {error}"[:200])
            return
        if len(h.failures) >= self.max_failures:
            backoff = min(self.backoff_seconds * 2 ** h.bench_count, MAX_BACKOFF_SECONDS)
            h.bench_count += 1
            h.benched_until = now + backoff
            h.failures.clear()
            agent.set_state(AgentState.RESTING, task=f"no banco por erros ({backoff:.0f} s)",
                            message="Vou descansar e reiniciar.")
            self.bus.publish(AgentBenched(f"{self.max_failures} failures within {self.window_seconds:.0f} s",
                                          "errors", until=backoff), agent=agent_id)
        else:
            agent.set_state(AgentState.ERROR, task="erro isolado", message=f"Erro: {error}"[:200])

    def snapshot(self) -> dict[str, dict]:
        now = self._monotonic()
        return {agent_id: h.to_dict(now) for agent_id, h in self.health.items()}

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
