"""CYBERBOX Trading Office panel: API contracts, secrets never leave, secure headers, static
files, the live event stream (delivery and resume), control actions (token, CSRF, origin,
confirmation, rate limit, audit), remote-access rules, the event -> character mapping, and
isolation from the bot (no trading-loop imports; killing the panel does not hurt the bot)."""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from mt5bot.config import Config
from mt5bot.officedb import OfficeStore
from mt5bot.sim.terminal import SIM_LOGIN
from panel import office as office_mod
from panel.app import main as panel_main
from panel.app import run_in_thread, stop_server

PROJECT = Path(__file__).resolve().parent.parent
TOKEN = "panel-test-token-0123456789"
API_KEY = "sk-ant-api03-PANEL-LEAK-CHECK-000000"
ENDPOINTS = ("/api/status", "/api/office/state", "/api/equity", "/api/performance", "/api/trades", "/api/agents",
             "/api/agents/risk_manager", "/api/decisions", "/api/strategies", "/api/risk", "/api/markets",
             "/api/advisor", "/api/health", "/api/team", "/api/events", "/api/replay")


@pytest.fixture(scope="module")
def sim_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from mt5bot.sim.runner import run_simulation

    root = tmp_path_factory.mktemp("panel_sim")
    stop = threading.Event()

    def enough(step: int, bot: Any) -> None:
        if step >= 25 and bot.team.store.dossiers(1):
            stop.set()

    run_simulation(Config(), root, steps=200, speed=0, warmup_bars=700, stop=stop, on_step=enough)
    return root


def panel_cfg(root: Path, **extra: Any) -> Config:
    values = {"state_root": str(root), "health_file": str(root / "health.json"), "kill_switch_file": str(root / "KILL"),
              "log_file": str(root / "simulation.log"), "symbols": ["EURUSD", "GBPUSD", "USDJPY"]}
    values.update(extra)
    return Config.from_dict(values)


@pytest.fixture
def panel(sim_root: Path):
    servers = []

    def start(**kwargs: Any) -> tuple[Any, int]:
        ctx, server, port = run_in_thread(panel_cfg(sim_root), **kwargs)
        servers.append((ctx, server))
        return ctx, port

    yield start
    for ctx, server in servers:
        stop_server(ctx, server)


def request(port: int, path: str, method: str = "GET", body: Optional[dict] = None,
            headers: Optional[dict] = None) -> tuple[int, dict, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if payload is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, text)
    conn.close()
    return out


# ---------------------------------------------------------------- API contract


def test_every_endpoint_answers_json_with_secure_headers(panel) -> None:
    _ctx, port = panel()
    for path in ENDPOINTS:
        status, headers, text = request(port, path)
        assert status == 200, (path, text[:200])
        json.loads(text)
        assert headers["content-type"].startswith("application/json")
        assert "default-src 'self'" in headers["content-security-policy"]
        assert "unsafe-eval" not in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff" and headers["referrer-policy"] == "no-referrer"
        assert headers["x-frame-options"] == "DENY" and headers["cache-control"] == "no-store"
        assert "python" not in headers.get("server", "").lower()


def test_status_and_office_contract(panel) -> None:
    _ctx, port = panel()
    status = json.loads(request(port, "/api/status")[2])
    assert status["product"] == "CYBERBOX Trading Office" and status["account_type"] == "DEMO"
    assert status["simulation"] is True and status["team"]["effective_mode"] == "full"
    assert {"tokyo", "london", "new_york"} <= set(status["sessions"]["detail"])
    office = json.loads(request(port, "/api/office/state")[2])
    assert len(office["agents"]) == 21 and len(office["rooms"]) == 10
    assert {a["visual"] for a in office["agents"]} <= {"idle", "typing", "reading", "talking", "meeting", "ticket",
                                                        "veto", "celebrating", "resting", "error", "away"}
    assert office["advisor"]["state"] and office["last_event_id"] > 0
    assert set(office["screens"]["symbols"]) == {"EURUSD", "GBPUSD", "USDJPY"}


def test_windows_follow_the_simulation_clock_and_the_wall_clock_otherwise(sim_root: Path, tmp_path: Path) -> None:
    from mt5bot.registry import Registry
    from panel.data import PanelData, StateLocator

    cfg = panel_cfg(sim_root)
    wall = 4_000_000_000.0  # far from the simulation's virtual dates
    data = PanelData(cfg, StateLocator(cfg, sim_root), clock=lambda: wall)
    health = json.loads((sim_root / "health.json").read_text())
    virtual = datetime.strptime(health["last_loop_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(
        tzinfo=timezone.utc).timestamp()
    assert data.now() == virtual and data.status()["now_utc"] == virtual
    equity = data.equity(days=30)
    assert not equity["insufficient"] and equity["summary"]["today"] is not None
    assert data.office_state()["now"] == virtual

    state = tmp_path / "state" / "DEMO_1"
    state.mkdir(parents=True)
    Registry(state / "registry.sqlite").close()
    real = PanelData(cfg, StateLocator(cfg, tmp_path, state), clock=lambda: wall)
    assert real.now() == wall


def test_detail_endpoints_and_404s(panel) -> None:
    _ctx, port = panel()
    decisions = json.loads(request(port, "/api/decisions")[2])["rows"]
    assert decisions
    dossier = json.loads(request(port, f"/api/decisions/{decisions[0]['id']}")[2])
    assert dossier["id"] == decisions[0]["id"] and dossier["opinions"]
    agent = json.loads(request(port, "/api/agents/cio")[2])
    assert agent["name"] and "messages" in agent and "weight_history" in agent
    assert request(port, "/api/agents/not_an_agent")[0] == 404
    assert request(port, "/api/decisions/does-not-exist")[0] == 404
    assert request(port, "/api/unknown")[0] == 404
    assert request(port, "/api/status", method="PUT")[0] == 405


def test_no_secret_or_identifier_in_any_response(panel, sim_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    _ctx, port = panel(token=TOKEN, controls=True)
    account_folder = next(sim_root.glob("*/office.sqlite")).parent.name
    health = json.loads((sim_root / "health.json").read_text())
    assert health["state_dir"] and health["account"]  # present in the file the panel reads...
    for path in ENDPOINTS + ("/api/csrf",):
        text = request(port, path)[2]
        for secret in (str(SIM_LOGIN), account_folder, TOKEN, API_KEY, health["account"]):
            assert secret not in text, (path, secret)  # ...and never in what it serves


def test_static_files_and_traversal(panel) -> None:
    _ctx, port = panel()
    for evil in ("/static/../app.py", "/static/%2e%2e/server.py", "/static/js/../../data.py", "/static/",
                 "/static/../../config.json"):
        assert request(port, evil)[0] == 404, evil


# ---------------------------------------------------------------------- stream


def read_events(port: int, after: Optional[int], wanted: int, timeout: float = 8.0) -> list[dict]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    headers = f"Last-Event-ID: {after}\r\n" if after is not None else ""
    sock.sendall(f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n{headers}\r\n".encode())
    buf, events = b"", []
    deadline = time.monotonic() + timeout
    try:
        while len(events) < wanted and time.monotonic() < deadline:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                for line in block.decode().splitlines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
    finally:
        sock.close()
    return events


def test_stream_delivers_new_events_and_resumes_after_reconnect(panel, sim_root: Path) -> None:
    _ctx, port = panel()
    store = OfficeStore(next(sim_root.glob("*/office.sqlite")))
    try:
        start = store.max_event_id()
        received: list[dict] = []
        reader = threading.Thread(target=lambda: received.extend(read_events(port, start, 2)))
        reader.start()
        time.sleep(0.5)
        store.insert_events([{"ts": time.time(), "topic": "agent.message", "agent": "cio", "payload": {"text": "a"}},
                             {"ts": time.time(), "topic": "risk.decision", "agent": "risk_manager",
                              "payload": {"allowed": False, "reason": "test veto"}}])
        reader.join(10)
        assert [e["topic"] for e in received] == ["agent.message", "risk.decision"]
        assert received[1]["effects"][0] == {"agent": "risk_manager", "visual": "veto", "bubble": "Veto: test veto"}
        # Reconnect with Last-Event-ID = the first event: only the second is replayed.
        again = read_events(port, received[0]["id"], 1)
        assert [e["id"] for e in again] == [received[1]["id"]]
    finally:
        store.close()


def test_stream_count_is_bounded(sim_root: Path) -> None:
    cfg = panel_cfg(sim_root, panel={"max_streams": 1})
    ctx, server, port = run_in_thread(cfg)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\n\r\n")
        sock.recv(100)
        assert request(port, "/api/stream")[0] == 503
        sock.close()
    finally:
        stop_server(ctx, server)


# ---------------------------------------------------------------------- control


def control(port: int, action: str, token: Optional[str] = TOKEN, csrf: Optional[str] = None,
            body: Optional[dict] = None, origin: Optional[str] = None) -> tuple[int, dict]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if origin:
        headers["Origin"] = origin
    status, _h, text = request(port, f"/api/control/{action}", "POST", body if body is not None else {"confirm": True},
                               headers)
    return status, json.loads(text)


def audit_rows(sim_root: Path) -> list[dict]:
    store = OfficeStore(next(sim_root.glob("*/office.sqlite")), read_only=True)
    try:
        return store.control_audit(200)
    finally:
        store.close()


def test_controls_are_disabled_by_default_and_audited(panel, sim_root: Path) -> None:
    _ctx, port = panel(token=TOKEN, controls=False)
    before = len(audit_rows(sim_root))
    assert control(port, "pause")[0] == 403
    rows = audit_rows(sim_root)
    assert len(rows) == before + 1 and rows[0]["ok"] == 0 and TOKEN not in json.dumps(rows)


def test_control_actions_need_token_csrf_origin_and_confirmation(sim_root: Path) -> None:
    ctx, server, port = run_in_thread(panel_cfg(sim_root, panel={"control_rate_per_minute": 30}), token=TOKEN,
                                      controls=True)
    try:
        _check_controls(ctx, port, sim_root)
    finally:
        stop_server(ctx, server)


def _check_controls(ctx: Any, port: int, sim_root: Path) -> None:
    state = next(sim_root.glob("*/office.sqlite")).parent
    csrf = json.loads(request(port, "/api/csrf")[2])["csrf"]
    assert control(port, "pause", token=None, csrf=csrf)[0] == 401
    assert control(port, "pause", token="wrong", csrf=csrf)[0] == 401
    assert control(port, "pause", csrf=None)[0] == 403
    assert control(port, "pause", csrf=csrf, origin="http://evil.example")[0] == 403
    assert control(port, "pause", csrf=csrf, body={})[0] == 400
    assert control(port, "launch_missiles", csrf=csrf)[0] == 404
    assert not (state / "PAUSE_ENTRIES").exists()
    status, body = control(port, "pause", csrf=csrf, origin=f"http://127.0.0.1:{port}")
    assert status == 200 and (state / "PAUSE_ENTRIES").exists()
    assert control(port, "resume", csrf=csrf)[0] == 200 and not (state / "PAUSE_ENTRIES").exists()
    assert ctx.kill_path() == sim_root / "KILL"
    assert control(port, "kill", csrf=csrf)[0] == 200 and (sim_root / "KILL").exists()
    (sim_root / "KILL").unlink()
    rows = audit_rows(sim_root)
    assert {r["action"] for r in rows if r["ok"]} >= {"pause", "resume", "kill"}
    assert sum(1 for r in rows if not r["ok"]) >= 5


def test_control_actions_are_rate_limited(sim_root: Path) -> None:
    cfg = panel_cfg(sim_root, panel={"control_rate_per_minute": 2})
    ctx, server, port = run_in_thread(cfg, token=TOKEN, controls=True)
    try:
        csrf = json.loads(request(port, "/api/csrf")[2])["csrf"]
        codes = [control(port, "resume", csrf=csrf)[0] for _ in range(4)]
        assert codes[:2] == [200, 200] and 429 in codes[2:]
    finally:
        stop_server(ctx, server)


# ----------------------------------------------------------------- remote mode


def test_remote_mode_requires_a_session_for_everything(panel) -> None:
    _ctx, port = panel(token=TOKEN, remote=True)
    assert request(port, "/api/status")[0] == 401
    assert request(port, "/")[0] == 302
    assert request(port, "/api/login", "POST", {"token": "nope"})[0] == 401
    status, headers, _ = request(port, "/api/login", "POST", {"token": TOKEN})
    assert status == 200
    cookie = headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert request(port, "/api/status", headers={"Cookie": cookie.split(";")[0]})[0] == 200
    assert request(port, "/api/status", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200


def test_login_attempts_are_rate_limited(panel) -> None:
    _ctx, port = panel(token=TOKEN, remote=True)
    codes = [request(port, "/api/login", "POST", {"token": "guess"})[0] for _ in range(12)]
    assert codes[-1] == 429


def test_remote_bind_without_token_refuses_to_start(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MT5BOT_PANEL_TOKEN", raising=False)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state_root": str(tmp_path), "log_file": str(tmp_path / "bot.log")}))
    assert panel_main(["--config", str(config), "--host", "0.0.0.0", "--port", "18765"]) == 2


def test_port_in_use_is_reported(tmp_path: Path) -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"state_root": str(tmp_path), "log_file": str(tmp_path / "bot.log")}))
        assert panel_main(["--config", str(config), "--port", str(holder.getsockname()[1])]) == 1
    finally:
        holder.close()


# ------------------------------------------------------ office state from events


def ev(topic: str, agent: Optional[str] = None, ts: float = 1000.0, cid: Optional[str] = None, **payload: Any) -> dict:
    return {"id": int(ts), "ts": ts, "topic": topic, "agent": agent, "symbol": "EURUSD", "severity": "info",
            "correlation_id": cid, "payload": payload}


def test_event_to_character_mapping() -> None:
    effects = office_mod.event_effects
    assert effects(ev("agent.state", "trend_analyst", state="analyzing", task="x", message="hi"))[0]["visual"] == "typing"
    assert effects(ev("risk.decision", "risk_manager", allowed=False, reason="limit"))[0]["visual"] == "veto"
    assert effects(ev("risk.decision", "risk_manager", allowed=True, reason="ok")) == []
    assert {e["agent"] for e in effects(ev("order.executed", "execution_trader", side="BUY", volume=0.1))} == \
        {"execution_trader", "cio"}
    win = effects(ev("trade.closed", "performance_analyst", mode="live", r=1.5, win=True))
    assert win[0]["visual"] == "celebrating"
    assert effects(ev("agent.benched", "mtf_analyst", kind="performance"))[0]["room"] == "break_room"
    assert effects(ev("advisor.state", "claude_strategist", new="OFFLINE_NO_CREDIT"))[0]["visual"] == "away"
    meeting = effects(ev("decision.started", "cio", participants=["trend_analyst", "risk_manager"]))
    assert {e["agent"] for e in meeting} == {"trend_analyst", "risk_manager", "cio"}
    assert all(e["room"] == "boardroom" for e in meeting)


def test_derive_builds_the_office_from_rows_and_events() -> None:
    rows = {"mtf_analyst": {"state": "resting", "benched": True}, "guardian": {"state": "error"},
            "trend_analyst": {"state": "analyzing"}}
    events = [ev("decision.started", "cio", 990.0, "d1", participants=["trend_analyst", "risk_manager"], side="BUY",
                 strategy="donchian", mode="full"),
              ev("opinion", "trend_analyst", 991.0, "d1", opinion={"stance": "long", "confidence": 0.7}),
              ev("risk.decision", "risk_manager", 995.0, "d1", allowed=False, reason="spread")]
    state = office_mod.derive(rows, events, 1000.0, "NO_KEY")
    by_id = {a["id"]: a for a in state["agents"]}
    assert by_id["trend_analyst"]["visual"] == "meeting" and by_id["trend_analyst"]["room"] == "boardroom"
    assert by_id["risk_manager"]["visual"] == "veto"
    assert by_id["mtf_analyst"]["visual"] == "resting" and by_id["mtf_analyst"]["room"] == "break_room"
    assert by_id["guardian"]["visual"] == "error"
    assert by_id["claude_strategist"]["visual"] == "away" and state["advisor"]["lamp"] == "grey"
    assert state["meeting"]["votes"] == [{"agent": "trend_analyst", "stance": "long", "confidence": 0.7}]
    later = office_mod.derive(rows, events, 1200.0, "ONLINE")
    assert later["meeting"] is None and {a["id"]: a for a in later["agents"]}["trend_analyst"]["visual"] == "typing"


# --------------------------------------------------------------------- isolation


def test_panel_never_imports_the_trading_loop() -> None:
    code = ("import sys; sys.path.insert(0, '.'); import panel.app, panel.server, panel.data; "
            "bad = [m for m in ('mt5bot.bot', 'mt5bot.engine', 'mt5bot.cycle', 'MetaTrader5') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(os.name == "nt", reason="uses SIGKILL")
def test_killing_the_panel_does_not_affect_the_bot(tmp_path: Path) -> None:
    from mt5bot.sim.runner import run_simulation

    root = tmp_path / "sim"
    port = free_port()
    state: dict[str, Any] = {}
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state_root": str(root), "health_file": str(root / "health.json"),
                                  "log_file": str(tmp_path / "bot.log")}))

    def on_step(step: int, bot: Any) -> None:
        if step == 3:
            state["proc"] = subprocess.Popen([sys.executable, "-m", "panel", "--config", str(config), "--port",
                                              str(port)], cwd=PROJECT, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
        if step == 12:
            for _ in range(50):
                try:
                    state["status"] = request(port, "/api/office/state")[0]
                    break
                except OSError:
                    time.sleep(0.1)
        if step == 14:
            state["proc"].send_signal(signal.SIGKILL)
            state["proc"].wait(10)
            state["killed_at"] = bot._utc_now()

    code = run_simulation(Config(), root, steps=30, speed=0, warmup_bars=600, on_step=on_step)
    assert state.get("status") == 200 and state["proc"].returncode == -signal.SIGKILL
    assert code == 0
    store = OfficeStore(next(root.glob("*/office.sqlite")), read_only=True)
    try:
        assert store.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        last = max(e["ts"] for e in store.latest_events(50))
    finally:
        store.close()
    assert last > state["killed_at"]  # the bot kept working and writing after the panel died


# ------------------------------------------------------------ supervisor keeper


class FakePanelProcess:
    def __init__(self, exit_code: Optional[int]) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.pid = 4242

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.exit_code is None and not self.terminated:
            time.sleep(min(timeout or 0.01, 0.01))
            raise subprocess.TimeoutExpired("panel", timeout or 0)
        return 0 if self.terminated else int(self.exit_code)

    def poll(self) -> Optional[int]:
        return None if self.exit_code is None and not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


def test_panel_keeper_restarts_a_crashing_panel_then_gives_up() -> None:
    from mt5bot.supervisor import PanelKeeper

    spawned: list[FakePanelProcess] = []

    def spawn(cmd: list, cwd: str) -> FakePanelProcess:
        spawned.append(FakePanelProcess(1))
        return spawned[-1]

    keeper = PanelKeeper(["python", "-m", "panel"], PROJECT, spawn=spawn, max_crashes=3, backoff_start=0.01,
                         backoff_max=0.02)
    keeper.run()  # returns once the crash breaker trips; the bot is a separate child, untouched
    assert len(spawned) == 3


def test_panel_keeper_stops_its_panel_on_shutdown() -> None:
    from mt5bot.supervisor import PanelKeeper

    child = FakePanelProcess(None)
    keeper = PanelKeeper(["python", "-m", "panel"], PROJECT, spawn=lambda cmd, cwd: child)
    keeper.start()
    time.sleep(0.1)
    keeper.stop()
    keeper.thread.join(2)
    assert child.terminated and not keeper.thread.is_alive()


def test_panel_enabled_follows_config_and_no_panel_flag(tmp_path: Path) -> None:
    from mt5bot.supervisor import panel_enabled

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"panel": {"enabled": True}}))
    assert panel_enabled(["--config", str(config)], tmp_path)
    assert not panel_enabled(["--config", str(config), "--no-panel"], tmp_path)
    config.write_text(json.dumps({"panel": {"enabled": False}}))
    assert not panel_enabled(["--config", str(config)], tmp_path)


def test_panel_code_has_no_runtime_code_generation() -> None:
    import ast
    import re

    for path in (PROJECT / "panel").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, path
    for path in (PROJECT / "panel" / "static").rglob("*"):
        if path.suffix not in (".js", ".html", ".css", ".svg"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\beval\s*\(|new\s+Function\s*\(|\.innerHTML\s*=|\.outerHTML\s*=|document\.write", text), path
        assert "http://" not in text.replace("http://www.w3.org", "") and "https://" not in text, f"{path}: no CDN"
        if path.suffix == ".html":
            assert "<script>" not in text and " style=" not in text and "onclick" not in text.lower(), path
