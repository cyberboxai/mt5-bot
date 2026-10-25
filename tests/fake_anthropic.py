"""A fake Anthropic client for the advisor tests. No network, no SDK, no key.

The exception classes reproduce the SDK's class NAMES and attributes (``status_code``,
``body``, ``type``, ``response.headers``) because the advisor classifies errors by MRO
class name, status and error type, never by importing the SDK.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional, Union


class _Headers(dict):
    def get(self, key: str, default: Any = None) -> Any:  # case-insensitive like httpx
        return super().get(key.lower(), default)


class APIError(Exception):
    def __init__(self, message: str = "", body: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.body = body


class APIStatusError(APIError):
    status_code = 0

    def __init__(self, message: str = "", status_code: Optional[int] = None, error_type: Optional[str] = None,
                 headers: Optional[dict] = None) -> None:
        etype = error_type or ""
        super().__init__(message, {"type": "error", "error": {"type": etype, "message": message}})
        if status_code is not None:
            self.status_code = status_code
        self.type = etype or None
        self.response = SimpleNamespace(status_code=self.status_code,
                                        headers=_Headers({k.lower(): v for k, v in (headers or {}).items()}))


class BadRequestError(APIStatusError):
    status_code = 400


class AuthenticationError(APIStatusError):
    status_code = 401


class PermissionDeniedError(APIStatusError):
    status_code = 403


class NotFoundError(APIStatusError):
    status_code = 404


class RateLimitError(APIStatusError):
    status_code = 429


class InternalServerError(APIStatusError):
    status_code = 500


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


# The failure matrix of the spec: (name, factory). Each must leave decisions unchanged.
FAILURES: dict[str, Callable[[], BaseException]] = {
    "invalid_key_401": lambda: AuthenticationError("invalid x-api-key", error_type="authentication_error"),
    "forbidden_403": lambda: PermissionDeniedError("not allowed", error_type="permission_error"),
    "credit_exhausted_400": lambda: BadRequestError(
        "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to purchase "
        "credits.", error_type="invalid_request_error"),
    "billing_402": lambda: APIStatusError("payment problem", status_code=402, error_type="billing_error"),
    "rate_limit_429": lambda: RateLimitError("rate limited", error_type="rate_limit_error",
                                             headers={"retry-after": "7"}),
    "overloaded_529": lambda: APIStatusError("Overloaded", status_code=529, error_type="overloaded_error"),
    "server_500": lambda: InternalServerError("internal error", error_type="api_error"),
    "timeout": lambda: APITimeoutError("Request timed out."),
    "connection_error": lambda: APIConnectionError("Connection error."),
    "network_oserror": lambda: ConnectionResetError("connection reset by peer"),
}


def review_payload(verdict: str = "confirm", size: float = 1.0, confidence: float = 0.6,
                   flags: tuple[str, ...] = ()) -> dict:
    return {"verdict": verdict, "size_multiplier": size, "confidence": confidence,
            "reasons": [{"code": "OTHER", "text": "test"}], "risk_flags": list(flags)}


def response(payload: Union[dict, str], input_tokens: int = 800, output_tokens: int = 60,
             stop_reason: str = "end_turn", thinking: bool = True) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    blocks = [SimpleNamespace(type="thinking", thinking="")] if thinking else []
    blocks.append(SimpleNamespace(type="text", text=text))
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, usage=usage, stop_reason=stop_reason, model="fake")


Behaviour = Union[SimpleNamespace, BaseException, Callable[[dict], Any]]


class FakeMessages:
    def __init__(self, client: "FakeClient", beta: bool) -> None:
        self._client = client
        self._beta = beta

    def create(self, **params: Any) -> Any:
        return self._client._handle(params, self._beta)


class FakeClient:
    """``behaviour`` is a response, an exception (raised), or a callable(params) returning either."""

    def __init__(self, behaviour: Behaviour = None, delay: float = 0.0) -> None:
        self.behaviour = behaviour if behaviour is not None else response(review_payload())
        self.delay = delay
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.messages = FakeMessages(self, beta=False)
        self.beta = SimpleNamespace(messages=FakeMessages(self, beta=True))

    def _handle(self, params: dict, beta: bool) -> Any:
        with self.lock:
            self.calls.append({"beta": beta, **params})
        if self.delay:
            time.sleep(self.delay)
        outcome = self.behaviour(params) if callable(self.behaviour) and not isinstance(
            self.behaviour, (SimpleNamespace, BaseException)) else self.behaviour
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ClientBox:
    """Factory handed to ClaudeAdvisor; records the keys and timeouts it was given."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.created: list[tuple[str, float]] = []

    def __call__(self, api_key: str, timeout: float) -> FakeClient:
        self.created.append((api_key, timeout))
        return self.client
