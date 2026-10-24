"""Supervisor: runs the bot as a child process and restarts it after crashes.

* exit 0 (Ctrl+C, kill switch) and exit 3 (safety stop, real money not confirmed,
  account policy) are deliberate: the supervisor stops too;
* any other exit is a crash: restart with exponential backoff (``--no-countdown`` added);
* more than ``max_crashes`` within ``window_seconds`` trips the restart circuit breaker:
  notify and stop instead of restarting in a tight loop.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .notify import Notifier

log = logging.getLogger("mt5_supervisor")

EXIT_OK = 0
EXIT_SAFETY_STOP = 3
EXIT_BREAKER = 4
NO_RESTART_CODES = {EXIT_OK, EXIT_SAFETY_STOP}


class Supervisor:
    def __init__(self, command: Sequence[str], spawn: Callable[..., Any] = subprocess.Popen,
                 sleep: Optional[Callable[[float], None]] = None, clock: Callable[[], float] = time.monotonic,
                 notify: Callable[[str], None] = lambda text: None, max_crashes: int = 5,
                 window_seconds: float = 1800.0, backoff_start: float = 15.0, backoff_max: float = 300.0,
                 stable_seconds: float = 600.0, stop_grace_seconds: float = 30.0) -> None:
        self.command = list(command)
        self.spawn = spawn
        self.clock = clock
        self.notify = notify
        self.max_crashes = max_crashes
        self.window_seconds = window_seconds
        self.backoff_start = backoff_start
        self.backoff_max = backoff_max
        self.stable_seconds = stable_seconds
        self.stop_grace_seconds = stop_grace_seconds
        self.crashes: deque[float] = deque()
        self.starts = 0
        self._stop = threading.Event()
        self._sleep = sleep or self._interruptible_sleep
        self.child: Any = None

    def request_stop(self, *_args: Any) -> None:
        self._stop.set()

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(min(1.0, deadline - time.monotonic()))

    def _wait_child(self) -> int:
        while True:
            try:
                return self.child.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                if self._stop.is_set():
                    # Ctrl+C reaches the child through the shared console; give it time to exit cleanly.
                    try:
                        return self.child.wait(timeout=self.stop_grace_seconds)
                    except subprocess.TimeoutExpired:
                        log.warning("Bot did not stop within %.0fs; terminating it", self.stop_grace_seconds)
                        self.child.terminate()
                        return self.child.wait()

    def run(self) -> int:
        backoff = self.backoff_start
        while not self._stop.is_set():
            args = list(self.command)
            if self.starts > 0 and "--no-countdown" not in args:
                args.append("--no-countdown")
            self.starts += 1
            started = self.clock()
            log.info("Starting bot (start #%d): %s", self.starts, " ".join(args[1:]))
            try:
                self.child = self.spawn(args)
            except OSError as exc:
                log.error("Could not start the bot: %s", exc)
                code = -1
            else:
                code = self._wait_child()
            ran = self.clock() - started
            if self._stop.is_set():
                log.info("Supervisor stop requested; bot exited with code %s", code)
                return EXIT_OK
            if code in NO_RESTART_CODES:
                log.info("Bot exited deliberately with code %s; supervisor stops (no restart).", code)
                if code == EXIT_SAFETY_STOP:
                    self.notify("Bot stopped by a safety rule (exit code 3); not restarting. Check bot.log.")
                return code
            now = self.clock()
            self.crashes.append(now)
            while self.crashes and now - self.crashes[0] > self.window_seconds:
                self.crashes.popleft()
            if len(self.crashes) >= self.max_crashes:
                text = (f"Restart circuit breaker: {len(self.crashes)} crashes within "
                        f"{self.window_seconds / 60:.0f} minutes (last exit code {code}). Supervisor stopped; "
                        "positions keep their server-side SL/TP. Check bot.log and restart manually.")
                log.critical(text)
                self.notify(text)
                return EXIT_BREAKER
            if ran >= self.stable_seconds:
                backoff = self.backoff_start
            log.warning("Bot exited with code %s after %.0fs; restarting in %.0fs", code, ran, backoff)
            if len(self.crashes) == 2:
                self.notify(f"Bot crashed repeatedly (exit code {code}); restarting with backoff.")
            self._sleep(backoff)
            backoff = min(backoff * 2, self.backoff_max)
        return EXIT_OK


def setup_logging(path: Path) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(formatter)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    log.handlers[:] = [handler, console]
    log.setLevel(logging.INFO)
    log.propagate = False


def telegram_enabled(argv: Sequence[str], project_dir: Path) -> bool:
    """Follow the bot's own ``telegram_enabled`` setting (off by default, off if config is unreadable)."""
    from .config import Config

    path = Path(argv[argv.index("--config") + 1]) if "--config" in argv[:-1] else project_dir / "config.json"
    if not path.is_absolute():
        path = project_dir / path
    try:
        return Config.load(path).telegram_enabled
    except ValueError:
        return False


def main(argv: Optional[list[str]] = None, project_dir: Optional[Path] = None) -> int:  # pragma: no cover - CLI
    import os

    argv = list(sys.argv[1:] if argv is None else argv)
    project_dir = project_dir or Path(__file__).resolve().parent.parent
    setup_logging(project_dir / "supervisor.log")
    notifier = Notifier(telegram_enabled(argv, project_dir), os.environ, prefix="mt5_supervisor")
    command = [sys.executable, str(project_dir / "mt5_bot.py"), *argv]
    sup = Supervisor(command, notify=notifier.send)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, sup.request_stop)
            except (OSError, ValueError, RuntimeError):
                pass
    log.info("Supervisor started in %s", project_dir)
    code = sup.run()
    log.info("Supervisor exiting with code %s", code)
    return code
