"""CYBERBOX Trading Office HTTP server (standard library only).

REST JSON under ``/api``, a Server-Sent Events stream at ``/api/stream`` (resumable with
``Last-Event-ID``), static files from ``panel/static`` (vendored, no CDN), and three
audited control actions. The server never imports the trading loop; a crash here cannot
affect the bot. Every response carries the secure headers from ``security``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from mt5bot.officedb import OFFICE_DB, OfficeStore

from .data import PAUSE_MARKER, PanelData, StateLocator
from .security import SECURE_HEADERS, SESSION_COOKIE, SESSION_SECONDS, PanelSecurity, same_origin

log = logging.getLogger("cyberbox_panel")

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 4096
STREAM_PING_SECONDS = 15.0
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
                 ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}
CONTROL_ACTIONS = ("pause", "resume", "kill")


class PanelContext:
    """Everything the handler needs; one per server."""

    def __init__(self, cfg: Any, project_dir: Path, security: PanelSecurity, state_dir: Optional[Path] = None,
                 clock: Callable[[], float] = time.time, demo: bool = False) -> None:
        self.cfg = cfg
        self.project_dir = Path(project_dir)
        self.security = security
        self.locator = StateLocator(cfg, project_dir, state_dir)
        self.data = PanelData(cfg, self.locator, clock)
        self.clock = clock
        self.demo = demo
        self.stop = threading.Event()
        self.streams = threading.BoundedSemaphore(cfg.panel.max_streams)
        self.started = clock()

    def kill_path(self) -> Path:
        return self.locator.resolve(self.cfg.kill_switch_file)

    def audit(self, action: str, client: str, ok: bool, detail: str) -> None:
        log.warning("Control action %s from %s: %s (%s)", action, client, "OK" if ok else "REFUSED", detail)
        state = self.locator.get()
        if state is None:
            return
        try:
            store = OfficeStore(state / OFFICE_DB)
            try:
                store.add_control_audit(action, client, ok, detail)
                if ok:
                    from mt5bot.team.events import ControlAction

                    row = {"ts": self.clock(), "topic": ControlAction.TOPIC, "agent": "guardian", "symbol": None,
                           "severity": ControlAction.SEVERITY, "correlation_id": None,
                           "payload": {"action": action, "source": "panel", "detail": detail}}
                    store.insert_events([row])
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - auditing must not crash the panel; the log line above remains
            log.exception("Could not write the control audit row")


class Handler(BaseHTTPRequestHandler):
    server_version = "CYBERBOX"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    ctx: PanelContext  # set on the subclass created by make_server

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:  # route access logs through logging, redacted
        log.debug("%s %s", self.address_string(), self.ctx.security.redactor.redact(fmt % args))

    def _headers(self, status: int, content_type: str, length: Optional[int], extra: Optional[dict] = None,
                 cache: str = "no-store") -> None:
        self.send_response(status)
        for key, value in SECURE_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, payload: Any, status: int = 200, extra: Optional[dict] = None) -> None:
        body = json.dumps(self.ctx.security.scrub(payload), default=str, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), extra)
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _query(self) -> dict[str, str]:
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _client(self) -> str:
        return str(self.client_address[0])

    # --------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urlsplit(self.path).path
        try:
            if not self.ctx.security.request_allowed(path, self.headers):
                if path.startswith("/api/"):
                    self._error(401, "authentication required")
                else:
                    self._redirect("/login")
                return
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path == "/login":
                self._static("login.html")
            elif path == "/favicon.svg":
                self._static("favicon.svg")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path == "/api/stream":
                self._stream()
            elif path.startswith("/api/"):
                self._api(path)
            else:
                self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            log.exception("panel error on GET %s", path)
            try:
                self._error(500, "internal error")
            except OSError:
                pass

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._error(413, "request too large")
                return
            raw = self.rfile.read(length) if length else b""
            if path == "/api/login":
                self._login(raw)
                return
            if not self.ctx.security.request_allowed(path, self.headers):
                self._error(401, "authentication required")
                return
            match = re.fullmatch(r"/api/control/(\w+)", path)
            if match:
                self._control(match.group(1), raw)
                return
            self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("panel error on POST %s", path)
            self._error(500, "internal error")

    def do_PUT(self) -> None:  # noqa: N802
        self._error(405, "method not allowed")

    do_DELETE = do_PATCH = do_PUT

    def _redirect(self, location: str) -> None:
        self._headers(302, "text/plain", 0, {"Location": location})

    # ---------------------------------------------------------------- static

    def _static(self, rel: str) -> None:
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():  # no path traversal, files only
            self._error(404, "not found")
            return
        ctype = CONTENT_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or \
            "application/octet-stream"
        body = target.read_bytes()
        cache = "no-cache" if target.suffix == ".html" else "public, max-age=300"
        self._headers(200, ctype, len(body), cache=cache)
        self.wfile.write(body)

    # ------------------------------------------------------------------- api

    def _api(self, path: str) -> None:
        q = self._query()
        data = self.ctx.data
        routes: dict[str, Callable[[], Any]] = {
            "/api/status": lambda: dict(data.status(), controls={
                "enabled": self.ctx.security.controls_enabled, "reason": self.ctx.security.controls_reason},
                demo=self.ctx.demo, auth_required=self.ctx.security.remote),
            "/api/office/state": data.office_state,
            "/api/equity": lambda: data.equity(_num(q.get("days"), 30.0, 1.0, 3650.0)),
            "/api/performance": data.performance,
            "/api/trades": lambda: data.trades(q.get("mode", "live"), q.get("symbol"), int(_num(q.get("limit"), 100,
                                                                                               1, 500)),
                                               int(_num(q.get("offset"), 0, 0, 1e9)), q.get("sort", "open_ts"),
                                               q.get("order", "desc"), q.get("status")),
            "/api/agents": data.agents,
            "/api/decisions": lambda: data.decisions(q.get("q"), q.get("symbol"), q.get("action"),
                                                     int(_num(q.get("limit"), 50, 1, 500)),
                                                     int(_num(q.get("offset"), 0, 0, 1e9))),
            "/api/strategies": data.strategies,
            "/api/risk": data.risk,
            "/api/markets": data.markets,
            "/api/advisor": data.advisor,
            "/api/health": data.health_view,
            "/api/team": data.team,
            "/api/events": lambda: data.events(int(_num(q.get("after"), 0, 0, 1e15)),
                                               int(_num(q.get("limit"), 200, 1, 2000)), q.get("agent") or None,
                                               q.get("symbol") or None, q.get("severity") or None),
            "/api/replay": lambda: data.replay(_num(q.get("from"), self.ctx.clock() - 86400, 0, 1e12),
                                               _num(q.get("to"), self.ctx.clock(), 0, 1e12)),
            "/api/csrf": lambda: {"csrf": self.ctx.security.csrf},
        }
        if path in routes:
            self._json(routes[path]())
            return
        match = re.fullmatch(r"/api/agents/([a-z_]{2,40})", path)
        if match:
            agent = data.agent(match.group(1))
            self._json(agent) if agent is not None else self._error(404, "unknown agent")
            return
        match = re.fullmatch(r"/api/decisions/([A-Za-z0-9._+\-]{1,80})", path)
        if match:
            dossier = data.decision(match.group(1))
            self._json(dossier) if dossier is not None else self._error(404, "unknown decision")
            return
        self._error(404, "not found")

    # ---------------------------------------------------------------- stream

    def _stream(self) -> None:
        if not self.ctx.streams.acquire(blocking=False):
            self._error(503, "too many live streams; close another tab")
            return
        try:
            last = self.headers.get("Last-Event-ID") or self._query().get("after") or ""
            after = int(last) if last.isdigit() else -1
            self._headers(200, "text/event-stream; charset=utf-8", None,
                          {"Connection": "keep-alive", "X-Accel-Buffering": "no"})
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            if after < 0:
                with self.ctx.data.office() as office:
                    after = office.max_event_id() if office is not None else 0
            last_ping = time.monotonic()
            poll = self.ctx.cfg.panel.stream_poll_ms / 1000.0
            while not self.ctx.stop.is_set():
                events = self.ctx.data.events(after, 500)
                for event in events:
                    after = max(after, int(event["id"]))
                    payload = json.dumps(self.ctx.security.scrub(event), default=str, separators=(",", ":"))
                    self.wfile.write(f"id: {event['id']}\nevent: office\ndata: {payload}\n\n".encode("utf-8"))
                if events:
                    self.wfile.flush()
                if time.monotonic() - last_ping >= STREAM_PING_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.monotonic()
                self.ctx.stop.wait(poll)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.ctx.streams.release()
            self.close_connection = True

    # ---------------------------------------------------------------- login

    def _login(self, raw: bytes) -> None:
        sec = self.ctx.security
        client = self._client()
        if not sec.auth_failures.allow(client):
            self._error(429, "too many attempts; wait a few minutes")
            return
        try:
            token = json.loads(raw or b"{}").get("token")
        except ValueError:
            token = None
        if not sec.token_ok(token):
            self._error(401, "invalid token")
            return
        sid = sec.new_session()
        cookie = f"{SESSION_COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
        self._json({"ok": True}, extra={"Set-Cookie": cookie})

    # -------------------------------------------------------------- control

    def _control(self, action: str, raw: bytes) -> None:
        sec, ctx, client = self.ctx.security, self.ctx, self._client()
        if action not in CONTROL_ACTIONS:
            self._error(404, "acção desconhecida")
            return
        if not sec.controls_enabled:
            ctx.audit(action, client, False, sec.controls_reason)
            self._error(403, sec.controls_reason)
            return
        if not sec.auth_failures.allow(f"ctl:{client}") or not sec.control_limit.allow(client) \
                or not sec.control_limit_global.allow("all"):
            ctx.audit(action, client, False, "rate limited")
            self._error(429, "demasiados pedidos; aguarde um minuto")
            return
        if not sec.token_ok(sec.bearer(self.headers)):
            ctx.audit(action, client, False, "missing or invalid token")
            self._error(401, "as acções de controlo exigem um token do painel válido")
            return
        if not sec.csrf_ok(self.headers.get("X-CSRF-Token")) or not same_origin(self.headers, self.headers.get("Host")):
            ctx.audit(action, client, False, "CSRF check failed")
            self._error(403, "falhou a verificação CSRF; recarregue a página")
            return
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            ctx.audit(action, client, False, "wrong content type")
            self._error(415, "é necessário um corpo JSON")
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if body.get("confirm") is not True:
            ctx.audit(action, client, False, "not confirmed")
            self._error(400, "confirme a acção ({\"confirm\": true})")
            return
        state = ctx.locator.get()
        if state is None:
            self._error(409, "não foi encontrada a pasta de estado da conta: o bot está a correr?")
            return
        if action == "pause":
            (state / PAUSE_MARKER).write_text("New entries paused from the CYBERBOX panel. Delete this file or use "
                                              "Resume to allow new entries again.\n", encoding="utf-8")
            detail = "novas entradas em pausa (as posições abertas mantêm o SL/TP)"
        elif action == "resume":
            marker = state / PAUSE_MARKER
            if marker.exists():
                marker.unlink()
            detail = "pausa do painel retirada (os disjuntores de risco e o Guardião não são afectados)"
        else:
            path = ctx.kill_path()
            path.write_text("Kill switch triggered from the CYBERBOX panel.\n", encoding="utf-8")
            detail = "ficheiro do kill switch criado: o bot fecha as suas posições e pára"
        ctx.audit(action, client, True, detail)
        self._json({"ok": True, "action": action, "detail": detail})


def _num(value: Optional[str], default: float, low: float, high: float) -> float:
    try:
        x = float(value) if value is not None else default
    except ValueError:
        return default
    if x != x:  # NaN
        return default
    return min(max(x, low), high)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(ctx: PanelContext, host: str, port: int) -> PanelServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    return PanelServer((host, port), handler)


def heartbeat_loop(ctx: PanelContext, port: int, every: float = 10.0) -> None:
    """Tell the Ops Engineer the panel is alive (a tiny file in the state folder)."""
    import os

    while not ctx.stop.wait(every):
        state = ctx.locator.get()
        if state is None:
            continue
        try:
            (state / "panel_heartbeat.json").write_text(json.dumps({"pid": os.getpid(), "port": port,
                                                                    "ts": ctx.clock()}), encoding="utf-8")
        except OSError:
            pass
