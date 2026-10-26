"""``python -m panel [--demo]``: start the CYBERBOX Trading Office panel.

Normal mode reads the state of the bot running from the same folder. ``--demo`` starts the
REAL bot code on a synthetic market in a CHILD process (``mt5_bot.py --simulate``) and
shows it, so the whole office can be explored with no MT5, no broker and no API key.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import logging
import logging.handlers
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from mt5bot.advisor.redact import RedactingFormatter
from mt5bot.config import Config, apply_overrides

from .security import PanelSecurity
from .server import PanelContext, heartbeat_loop, make_server

log = logging.getLogger("cyberbox_panel")
PROJECT_DIR = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_ERROR, EXIT_CONFIG = 0, 1, 2


def setup_logging(path: Optional[Path]) -> None:
    formatter = RedactingFormatter("%(asctime)s %(levelname)-8s %(message)s")
    log.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)  # the demo folder may not exist yet
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m panel", description="CYBERBOX Trading Office web panel")
    parser.add_argument("--config", default=str(PROJECT_DIR / "config.json"), help="path to config.json")
    parser.add_argument("--host", default=None, help="bind address (default from config: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (default from config: 8765)")
    parser.add_argument("--state-dir", default=None, help="account state folder to show (default: automatic)")
    parser.add_argument("--demo", action="store_true", help="run a synthetic simulation and show it (no MT5, no key)")
    parser.add_argument("--demo-speed", type=float, default=2.0, help="with --demo: bars per second")
    parser.add_argument("--demo-fast-forward", type=int, default=300, help="with --demo: bars to run at full speed")
    parser.add_argument("--demo-dir", default=str(PROJECT_DIR / "state_demo"), help="with --demo: state folder")
    return parser.parse_args(argv)


def start_demo(root: Path, speed: float, fast_forward: int) -> subprocess.Popen:
    root.mkdir(parents=True, exist_ok=True)
    out = open(root / "simulation_stdout.log", "a", encoding="utf-8")
    args = [sys.executable, str(PROJECT_DIR / "mt5_bot.py"), "--simulate", "--sim-state", str(root),
            "--sim-speed", str(speed), "--sim-fast-forward", str(fast_forward)]
    kwargs = {"cwd": str(PROJECT_DIR), "stdout": out, "stderr": subprocess.STDOUT}
    if os.name == "nt":  # pragma: no cover - Windows only
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(args, **kwargs)  # noqa: S603 - fixed argument list, no shell


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = Config.load(args.config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    panel = cfg.panel
    host = args.host or panel.host
    port = args.port or panel.port
    if args.host or args.port:
        cfg = dataclasses.replace(cfg, panel=dataclasses.replace(panel, host=host, port=port))
    child: Optional[subprocess.Popen] = None
    state_dir = Path(args.state_dir) if args.state_dir else None
    token = (os.environ.get(cfg.panel.token_env) or "").strip() or None
    if args.demo:
        root = Path(args.demo_dir)
        cfg = apply_overrides(cfg, {"state_root": str(root), "health_file": str(root / "health.json"),
                                    "log_file": str(root / "simulation.log"), "kill_switch_file": str(root / "KILL"),
                                    "symbols": ("EURUSD", "GBPUSD", "USDJPY"), "panel.control_actions_enabled": True})
        if token is None:
            token = secrets.token_urlsafe(18)
            print(f"DEMO: control actions use this one-time token: {token}")
    log_path = Path(cfg.log_file)
    log_path = (log_path if log_path.is_absolute() else PROJECT_DIR / log_path).with_name("panel.log")
    setup_logging(log_path)  # next to bot.log (in demo mode: inside the demo folder)
    remote = not cfg.panel.is_loopback
    if remote and not token:
        log.error("panel.host is %s (not localhost) but %s is not set. Remote access needs a token; prefer an SSH "
                  "tunnel or a VPN instead of exposing the port.", host, cfg.panel.token_env)
        return EXIT_CONFIG
    security = PanelSecurity(token, remote, cfg.panel.control_actions_enabled, cfg.panel.control_rate_per_minute)
    ctx = PanelContext(cfg, PROJECT_DIR, security, state_dir, demo=args.demo)
    try:
        server = make_server(ctx, host, port)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 10048):
            log.error("Port %d is already in use (another panel running?). Choose another with --port.", port)
        else:
            log.error("Cannot listen on %s:%d: %s", host, port, exc)
        return EXIT_ERROR
    if args.demo:
        child = start_demo(Path(args.demo_dir), args.demo_speed, args.demo_fast_forward)
        log.info("DEMO: simulation started (pid %s) in %s", child.pid, args.demo_dir)
    threading.Thread(target=heartbeat_loop, args=(ctx, port), name="panel-heartbeat", daemon=True).start()
    log.info("CYBERBOX Trading Office at http://%s:%d/ (%s, controls %s)", "127.0.0.1" if host in ("0.0.0.0", "::")
             else host, port, "remote: token required" if remote else "local only",
             "enabled" if security.controls_enabled else f"off ({security.controls_reason})")
    serve = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="panel-http",
                             daemon=True)
    serve.start()
    try:
        while serve.is_alive():
            serve.join(0.5)
            if child is not None and child.poll() is not None:
                log.info("DEMO: the simulation ended (exit code %s); the panel keeps showing its data", child.returncode)
                child = None
    except KeyboardInterrupt:
        log.info("Stopping the panel")
    finally:
        ctx.stop.set()
        server.shutdown()
        server.server_close()
        if child is not None:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
    return EXIT_OK


def run_in_thread(cfg: Config, state_dir: Optional[Path] = None, host: str = "127.0.0.1", port: int = 0,
                  token: Optional[str] = None, controls: bool = False,
                  remote: Optional[bool] = None) -> tuple[PanelContext, object, int]:
    """Start a panel server in this process (tests, embedding). Returns (context, server, port).
    ``remote=True`` applies the remote-access rules (token for every request) even on loopback."""
    cfg = dataclasses.replace(cfg, panel=dataclasses.replace(cfg.panel, host=host, port=max(port, 1024),
                                                             control_actions_enabled=controls))
    if remote is None:
        remote = host not in ("127.0.0.1", "localhost", "::1")
    security = PanelSecurity(token, remote, controls, cfg.panel.control_rate_per_minute)
    ctx = PanelContext(cfg, PROJECT_DIR, security, state_dir)
    server = make_server(ctx, host, port)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return ctx, server, server.server_address[1]


def stop_server(ctx: PanelContext, server: object) -> None:
    ctx.stop.set()
    server.shutdown()  # type: ignore[attr-defined]
    server.server_close()  # type: ignore[attr-defined]
    time.sleep(0.05)
