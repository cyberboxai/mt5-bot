"""Secret redaction for every log path, stored prompt and stored error text.

``RedactingFormatter`` redacts the FINAL formatted record, so messages, arguments and
exception tracebacks are all covered. Values of known secret environment variables are
redacted literally, and anything shaped like an Anthropic key or a Telegram bot URL token
is redacted by pattern even when it never came from the environment.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Iterable, Mapping, Optional

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 4

SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "MT5_PASSWORD",
                   "MT5BOT_PANEL_TOKEN")

PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\b(x-api-key|authorization|api[_-]?key|token)(\s*[:=]\s*)((?:bearer\s+)?[^\s,;'\"]{6,})"),
    re.compile(r"(?i)\b(bearer)(\s+)([^\s,;'\"]{6,})"),
    re.compile(r"\bbot\d{5,}:[A-Za-z0-9_\-]{20,}"),
)


class Redactor:
    """Thread-safe set of literal secrets plus the shape patterns above."""

    def __init__(self, env: Optional[Mapping[str, str]] = None, names: Iterable[str] = SECRET_ENV_VARS) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self.refresh(env if env is not None else os.environ, names)

    def refresh(self, env: Mapping[str, str], names: Iterable[str] = SECRET_ENV_VARS) -> None:
        for name in names:
            self.add_secret(env.get(name, ""))

    def add_secret(self, value: Optional[str]) -> None:
        if value and len(value.strip()) >= MIN_SECRET_LENGTH:
            with self._lock:
                self._secrets.add(value.strip())

    def redact(self, text: object) -> str:
        out = str(text)
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in out:
                out = out.replace(secret, REDACTED)
        for pattern in PATTERNS:
            if pattern.groups >= 3:
                out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
            else:
                out = pattern.sub(REDACTED, out)
        return out


# One process-wide redactor; ``setup_logging`` installs it on every handler.
REDACTOR = Redactor()


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: Optional[str] = None, redactor: Redactor = REDACTOR) -> None:
        super().__init__(fmt)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact(super().format(record))
