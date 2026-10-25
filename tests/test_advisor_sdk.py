"""End-to-end check of ClaudeAdvisor with the REAL ``anthropic`` SDK against a local stub
HTTP server (no internet, no real key). Skipped when the optional SDK is not installed.
It verifies that the SDK accepts the request we build (structured output, effort,
cache_control, no sampling parameters) and that HTTP failures map to the right states."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

anthropic = pytest.importorskip("anthropic")

from mt5bot.advisor.claude import ClaudeAdvisor  # noqa: E402
from mt5bot.advisor.types import AdvisorState  # noqa: E402
from mt5bot.config import Config  # noqa: E402

KEY = "sk-ant-api03-LOCALSTUB-000000000000"


class Stub(BaseHTTPRequestHandler):
    status = 200
    body: Any = None
    seen: list = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length", "0"))
        Stub.seen.append({"path": self.path, "headers": dict(self.headers),
                          "json": json.loads(self.rfile.read(length) or b"{}")})
        payload = json.dumps(Stub.body).encode()
        self.send_response(Stub.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    Stub.seen = []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def message(text: str) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 40, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}


def advisor_for(base_url: str) -> ClaudeAdvisor:
    cfg = Config.from_dict({"claude": {"enabled": True, "latency_budget_ms": 5000}}).claude

    def factory(key: str, timeout: float) -> Any:
        return anthropic.Anthropic(api_key=key, base_url=base_url, timeout=timeout, max_retries=0)

    return ClaudeAdvisor(cfg, env={"ANTHROPIC_API_KEY": KEY}, client_factory=factory)


def test_real_sdk_request_shape_and_parsing(server: str) -> None:
    Stub.status = 200
    Stub.body = message(json.dumps({"verdict": "reduce", "size_multiplier": 0.5, "confidence": 0.7,
                                    "reasons": [{"code": "SPREAD_WIDE", "text": "spread"}], "risk_flags": []}))
    advisor = advisor_for(server)
    try:
        op = advisor.review_trade({"symbol": "EURUSD", "side": "BUY", "spread_points": 25.0})
        assert op.available and op.effective_multiplier == 0.5
        sent = Stub.seen[0]
        body = sent["json"]
        assert sent["path"].startswith("/v1/messages")
        assert body["output_config"]["format"]["type"] == "json_schema" and body["output_config"]["effort"] == "low"
        assert "temperature" not in body and body["system"][0]["cache_control"]["type"] == "ephemeral"
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        assert KEY in (headers.get("x-api-key", "") + headers.get("authorization", ""))  # SDK 2.x: Bearer
        assert headers.get("x-stainless-retry-count") == "0"
    finally:
        advisor.close()


@pytest.mark.parametrize("status,etype,message_text,state", [
    (401, "authentication_error", "invalid x-api-key", AdvisorState.OFFLINE_AUTH),
    (400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API.",
     AdvisorState.OFFLINE_NO_CREDIT),
    (529, "overloaded_error", "Overloaded", AdvisorState.DEGRADED),
    (500, "api_error", "Internal server error", AdvisorState.DEGRADED),
])
def test_real_sdk_http_failures_map_to_states(server: str, status: int, etype: str, message_text: str,
                                              state: AdvisorState) -> None:
    Stub.status = status
    Stub.body = {"type": "error", "error": {"type": etype, "message": message_text}}
    advisor = advisor_for(server)
    try:
        op = advisor.review_trade({"symbol": "EURUSD", "side": "SELL"})
        assert not op.available and op.effective_multiplier == 1.0
        assert advisor.state == state
        assert len(Stub.seen) == 1  # max_retries=0: the SDK does not retry behind our back
    finally:
        advisor.close()
