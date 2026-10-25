"""Optimisation cycles: run from the cached market data, and schedule them from the live bot.

The live process refreshes the SQLite data cache, then launches a SEPARATE low-priority
process (``--optimize-once --offline``) so optimisation never blocks the trading loop.
Results reach the live process through the registry.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .backtest import CostModel
from .optimizer import OptimizationResult, OptimizerSettings, hypothesis_params, optimize_symbol
from .registry import Registry
from .risk import SymbolSpec

log = logging.getLogger("mt5_bot")

LOCK_NAME = "optimizer.lock"
LOCK_STALE_SECONDS = 12 * 3600
MONTH = int(30.4375 * 86400)


def cost_model(cfg: Any, commission_per_lot: float) -> CostModel:
    return CostModel(commission_per_lot=commission_per_lot, slippage_points=cfg.backtest_slippage_points,
                     max_spread_points=cfg.max_spread_points, trading_hours=cfg.trading_hours_server,
                     close_on_opposite=cfg.close_on_opposite)


class CycleLock:
    """Exclusive lock file so two optimisation cycles never run at once for one account."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / LOCK_NAME
        self._held = False

    def acquire(self) -> bool:
        try:
            if self.path.exists() and time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                self.path.unlink()
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False

    def __enter__(self) -> "CycleLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def resolved_symbols(cfg: Any, registry: Registry) -> list[str]:
    raw = registry.get_kv("resolved_symbols")
    mapping = json.loads(raw) if raw else {}
    return [mapping.get(s, s) for s in cfg.symbols]


def run_cycle(cfg: Any, registry: Registry, trigger: str, symbols: Optional[Sequence[str]] = None,
              workers: Optional[int] = None, lower_priority: bool = False) -> list[tuple[str, int, Optional[OptimizationResult]]]:
    """Optimise every symbol from the data cache. Never raises for one bad symbol."""
    symbols = list(symbols) if symbols is not None else resolved_symbols(cfg, registry)
    is_real = registry.get_kv("is_real") == "1"
    risk_pct = cfg.risk_per_trade_pct(is_real)
    equity = float(registry.get_kv("equity") or 10_000.0)
    settings = OptimizerSettings.from_config(cfg, risk_pct, equity)
    hypotheses = json.loads(registry.get_kv("research_hypotheses") or "[]")
    extra = hypothesis_params(hypotheses)
    out: list[tuple[str, int, Optional[OptimizationResult]]] = []
    for symbol in symbols:
        run_id = registry.start_run(symbol, trigger)
        try:
            spec_row = registry.load_spec(symbol)
            if spec_row is None:
                raise RuntimeError(f"no cached symbol specification for {symbol}; start the bot once online")
            spec = SymbolSpec.from_json(spec_row[0])
            bars = registry.load_bars(symbol, cfg.timeframe)
            if len(bars):
                bars = bars[bars["time"] >= int(bars["time"][-1]) - cfg.history_months * MONTH]
            needed = (cfg.wf_train_months + cfg.wf_test_months) * MONTH
            if len(bars) < 500 or int(bars["time"][-1]) - int(bars["time"][0]) < needed:
                raise RuntimeError(f"only {len(bars)} cached bars for {symbol}; not enough history for walk-forward")
            result = optimize_symbol(symbol, bars, spec, cost_model(cfg, spec_row[1]), settings,
                                     cfg.strategy_names(), workers or cfg.optimizer_workers, lower_priority,
                                     extra_params=extra)
            registry.finish_run(run_id, result)
            best = result.best
            log.info("%s: optimisation run %d finished in %.0fs, %d trials, best: %s", symbol, run_id,
                     result.seconds, result.trials,
                     f"{best.strategy} score {best.score:.3f}" if best else "NoTrade (no candidate passed the gates)")
            out.append((symbol, run_id, result))
        except Exception as exc:  # noqa: BLE001 - record and continue with the other symbols
            log.exception("%s: optimisation run %d failed", symbol, run_id)
            registry.fail_run(run_id, f"{type(exc).__name__}: {exc}")
            out.append((symbol, run_id, None))
    if hypotheses:
        registry.set_kv("research_hypotheses", "[]")
        for symbol, run_id, _result in out:
            registry.log_event(symbol, "research", f"{len(hypotheses)} research hypothesis(es) evaluated in run "
                               f"{run_id} through the normal walk-forward gates",
                               {"hypotheses": hypotheses[:20], "run_id": run_id})
    registry.set_kv("last_cycle_finished", str(registry.now()))
    return out


class OptimizerScheduler:
    """Decides when to launch a background optimisation process, and launches it."""

    def __init__(self, cfg: Any, registry: Registry, state_dir: Path, config_path: str,
                 refresh_data: Callable[[], bool], clock: Callable[[], float] = time.time,
                 spawn: Optional[Callable[..., Any]] = None, script: Optional[str] = None) -> None:
        self.cfg = cfg
        self.registry = registry
        self.state_dir = Path(state_dir)
        self.config_path = config_path
        self.refresh_data = refresh_data
        self.clock = clock
        self.spawn = spawn or subprocess.Popen
        self.script = script or str(Path(__file__).resolve().parent.parent / "mt5_bot.py")
        self.process: Any = None
        self.demand: Optional[str] = None
        self._log_handle: Any = None
        self._last_attempt = 0.0

    def request(self, symbol: str, reason: str) -> None:
        self.demand = f"{symbol}: {reason}"

    def running(self) -> bool:
        if self.process is None:
            return False
        code = self.process.poll()
        if code is None:
            return True
        log.info("Optimisation process finished with exit code %s", code)
        self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return False

    def due(self, weekend: bool) -> Optional[str]:
        """Reason to start a cycle now, or None."""
        if self.demand:
            return self.demand
        last = self.registry.get_kv("last_cycle_finished")
        if last is None:
            return "cold start: no optimisation results yet"
        age_days = (self.clock() - float(last)) / 86400
        if age_days >= self.cfg.reoptimize_every_days and weekend:
            return "weekly cycle (market closed)"
        if age_days >= self.cfg.reoptimize_every_days + 3:
            return "weekly cycle overdue (no weekend window seen)"
        return None

    def maybe_launch(self, weekend: bool) -> Optional[str]:
        if self.running():
            return None
        reason = self.due(weekend)
        if reason is None or self.clock() - self._last_attempt < 600:
            return None
        self._last_attempt = self.clock()
        if not self.refresh_data():
            log.warning("Market data refresh failed; optimisation postponed (%s)", reason)
            return None
        args = [sys.executable, self.script, "--optimize-once", "--offline", "--config", self.config_path,
                "--state-dir", str(self.state_dir), "--trigger", reason[:200]]
        kwargs: dict[str, Any] = {"cwd": str(Path(self.config_path).resolve().parent)}
        self._log_handle = open(self.state_dir / "optimizer.log", "a", encoding="utf-8")
        kwargs["stdout"] = self._log_handle
        kwargs["stderr"] = subprocess.STDOUT
        if os.name == "nt":  # pragma: no cover - Windows only
            kwargs["creationflags"] = 0x00004000 | 0x08000000  # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
        try:
            self.process = self.spawn(args, **kwargs)
        except OSError as exc:
            log.error("Could not start the optimisation process: %s", exc)
            self._log_handle.close()
            self._log_handle = None
            return None
        self.demand = None
        log.info("Optimisation process started (low priority): %s", reason)
        return reason
