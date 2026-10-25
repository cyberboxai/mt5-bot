"""Optional Telegram notifications using only the standard library.

Off by default. The token and chat id come ONLY from the environment variables
``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``; they are never read from files and
never logged. Sending happens on a background thread and can never raise into the bot.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Optional

log = logging.getLogger("mt5_bot")

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3900
REPEAT_WINDOW_SECONDS = 600


class Notifier:
    def __init__(self, enabled: bool, env: Mapping[str, str], prefix: str = "mt5_bot",
                 opener: Optional[Callable[..., object]] = None, clock: Callable[[], float] = time.monotonic,
                 background: bool = True) -> None:
        self.prefix = prefix
        self._token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self._chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
        self.enabled = bool(enabled and self._token and self._chat)
        if enabled and not self.enabled:
            log.warning("telegram_enabled is true but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set; "
                        "notifications are off")
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._background = background
        self._recent: dict[str, float] = {}
        self._lock = threading.Lock()

    def send(self, text: str, key: Optional[str] = None) -> None:
        """Queue a message. ``key`` de-duplicates repeats within 10 minutes."""
        if not self.enabled:
            return
        dedup = key or text
        now = self._clock()
        with self._lock:
            last = self._recent.get(dedup)
            if last is not None and now - last < REPEAT_WINDOW_SECONDS:
                return
            self._recent[dedup] = now
        message = f"[{self.prefix}] {text}"[:MAX_LEN]
        if self._background:
            threading.Thread(target=self._post, args=(message,), daemon=True).start()
        else:
            self._post(message)

    def _post(self, message: str) -> None:
        data = urllib.parse.urlencode({"chat_id": self._chat, "text": message}).encode()
        request = urllib.request.Request(API.format(token=self._token), data=data, method="POST")
        try:
            with self._opener(request, timeout=10) as response:  # type: ignore[attr-defined]
                body = json.loads(response.read().decode("utf-8", "replace") or "{}")
                if not body.get("ok", False):
                    log.warning("Telegram rejected the message (description: %s)", body.get("description", "?"))
        except Exception as exc:  # noqa: BLE001 - notifications must never break trading
            # Never include the URL: it contains the token.
            log.warning("Telegram notification failed (%s)", type(exc).__name__)
