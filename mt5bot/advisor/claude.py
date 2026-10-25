"""``ClaudeAdvisor``: the optional Claude API implementation of ``Advisor``.

* The ``anthropic`` SDK is imported lazily; without it the advisor reports DISABLED.
* Calls run on worker threads. The trading thread waits at most ``latency_budget_ms``;
  a late answer is cached for an identical situation and audited, never applied late.
* The SDK's own retries are off (``max_retries=0``): backoff and probing belong to
  ``AdvisorHealth`` so an outage costs one fast failure, not a retry storm.
* Requests use structured outputs (``output_config.format`` with a JSON schema) and a
  low ``effort``. Sampling parameters such as ``temperature`` are deliberately NOT sent:
  current API versions reject them (HTTP 400). Consistency comes from the strict schema.
* Every call is audited (purpose, prompt hash, redacted prompt, response, tokens, cost,
  latency, outcome) for the value-added measurement and for cost review.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import random
import threading
import time
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable, Mapping, Optional, Sequence

from . import keys
from .base import Advisor, AdvisorStore
from .budget import CallBudget, estimate_cost
from .cache import TTLCache, cache_key
from .classify import classify, describe, retry_after_seconds
from .health import OFFLINE_STATES, AdvisorHealth, HealthSettings
from .prompts import (BRIEFING_SYSTEM, EXPLAIN_SYSTEM, MANAGEMENT_SYSTEM, PROBE_SYSTEM, RESEARCH_SYSTEM,
                      TRADE_REVIEW_SYSTEM, clean_untrusted, sanitize_features, untrusted_block,
                      user_message)
from .redact import REDACTOR, Redactor
from .schemas import (EXPLAIN_SCHEMA, MANAGEMENT_SCHEMA, PROBE_SCHEMA, SchemaError, TRADE_REVIEW_SCHEMA,
                      api_schema, briefing_schema, parse_briefing, parse_management, parse_research,
                      parse_trade_review, research_schema, validate)
from .types import (AdvisorOpinion, AdvisorState, Briefing, CallResult, ErrorKind, ManagementSuggestion,
                    ResearchNote, unavailable)

log = logging.getLogger("mt5_bot")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CHARS_PER_TOKEN = 3.0  # pessimistic, for the worst-case cost reservation only
MAX_STORED_TEXT = 4000
PROBE_MAX_TOKENS = 64
TILT_HOURS = 24.0

ClientFactory = Callable[[str, float], Any]


def _default_client_factory(api_key: str, timeout: float) -> Any:
    import anthropic  # noqa: PLC0415 - optional dependency, imported only when the advisor is used

    return anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)


def _first_text(response: Any) -> Optional[str]:
    for block in getattr(response, "content", None) or ():
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", None)
    return None


def _int_attr(obj: Any, name: str) -> int:
    value = getattr(obj, name, 0)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


class ClaudeAdvisor(Advisor):
    name = "claude"

    def __init__(self, cfg: Any, store: Optional[AdvisorStore] = None, env: Optional[Mapping[str, str]] = None,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 client_factory: Optional[ClientFactory] = None, notify: Callable[[str], None] = lambda text: None,
                 rng: Optional[random.Random] = None, redactor: Redactor = REDACTOR,
                 keyring_module: Any = None, sdk_check: Callable[[], bool] = keys.sdk_available,
                 strategy_menu: Optional[Callable[[], dict]] = None,
                 menu_lookup: Optional[Callable[[str, str, str], Any]] = None) -> None:
        self.cfg = cfg
        self.store = store
        self._env = os.environ if env is None else env
        self._clock = clock
        self._monotonic = monotonic
        self._client_factory = client_factory or _default_client_factory
        self._sdk_ok = client_factory is not None or sdk_check()
        self._notify = notify
        self._redactor = redactor
        self._keyring = keyring_module
        self._strategy_menu = strategy_menu or (lambda: {})
        self._menu_lookup = menu_lookup
        self.health = AdvisorHealth(HealthSettings.from_config(cfg), clock, rng, self._on_transition)
        self.budget = CallBudget(cfg.max_calls_per_hour, cfg.max_daily_cost_usd, clock,
                                 restore=store.advisor_usage_since if store is not None else None)
        self.cache = TTLCache(cfg.cache_ttl_seconds, clock=clock)
        self._fast = ThreadPoolExecutor(max_workers=1, thread_name_prefix="advisor")
        self._bg = ThreadPoolExecutor(max_workers=1, thread_name_prefix="advisor-bg")
        self._lock = threading.Lock()
        self._inflight: Optional[Future] = None
        self._bg_inflight: Optional[Future] = None
        self._client: Any = None
        self._client_fp: Optional[str] = None
        self._key: Optional[str] = None
        self._fallback_ok = True
        self._closed = False
        self._latencies: deque[float] = deque(maxlen=500)
        self._verdicts: Counter[str] = Counter()
        self._outcomes: Counter[str] = Counter()
        self._briefing: Optional[Briefing] = None
        self._research: Optional[ResearchNote] = None
        self._explanations: list[tuple[str, str]] = []
        self.refresh_key()

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> AdvisorState:
        self.health.tick()
        return self.health.state

    def refresh_key(self) -> None:
        key = keys.load_api_key(self._env, self.cfg.use_keyring, self._keyring) if self.cfg.enabled else None
        if key:
            self._redactor.add_secret(key)
        self._key = key
        self.health.configure(self.cfg.enabled, keys.fingerprint(key), sdk_available=self._sdk_ok)

    def update_config(self, cfg: Any) -> None:
        self.cfg = cfg
        self.health.settings = HealthSettings.from_config(cfg)
        self.budget.update_limits(cfg.max_calls_per_hour, cfg.max_daily_cost_usd)
        self.cache.ttl = float(cfg.cache_ttl_seconds)
        self.refresh_key()

    def _on_transition(self, old: AdvisorState, new: AdvisorState, reason: str) -> None:
        text = f"Claude advisor {old.value} -> {new.value}: {reason}"
        if new == AdvisorState.NO_KEY:
            log.info("Claude advisor is enabled but no API key was found (ANTHROPIC_API_KEY); the bot runs "
                     "deterministically without it and re-checks the key every %.0f s.",
                     self.cfg.key_recheck_seconds)
        elif new in OFFLINE_STATES or old in OFFLINE_STATES or new == AdvisorState.DEGRADED:
            log.warning("%s. Trading continues on the deterministic path.", self._redactor.redact(text))
        else:
            log.info("%s", self._redactor.redact(text))
        if new in OFFLINE_STATES:
            self._notify(f"{text}. The bot keeps trading without the advisor; probing every "
                         f"{self.cfg.probe_offline_seconds / 60:.0f}+ minutes.")
        if self.store is not None:
            try:
                self.store.log_advisor_state(self._clock(), old.value, new.value, self._redactor.redact(reason))
            except Exception:  # noqa: BLE001 - auditing must never break the advisor
                log.debug("advisor state audit failed", exc_info=True)

    def tick(self) -> None:
        """Called from the trading loop's housekeeping. Never blocks."""
        if self._closed:
            return
        self.health.tick()
        if self.health.key_recheck_due():
            self.health.mark_key_checked()
            self.refresh_key()
        if self.health.begin_probe():
            try:
                self._bg.submit(self._probe)
            except RuntimeError:
                self.health.end_probe()

    # ---------------------------------------------------------------- calling

    def _get_client(self) -> Any:
        fp = keys.fingerprint(self._key)
        if self._client is None or fp != self._client_fp:
            self._client = self._client_factory(self._key or "", float(self.cfg.timeout_seconds))
            self._client_fp = fp
        return self._client

    def _worst_case(self, model: str, system: str, user: str, max_tokens: int) -> float:
        price, _known = self.cfg.price(model)
        tokens_in = int((len(system) + len(user)) / CHARS_PER_TOKEN) + 50
        return estimate_cost(price, tokens_in, max_tokens, 0, tokens_in)

    def _audit(self, purpose: str, model: str, prompt_hash: str, user: str, outcome: str, result: CallResult,
               response_text: str = "") -> Optional[int]:
        if self.store is None:
            return None
        row = {
            "ts": self._clock(), "purpose": purpose, "model": model, "prompt_hash": prompt_hash,
            "redacted_prompt": self._redactor.redact(user)[:MAX_STORED_TEXT],
            "response": self._redactor.redact(response_text)[:MAX_STORED_TEXT],
            "valid": 1 if result.ok else 0, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "cache_read_tokens": result.cache_read_tokens,
            "cache_write_tokens": result.cache_write_tokens, "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms, "outcome": outcome,
            "error_kind": result.error_kind.value if result.error_kind else None,
            "detail": self._redactor.redact(result.detail)[:500], "state_after": self.health.state.value,
        }
        try:
            return self.store.log_advisor_call(row)
        except Exception:  # noqa: BLE001
            log.debug("advisor call audit failed", exc_info=True)
            return None

    def _call(self, purpose: str, model: str, system: str, user: str, schema: dict, max_tokens: int,
              effort: str, deep: bool = False, probe: bool = False) -> CallResult:
        """One API call. Runs on a worker thread. Never raises."""
        started = self._monotonic()
        prompt_hash = hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest()[:24]
        params = {
            "model": model,
            "max_tokens": int(max_tokens),
            # Stable system prompt with a cache breakpoint: identical prefixes are re-used.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": api_schema(schema)}, "effort": effort},
        }
        try:
            client = self._get_client()
            if deep and self.cfg.refusal_fallback_deep and self._fallback_ok:
                try:
                    response = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
                except Exception as exc:  # noqa: BLE001 - classified below
                    if classify(exc) != ErrorKind.BAD_REQUEST:
                        raise
                    self._fallback_ok = False
                    log.warning("Server-side refusal fallback was rejected (%s); deep calls continue without it "
                                "for this session", type(exc).__name__)
                    response = client.messages.create(**params)
            else:
                response = client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001 - every failure is classified, never propagated
            kind = classify(exc)
            info = describe(exc)
            result = CallResult(False, error_kind=kind, latency_ms=(self._monotonic() - started) * 1000.0,
                                detail=self._redactor.redact(f"{info['class']} status={info['status']} "
                                                             f"type={info['type']}"))
            self.health.record_failure(kind, "", retry_after_seconds(exc))
            self._outcomes[f"error:{kind.value}"] += 1
            log.debug("advisor %s call failed: %s", purpose, result.detail)
            result.call_id = self._audit(purpose, model, prompt_hash, user, "error", result)
            return result

        latency = (self._monotonic() - started) * 1000.0
        usage = getattr(response, "usage", None)
        result = CallResult(False, latency_ms=latency, input_tokens=_int_attr(usage, "input_tokens"),
                            output_tokens=_int_attr(usage, "output_tokens"),
                            cache_read_tokens=_int_attr(usage, "cache_read_input_tokens"),
                            cache_write_tokens=_int_attr(usage, "cache_creation_input_tokens"))
        price, known = self.cfg.price(model)
        if not known:
            log.debug("no price configured for %s; using the most expensive configured price", model)
        result.cost_usd = estimate_cost(price, result.input_tokens, result.output_tokens, result.cache_read_tokens,
                                        result.cache_write_tokens)
        self._latencies.append(latency)
        text = _first_text(response) or ""
        if probe:
            self.health.record_success()
            result.ok = True
            self._outcomes["probe_ok"] += 1
            result.call_id = self._audit(purpose, model, prompt_hash, user, "probe_ok", result, text)
            return result
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            self.health.record_success()  # the API is healthy; this one request was declined
            result.detail = "model declined the request (refusal)"
            self._outcomes["refusal"] += 1
            result.call_id = self._audit(purpose, model, prompt_hash, user, "refusal", result, text)
            return result
        try:
            data = json.loads(text)
            validate(schema, data)
        except (ValueError, SchemaError) as exc:
            result.error_kind = ErrorKind.INVALID_RESPONSE
            result.detail = f"invalid response ({'truncated at max_tokens' if stop == 'max_tokens' else exc})"[:300]
            self.health.record_invalid_response()
            self._outcomes["invalid"] += 1
            result.call_id = self._audit(purpose, model, prompt_hash, user, "invalid", result, text)
            return result
        self.health.record_success()
        result.ok, result.data = True, data
        self._outcomes["ok"] += 1
        result.call_id = self._audit(purpose, model, prompt_hash, user, "ok", result, json.dumps(data))
        return result

    def _reserve(self, model: str, system: str, user: str, max_tokens: int) -> tuple[bool, float, str]:
        worst = self._worst_case(model, system, user, max_tokens)
        ok, why, resume = self.budget.reserve(worst)
        if not ok:
            self.health.budget_capped(resume, why)
            return False, 0.0, why
        return True, worst, "ok"

    def _probe(self) -> None:
        try:
            self.refresh_key()  # a NEW key clears a sticky auth state without spending a probe
            if self.health.state not in (AdvisorState.DEGRADED, AdvisorState.OFFLINE_AUTH,
                                         AdvisorState.OFFLINE_NO_CREDIT):
                return
            user = "ping"
            ok, worst, _why = self._reserve(self.cfg.model_fast, PROBE_SYSTEM, user, PROBE_MAX_TOKENS)
            if not ok:
                return
            result = self._call("probe", self.cfg.model_fast, PROBE_SYSTEM, user, PROBE_SCHEMA, PROBE_MAX_TOKENS,
                                "low", probe=True)
            self.budget.settle(worst, result)
        except Exception:  # noqa: BLE001 - a probe must never escape its thread
            log.debug("advisor probe failed unexpectedly", exc_info=True)
        finally:
            self.health.end_probe()

    # -------------------------------------------------------- decision-time calls

    def _sync_call(self, purpose: str, system: str, schema: dict, task: str, features: Mapping[str, Any],
                   untrusted: Sequence[str], parse: Callable[[CallResult, str], Any],
                   on_unavailable: Callable[[str, float], Any]) -> Any:
        """Shared path of review_trade / suggest_management: bounded wait on the fast worker."""
        started = self._monotonic()

        def elapsed() -> float:
            return (self._monotonic() - started) * 1000.0

        if self._closed:
            return on_unavailable("advisor closed", elapsed())
        self.health.tick()
        if not self.health.can_call():
            return on_unavailable(f"advisor {self.health.state.value}: {self.health.reason}", elapsed())
        block = untrusted_block("news", untrusted)
        user = user_message(task, features, block)
        key = cache_key(purpose, self.cfg.model_fast, sanitize_features(features), clean_untrusted(untrusted))
        cached = self.cache.get(key)
        if cached is not None:
            return dataclasses.replace(cached, cached=True, latency_ms=elapsed()) \
                if isinstance(cached, AdvisorOpinion) else cached
        with self._lock:
            if self._inflight is not None and not self._inflight.done():
                return on_unavailable("advisor busy with a previous request", elapsed())
            ok, worst, why = self._reserve(self.cfg.model_fast, system, user, self.cfg.max_tokens_fast)
            if not ok:
                return on_unavailable(f"BUDGET_CAPPED: {why}", elapsed())

            def job() -> Any:
                result = self._call(purpose, self.cfg.model_fast, system, user, schema, self.cfg.max_tokens_fast,
                                    self.cfg.effort_fast)
                self.budget.settle(worst, result)
                value = parse(result, key)
                if result.ok:
                    self.cache.put(key, value)
                return value

            try:
                future = self._fast.submit(job)
            except RuntimeError:
                self.budget.settle(worst, None)
                return on_unavailable("advisor closed", elapsed())
            self._inflight = future
        remaining = self.cfg.latency_budget_ms / 1000.0 - (self._monotonic() - started)
        try:
            return future.result(timeout=max(0.0, remaining))
        except FuturesTimeout:
            self._outcomes["latency_budget_exceeded"] += 1
            return on_unavailable(f"latency budget {self.cfg.latency_budget_ms} ms exceeded", elapsed())
        except Exception:  # noqa: BLE001 - defensive; job() does not raise
            return on_unavailable("advisor error", elapsed())

    def review_trade(self, features: Mapping[str, Any], untrusted: Sequence[str] = ()) -> AdvisorOpinion:
        if not self.cfg.review_trades:
            return unavailable("trade reviews disabled (claude.review_trades)")
        if self.cfg.enabled is False:
            return unavailable("claude.enabled is false")

        def parse(result: CallResult, _key: str) -> AdvisorOpinion:
            if not result.ok or result.data is None:
                return unavailable(result.detail or (result.error_kind.value if result.error_kind else "failed"),
                                   result.latency_ms)
            fields = parse_trade_review(result.data)
            self._verdicts[fields["verdict"]] += 1
            return AdvisorOpinion(True, model=self.cfg.model_fast, latency_ms=result.latency_ms,
                                  call_id=result.call_id, **fields)

        return self._sync_call("trade_review", TRADE_REVIEW_SYSTEM, TRADE_REVIEW_SCHEMA,
                               "pre-trade review of one candidate entry", features, untrusted, parse, unavailable)

    def suggest_management(self, features: Mapping[str, Any]) -> ManagementSuggestion:
        if not self.cfg.manage_positions or not self.cfg.enabled:
            return ManagementSuggestion(False, unavailable_reason="management suggestions disabled")

        def parse(result: CallResult, _key: str) -> ManagementSuggestion:
            if not result.ok or result.data is None:
                return ManagementSuggestion(False, unavailable_reason=result.detail or "failed")
            return parse_management(result.data)

        def none(reason: str, _ms: float) -> ManagementSuggestion:
            return ManagementSuggestion(False, unavailable_reason=reason)

        return self._sync_call("management", MANAGEMENT_SYSTEM, MANAGEMENT_SCHEMA,
                               "management checkpoint for one open position", features, (), parse, none)

    # ------------------------------------------------------------ background calls

    def _background(self, purpose: str, model: str, system: str, schema: dict, user: str, max_tokens: int,
                    effort: str, deep: bool, deliver: Callable[[CallResult], None]) -> bool:
        if self._closed or not self.cfg.enabled:
            return False
        self.health.tick()
        if not self.health.can_call():
            return False
        with self._lock:
            if self._bg_inflight is not None and not self._bg_inflight.done():
                return False
            ok, worst, _why = self._reserve(model, system, user, max_tokens)
            if not ok:
                return False

            def job() -> None:
                try:
                    result = self._call(purpose, model, system, user, schema, max_tokens, effort, deep=deep)
                    self.budget.settle(worst, result)
                    if result.ok and result.data is not None:
                        deliver(result)
                except Exception:  # noqa: BLE001
                    log.debug("advisor background %s failed", purpose, exc_info=True)

            try:
                self._bg_inflight = self._bg.submit(job)
            except RuntimeError:
                self.budget.settle(worst, None)
                return False
        return True

    def request_briefing(self, features: Mapping[str, Any], untrusted: Sequence[str] = ()) -> bool:
        if not self.cfg.daily_briefing:
            return False
        from ..team.roster import TILTABLE  # noqa: PLC0415 - avoid a package import cycle at load time

        tiltable = sorted(TILTABLE)
        schema = briefing_schema(tiltable)
        user = user_message("daily regime briefing", features, untrusted_block("news", untrusted))

        def deliver(result: CallResult) -> None:
            briefing = parse_briefing(result.data or {}, tiltable, self._clock(), TILT_HOURS)
            with self._lock:
                self._briefing = briefing

        return self._background("briefing", self.cfg.model_deep, BRIEFING_SYSTEM, schema, user,
                                self.cfg.max_tokens_deep, self.cfg.effort_deep, True, deliver)

    def request_research(self, kind: str, features: Mapping[str, Any]) -> bool:
        enabled = self.cfg.post_trade_reviews if kind == "post_trade" else self.cfg.weekly_research
        if not enabled:
            return False
        menu = self._strategy_menu()
        names = sorted(menu) or ["notrade"]
        schema = research_schema(names)
        payload = dict(features)
        payload["menu"] = menu
        user = user_message(f"{kind} research review", payload)
        model = self.cfg.model_fast if kind == "post_trade" else self.cfg.model_deep

        def deliver(result: CallResult) -> None:
            note = parse_research(result.data or {}, names, self._menu_lookup)
            with self._lock:
                self._research = note

        return self._background(kind, model, RESEARCH_SYSTEM, schema, user,
                                self.cfg.max_tokens_fast if kind == "post_trade" else self.cfg.max_tokens_deep,
                                self.cfg.effort_fast if kind == "post_trade" else self.cfg.effort_deep,
                                kind != "post_trade", deliver)

    def request_explanation(self, dossier_id: str, features: Mapping[str, Any]) -> bool:
        if not self.cfg.explain_decisions:
            return False
        user = user_message("explain one decision dossier", features)

        def deliver(result: CallResult) -> None:
            text = str((result.data or {}).get("explanation", ""))
            with self._lock:
                self._explanations.append((dossier_id, text))
                del self._explanations[:-50]

        return self._background("explain", self.cfg.model_fast, EXPLAIN_SYSTEM, EXPLAIN_SCHEMA, user,
                                self.cfg.max_tokens_fast, self.cfg.effort_fast, False, deliver)

    def take_briefing(self) -> Optional[Briefing]:
        with self._lock:
            out, self._briefing = self._briefing, None
        return out

    def take_research(self) -> Optional[ResearchNote]:
        with self._lock:
            out, self._research = self._research, None
        return out

    def take_explanations(self) -> list[tuple[str, str]]:
        with self._lock:
            out, self._explanations = self._explanations, []
        return out

    # ------------------------------------------------------------------ status

    def snapshot(self) -> dict[str, Any]:
        lat = sorted(self._latencies)

        def pct(p: float) -> Optional[float]:
            return round(lat[min(len(lat) - 1, int(p * len(lat)))], 1) if lat else None

        out = self.health.snapshot()
        out.update({
            "provider": "claude", "model_fast": self.cfg.model_fast, "model_deep": self.cfg.model_deep,
            "budget": self.budget.snapshot(), "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "n": len(lat)},
            "latency_budget_ms": self.cfg.latency_budget_ms, "verdicts": dict(self._verdicts),
            "outcomes": dict(self._outcomes), "cache_entries": len(self.cache),
        })
        return out

    def close(self) -> None:
        self._closed = True
        self._fast.shutdown(wait=False, cancel_futures=True)
        self._bg.shutdown(wait=False, cancel_futures=True)

    # helpers for tests
    def wait_idle(self, timeout: float = 5.0) -> None:
        """Block until in-flight work finished (tests and orderly shutdown only)."""
        deadline = time.monotonic() + timeout
        for fut in (self._inflight, self._bg_inflight):
            if fut is not None:
                try:
                    fut.result(timeout=max(0.0, deadline - time.monotonic()))
                except Exception:  # noqa: BLE001
                    pass
        self._fast.submit(lambda: None).result(timeout=max(0.0, deadline - time.monotonic()))
        self._bg.submit(lambda: None).result(timeout=max(0.0, deadline - time.monotonic()))


def clean_headlines(items: Sequence[Any]) -> list[str]:
    """Public helper for the News Sentinel (same cleaning as the prompt wrapper)."""
    return clean_untrusted(items)
