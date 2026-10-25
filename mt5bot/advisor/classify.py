"""Table-driven classification of advisor call failures into ``ErrorKind``.

SDK exception classes and their attributes vary between ``anthropic`` versions, so a rule
matches on any of: a class name anywhere in the exception's MRO (no SDK import needed),
the HTTP status code, the API error ``type`` string, or text in the message. The billing
case is matched on text FIRST because "credit balance too low" has been reported under
more than one status code. Rules are evaluated in order; the first match wins.
"""

from __future__ import annotations

import concurrent.futures
import re
import socket
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .types import ErrorKind

BILLING_TEXT = re.compile(
    r"credit balance|insufficient (credit|funds|balance)|billing|payment required|purchase (more )?credits"
    r"|out of credits|plans? (&|and) billing", re.IGNORECASE)


def _mro_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def status_code(exc: BaseException) -> Optional[int]:
    for candidate in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None),
                      getattr(exc, "status", None)):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


def error_type(exc: BaseException) -> Optional[str]:
    """The API error type (``billing_error``, ``overloaded_error``...), from ``.type`` or the body."""
    value = getattr(exc, "type", None)
    if isinstance(value, str):
        return value
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(inner.get("type"), str):
            return inner["type"]
    return None


def error_text(exc: BaseException) -> str:
    parts = [str(exc), str(getattr(exc, "message", "") or "")]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        parts.append(str(inner.get("message", "")))
    return " ".join(p for p in parts if p)


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class Rule:
    name: str
    kind: ErrorKind
    classes: frozenset[str] = frozenset()
    statuses: frozenset[int] = frozenset()
    types: frozenset[str] = frozenset()
    text: Optional[re.Pattern[str]] = None
    predicate: Optional[Callable[[BaseException], bool]] = None

    def matches(self, exc: BaseException, names: set[str], status: Optional[int], etype: Optional[str],
                text: str) -> bool:
        if self.classes & names:
            return True
        if status is not None and status in self.statuses:
            return True
        if etype is not None and etype in self.types:
            return True
        if self.text is not None and self.text.search(text):
            return True
        return bool(self.predicate and self.predicate(exc))


def _is_server_status(exc: BaseException) -> bool:
    code = status_code(exc)
    return code is not None and code >= 500 and code != 529


def _is_local_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout, concurrent.futures.TimeoutError))


def _is_network(exc: BaseException) -> bool:
    return isinstance(exc, (ConnectionError, OSError)) and status_code(exc) is None


RULES: tuple[Rule, ...] = (
    Rule("billing text", ErrorKind.NO_CREDIT, text=BILLING_TEXT),
    Rule("billing status/type", ErrorKind.NO_CREDIT, statuses=frozenset({402}), types=frozenset({"billing_error"})),
    Rule("authentication", ErrorKind.AUTH, classes=frozenset({"AuthenticationError"}), statuses=frozenset({401}),
         types=frozenset({"authentication_error"})),
    Rule("permission", ErrorKind.PERMISSION, classes=frozenset({"PermissionDeniedError"}),
         statuses=frozenset({403}), types=frozenset({"permission_error"})),
    Rule("rate limit", ErrorKind.RATE_LIMIT, classes=frozenset({"RateLimitError"}), statuses=frozenset({429}),
         types=frozenset({"rate_limit_error"})),
    Rule("overloaded", ErrorKind.OVERLOADED, classes=frozenset({"OverloadedError"}), statuses=frozenset({529}),
         types=frozenset({"overloaded_error"})),
    Rule("not found", ErrorKind.NOT_FOUND, classes=frozenset({"NotFoundError"}), statuses=frozenset({404}),
         types=frozenset({"not_found_error"})),
    Rule("server error", ErrorKind.SERVER, classes=frozenset({"InternalServerError", "ServiceUnavailableError"}),
         types=frozenset({"api_error"}), predicate=_is_server_status),
    # APITimeoutError subclasses APIConnectionError in the Python SDK: timeout must come first.
    Rule("timeout", ErrorKind.TIMEOUT, classes=frozenset({"APITimeoutError", "ReadTimeout", "ConnectTimeout",
                                                          "TimeoutException"}), predicate=_is_local_timeout),
    Rule("connection", ErrorKind.CONNECTION, classes=frozenset({"APIConnectionError", "ConnectError",
                                                                "RemoteProtocolError", "NetworkError"}),
         predicate=_is_network),
    Rule("bad request", ErrorKind.BAD_REQUEST, classes=frozenset({"BadRequestError", "UnprocessableEntityError",
                                                                  "RequestTooLargeError"}),
         statuses=frozenset({400, 409, 413, 422}), types=frozenset({"invalid_request_error", "request_too_large"})),
)


def classify(exc: BaseException) -> ErrorKind:
    names = _mro_names(exc)
    status = status_code(exc)
    etype = error_type(exc)
    text = error_text(exc)
    for rule in RULES:
        if rule.matches(exc, names, status, etype, text):
            return rule.kind
    return ErrorKind.UNKNOWN


def describe(exc: BaseException) -> dict[str, Any]:
    """Safe, short description for logs and the audit table (callers still redact it)."""
    return {"class": type(exc).__name__, "status": status_code(exc), "type": error_type(exc),
            "kind": classify(exc).value}
