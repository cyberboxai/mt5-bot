"""MT5 terminal discovery and safe restart of a frozen terminal (Windows)."""

from __future__ import annotations

import glob
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

log = logging.getLogger("mt5_bot")

TERMINAL_EXE = "terminal64.exe"


def discover_terminal_paths(env: Mapping[str, str], configured: Optional[str] = None,
                            globber: Callable[[str], list[str]] = glob.glob) -> list[str]:
    """Candidate terminal64.exe paths, best first.

    Order: the configured ``mt5_path``; installations whose folder name contains
    "STARTRADER"; any other MetaTrader 5 installation under Program Files.
    ``mt5.initialize(path=...)`` launches the terminal itself when it is not running.
    """
    found: list[str] = []
    if configured:
        found.append(configured)
    roots = [env.get("ProgramFiles", r"C:\Program Files"), env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             env.get("ProgramW6432", "")]
    hits: list[str] = []
    for root in dict.fromkeys(r for r in roots if r):
        hits.extend(globber(os.path.join(root, "*", TERMINAL_EXE)))
    starts = sorted(h for h in hits if "startrader" in h.lower())
    others = sorted(h for h in hits if "startrader" not in h.lower())
    for path in starts + others:
        if path not in found:
            found.append(path)
    return found


def terminal_exe_from_info(info_path: Optional[str]) -> Optional[str]:
    """terminal_info().path is the installation folder; return the exe inside it."""
    if not info_path:
        return None
    return str(Path(info_path) / TERMINAL_EXE)


def kill_terminal(exe_path: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """Stop only the terminal process whose executable is exactly ``exe_path`` (Windows).

    The path is passed through an environment variable, never interpolated into the
    PowerShell command, so a path can never inject commands.
    """
    if os.name != "nt":  # pragma: no cover - Windows only in production
        log.warning("Terminal restart is only supported on Windows")
        return False
    script = ("Get-Process -Name terminal64 -ErrorAction SilentlyContinue | "
              "Where-Object { $_.Path -eq $env:MT5BOT_TERMINAL_PATH } | Stop-Process -Force")
    env = dict(os.environ)
    env["MT5BOT_TERMINAL_PATH"] = exe_path
    try:
        result = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                        capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("Could not stop the terminal: %s", exc)
        return False
    return result.returncode == 0


class FreezeWatchdog:
    """Decides when a frozen terminal may be restarted. Rate limited with exponential backoff."""

    def __init__(self, freeze_seconds: float, monotonic: Callable[[], float],
                 min_interval: float = 1800.0, max_interval: float = 6 * 3600.0) -> None:
        self.freeze_seconds = freeze_seconds
        self._monotonic = monotonic
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._interval = min_interval
        self._last_restart: Optional[float] = None

    def should_restart(self, stale_seconds: float, market_expected_open: bool, connected: bool,
                       all_positions_protected: bool) -> tuple[bool, str]:
        if not market_expected_open:
            return False, "market not expected to be open"
        if stale_seconds < self.freeze_seconds:
            return False, "ticks are fresh"
        if not connected:
            return False, "terminal disconnected (reconnect logic handles this)"
        if not all_positions_protected:
            return False, "a bot position has no SL/TP; not restarting until it is protected"
        now = self._monotonic()
        if self._last_restart is not None and now - self._last_restart < self._interval:
            return False, "restart rate limit"
        return True, f"no ticks for {stale_seconds / 60:.0f} minutes while the market should be open"

    def restarted(self) -> None:
        now = self._monotonic()
        if self._last_restart is not None and now - self._last_restart < self._interval * 2:
            self._interval = min(self._interval * 2, self.max_interval)
        else:
            self._interval = self.min_interval
        self._last_restart = now

