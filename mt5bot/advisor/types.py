"""Value types shared by the advisor modules. Advisor output is DATA: these objects are
built only from schema-validated responses and are never executed or used as commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AdvisorState(str, Enum):
    DISABLED = "DISABLED"
    NO_KEY = "NO_KEY"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE_NO_CREDIT = "OFFLINE_NO_CREDIT"
    OFFLINE_AUTH = "OFFLINE_AUTH"
    BUDGET_CAPPED = "BUDGET_CAPPED"


class ErrorKind(str, Enum):
    AUTH = "auth"                    # 401: invalid or revoked key
    PERMISSION = "permission"        # 403: key not allowed
    NO_CREDIT = "no_credit"          # credit balance too low / billing problem
    RATE_LIMIT = "rate_limit"        # 429
    OVERLOADED = "overloaded"        # 529
    SERVER = "server"                # 5xx
    TIMEOUT = "timeout"              # SDK or local timeout
    CONNECTION = "connection"        # network failure before a response
    BAD_REQUEST = "bad_request"      # 400/413/422 not caused by billing: our request or config
    NOT_FOUND = "not_found"          # 404: usually a wrong model id in config
    INVALID_RESPONSE = "invalid_response"  # malformed JSON or schema violation
    UNKNOWN = "unknown"


TRANSIENT_KINDS = frozenset({ErrorKind.RATE_LIMIT, ErrorKind.OVERLOADED, ErrorKind.SERVER, ErrorKind.TIMEOUT,
                             ErrorKind.CONNECTION, ErrorKind.UNKNOWN})
CONFIG_KINDS = frozenset({ErrorKind.BAD_REQUEST, ErrorKind.NOT_FOUND})

VERDICTS = ("confirm", "reduce", "veto")
MANAGEMENT_ACTIONS = ("hold", "tighten_stop", "partial_close", "close_now")

REASON_CODES = (
    "TREND_ALIGNED", "TREND_CONFLICT", "VOLATILITY_HIGH", "VOLATILITY_LOW", "SPREAD_WIDE", "NEWS_RISK",
    "CORRELATION_RISK", "SESSION_ILLIQUID", "REGIME_UNFAVORABLE", "AGENT_DISAGREEMENT", "DATA_QUALITY",
    "DRAWDOWN_CONTEXT", "PROFIT_PROTECTION", "OTHER",
)
RISK_FLAGS = (
    "news_event", "high_volatility", "wide_spread", "correlated_exposure", "low_liquidity", "regime_shift",
    "drawdown_pressure", "data_anomaly", "prompt_injection_suspected", "other",
)


@dataclass(frozen=True)
class Reason:
    code: str
    text: str


@dataclass(frozen=True)
class AdvisorOpinion:
    """Pre-trade review. ``available=False`` means: behave exactly as if no advisor existed."""

    available: bool
    verdict: str = "confirm"
    size_multiplier: float = 1.0
    confidence: float = 0.0
    reasons: tuple[Reason, ...] = ()
    risk_flags: tuple[str, ...] = ()
    unavailable_reason: str = ""
    model: str = ""
    latency_ms: float = 0.0
    cached: bool = False
    call_id: Optional[int] = None

    @property
    def effective_multiplier(self) -> float:
        """What this opinion does to size: 1.0 when unavailable or "confirm"."""
        if not self.available:
            return 1.0
        if self.verdict == "veto":
            return 0.0
        if self.verdict == "reduce":
            return min(1.0, max(0.0, float(self.size_multiplier)))
        return 1.0

    def to_dict(self) -> dict[str, Any]:
        if not self.available:
            return {"status": "unavailable", "reason": self.unavailable_reason}
        return {"status": "ok", "verdict": self.verdict, "size_multiplier": self.size_multiplier,
                "effective_multiplier": self.effective_multiplier, "confidence": self.confidence,
                "reasons": [{"code": r.code, "text": r.text} for r in self.reasons],
                "risk_flags": list(self.risk_flags), "model": self.model, "latency_ms": round(self.latency_ms, 1),
                "cached": self.cached, "call_id": self.call_id}


def unavailable(reason: str, latency_ms: float = 0.0) -> AdvisorOpinion:
    return AdvisorOpinion(available=False, unavailable_reason=reason, latency_ms=latency_ms)


@dataclass(frozen=True)
class ManagementSuggestion:
    available: bool
    action: str = "hold"
    new_stop_price: float = 0.0
    close_fraction: float = 0.0
    confidence: float = 0.0
    reasons: tuple[Reason, ...] = ()
    unavailable_reason: str = ""


@dataclass(frozen=True)
class WeightTilt:
    agent: str
    factor: float
    expires_ts: float


@dataclass(frozen=True)
class Briefing:
    available: bool
    regime_view: str = ""
    narrative: str = ""
    tilts: tuple[WeightTilt, ...] = ()
    risk_flags: tuple[str, ...] = ()
    unavailable_reason: str = ""


@dataclass(frozen=True)
class Hypothesis:
    strategy: str
    param: str
    value: Any
    rationale: str


@dataclass(frozen=True)
class ResearchNote:
    available: bool
    lesson: str = ""
    reasons: tuple[Reason, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    unavailable_reason: str = ""


@dataclass
class CallResult:
    """Outcome of one API call, produced in the worker thread. Never raised, always returned."""

    ok: bool
    data: Optional[dict] = None
    error_kind: Optional[ErrorKind] = None
    detail: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    call_id: Optional[int] = None
    extra: dict = field(default_factory=dict)
