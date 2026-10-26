"""Panel security: token, sessions, CSRF, rate limits, response scrubbing, secure headers.

* Read-only by default. Control actions (pause, resume, kill) need: controls enabled in
  config, the token from the environment variable named by ``panel.token_env``
  (``Authorization: Bearer``), the per-process CSRF token (``X-CSRF-Token``), a JSON body,
  a same-origin request, and they are rate limited and audited.
* Bound to anything but loopback, EVERY request needs a session obtained with the token
  (HttpOnly, SameSite=Strict cookie); the panel refuses to start remotely without a token.
* ``scrub`` is applied to every JSON response: identifying keys are removed and secrets
  are redacted, whatever the data layer returns.
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any, Mapping, Optional

from mt5bot.advisor.redact import Redactor

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
SECURE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
# What the login page itself needs (brand styles, fonts, logo): public, contains no data.
PUBLIC_PATHS = ("/login", "/api/login", "/static/css/app.css", "/static/css/tokens.css", "/static/css/fonts.css",
                "/static/js/login.js", "/favicon.ico")
PUBLIC_PREFIXES = ("/static/fonts/", "/static/img/")
SESSION_COOKIE = "cbx_session"
SESSION_SECONDS = 12 * 3600
# Keys that must never leave the panel, wherever they appear in a payload.
FORBIDDEN_KEYS = re.compile(r"^(account|server|login|state_dir|mt5_path|terminal_path|path)$"
                            r"|(^|_)(token|api_?key|password|passwd|secret|login|credentials?)$", re.IGNORECASE)


class RateLimiter:
    """Sliding-window limiter per key."""

    def __init__(self, limit: int, window_seconds: float, clock: Any = time.monotonic) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


class PanelSecurity:
    def __init__(self, token: Optional[str], remote: bool, controls_enabled: bool, control_rate_per_minute: int,
                 clock: Any = time.monotonic) -> None:
        self.token = token or None
        self.remote = remote
        self.controls_enabled = bool(controls_enabled and self.token)
        self.controls_reason = ("" if self.controls_enabled else
                                "acções de controlo desligadas na configuração (panel.control_actions_enabled)"
                                if not controls_enabled else "não há token do painel definido no ambiente")
        self.csrf = secrets.token_urlsafe(32)
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()
        self.control_limit = RateLimiter(control_rate_per_minute, 60.0, clock)
        self.control_limit_global = RateLimiter(max(control_rate_per_minute * 2, 2), 60.0, clock)
        self.auth_failures = RateLimiter(10, 600.0, clock)
        self.redactor = Redactor()
        if self.token:
            self.redactor.add_secret(self.token)

    # ------------------------------------------------------------------ tokens

    def token_ok(self, presented: Optional[str]) -> bool:
        if not self.token or not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self.token.encode("utf-8"))

    @staticmethod
    def bearer(headers: Mapping[str, str]) -> Optional[str]:
        value = headers.get("Authorization") or headers.get("authorization") or ""
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return None

    def csrf_ok(self, presented: Optional[str]) -> bool:
        return bool(presented) and hmac.compare_digest(str(presented).encode(), self.csrf.encode())

    # ---------------------------------------------------------------- sessions

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            self._sessions[sid] = now + SESSION_SECONDS
        return sid

    def session_ok(self, cookie_header: Optional[str]) -> bool:
        sid = parse_cookie(cookie_header or "").get(SESSION_COOKIE)
        if not sid:
            return False
        with self._lock:
            expiry = self._sessions.get(sid)
            return expiry is not None and expiry > self._clock()

    def request_allowed(self, path: str, headers: Mapping[str, str]) -> bool:
        """Remote mode: everything except the login page needs a session or the bearer token."""
        if not self.remote:
            return True
        if path in PUBLIC_PATHS or (path.startswith(PUBLIC_PREFIXES) and ".." not in path):
            return True
        return self.session_ok(headers.get("Cookie")) or self.token_ok(self.bearer(headers))

    # --------------------------------------------------------------- scrubbing

    def scrub(self, value: Any, depth: int = 0) -> Any:
        if depth > 40:
            return None
        if isinstance(value, dict):
            return {k: self.scrub(v, depth + 1) for k, v in value.items()
                    if not (isinstance(k, str) and FORBIDDEN_KEYS.search(k))}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v, depth + 1) for v in value]
        if isinstance(value, str):
            return self.redactor.redact(value)
        return value


def parse_cookie(header: str) -> dict[str, str]:
    out = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def same_origin(headers: Mapping[str, str], host_header: Optional[str]) -> bool:
    """Reject cross-site requests: Origin (or Referer) must match the Host the browser used."""
    origin = headers.get("Origin") or headers.get("Referer")
    if not origin:
        return True  # non-browser clients (curl, tests); CSRF token and bearer token still apply
    if not host_header:
        return False
    match = re.match(r"^https?://([^/]+)", origin)
    return bool(match) and match.group(1).lower() == host_header.lower()
