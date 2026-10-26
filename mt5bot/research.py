"""Quant Research department: walk-forward research that turns history into arm priors.

The live process refreshes the market data cache, then launches a SEPARATE low-priority
process (``--research-once --offline``) so research never blocks the trading loop. For every
symbol and timeframe of the universe it runs the walk-forward procedure of every strategy
family (``optimizer``) and writes, through the registry:

* a ``research`` arm per family with the parameters chosen on the latest training window,
  whose prior is the procedure's out-of-sample R series (count, mean, variance);
* the prior of the family's ``default``-parameter arm, from the same out-of-sample span;
* one ``research_results`` row per family with the quality flags (deflated Sharpe, profit
  factor, window stability, neighbour robustness...). Flags are information, not gates.

Research runs at startup (quick mode) so priors exist within minutes, then weekly, and on
demand. A failed run is retried after ``research.retry_minutes`` (v3.0 waited a full week).
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import numpy as np

from . import strategies as strategy_lib
from .backtest import CostModel, simulate
from .config import TIMEFRAME_SECONDS
from .limits import WIDEST_TRADING_HOURS
from .optimizer import OptimizerSettings, hypothesis_params, optimize_symbol
from .registry import Registry, _json
from .risk import SymbolSpec

log = logging.getLogger("mt5_bot")

LOCK_NAME = "research.lock"
LOCK_STALE_SECONDS = 12 * 3600
MONTH = int(30.4375 * 86400)
MIN_RESEARCH_BARS = 600


def cost_model(cfg: Any, commission_per_lot: float, symbol: Optional[str] = None) -> CostModel:
    """Costs and entry filters shared by research and the shadow books.

    Evidence is gathered over the widest tradable hours (the live window may be narrower:
    signals outside it are counterfactuals that tell the Flow Doctor what the window costs)."""
    cap = cfg.universe.spread_cap(symbol) if symbol else None
    return CostModel(commission_per_lot=commission_per_lot, slippage_points=cfg.backtest_slippage_points,
                     max_spread_points=cap if cap is not None else 1e9, trading_hours=WIDEST_TRADING_HOURS,
                     close_on_opposite=cfg.close_on_opposite, max_spread_to_stop=cfg.universe.max_spread_to_stop)


class ResearchLock:
    """Exclusive lock file so two research processes never run at once for one account."""

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

    def __enter__(self) -> "ResearchLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def universe_from_registry(cfg: Any, registry: Registry) -> tuple[list[str], list[str]]:
    """(broker symbol names, timeframes) the live process published for research."""
    raw = registry.get_kv("universe")
    symbols: list[str] = []
    if raw:
        try:
            symbols = list(json.loads(raw).get("symbols") or [])
        except ValueError:
            symbols = []
    if not symbols:
        mapping = json.loads(registry.get_kv("resolved_symbols") or "{}")
        symbols = [mapping.get(s, s) for s in cfg.symbols]
    return symbols, list(cfg.arm_timeframes())


def research_settings(cfg: Any, timeframe: str, mode: str) -> OptimizerSettings:
    trials = cfg.research.startup_trials if mode == "startup" else cfg.trials_per_strategy
    return OptimizerSettings(cfg.wf_train_months, cfg.wf_test_months, cfg.wf_step_months, trials,
                             cfg.max_trials_per_cycle, cfg.optimizer_seed, cfg.research.reference_risk_pct,
                             cfg.research.reference_equity, TIMEFRAME_SECONDS[timeframe])


def fixed_params_prior(bars: np.ndarray, strategy: str, params: dict, spec: SymbolSpec, costs: CostModel,
                       equity: float, risk_pct: float, start_time: Optional[int]) -> tuple[int, Optional[float],
                                                                                           Optional[float]]:
    """(n, mean R, var R) of fixed parameters over the out-of-sample span. Fixed parameters were
    not selected on this data, so this is an honest (if small) piece of evidence."""
    strat = strategy_lib.get(strategy)
    start = int(np.searchsorted(bars["time"], start_time)) if start_time else len(bars) // 3
    trades = simulate(bars, strat.signals(bars, params), spec, costs, equity, risk_pct, start=max(start, 1),
                      compound=False)
    rs = np.array([t.r for t in trades], dtype=float)
    if rs.size == 0:
        return 0, None, None
    return int(rs.size), float(rs.mean()), float(rs.var(ddof=1)) if rs.size > 1 else None


def run_research(cfg: Any, registry: Registry, trigger: str, mode: str = "full",
                 symbols: Optional[Sequence[str]] = None, timeframes: Optional[Sequence[str]] = None,
                 workers: Optional[int] = None, lower_priority: bool = False,
                 clock: Callable[[], float] = time.time) -> list[dict[str, Any]]:
    """Research every symbol x timeframe from the data cache. Never raises for one bad pair."""
    from .arms import ArmStore

    uni_symbols, uni_tfs = universe_from_registry(cfg, registry)
    symbols = list(symbols) if symbols is not None else uni_symbols
    timeframes = list(timeframes) if timeframes is not None else uni_tfs
    hypotheses = json.loads(registry.get_kv("research_hypotheses") or "[]")
    extra = hypothesis_params(hypotheses)
    store = ArmStore(registry, clock)
    months = cfg.research.startup_history_months if mode == "startup" else cfg.history_months
    out: list[dict[str, Any]] = []
    ok = 0
    for symbol in symbols:
        spec_row = registry.load_spec(symbol)
        for tf in timeframes:
            run_id = registry.start_run(f"{symbol} {tf}", trigger)
            try:
                if spec_row is None:
                    raise RuntimeError(f"no cached symbol specification for {symbol}; the live bot stores it")
                spec = SymbolSpec.from_json(spec_row[0])
                bars = registry.load_bars(symbol, tf)
                if len(bars):
                    bars = bars[bars["time"] >= int(bars["time"][-1]) - months * MONTH]
                if len(bars) < MIN_RESEARCH_BARS:
                    raise RuntimeError(f"only {len(bars)} cached {tf} bars for {symbol} (need {MIN_RESEARCH_BARS}); "
                                       "check Tools > Options > Charts > Max bars in chart")
                costs = cost_model(cfg, spec_row[1], symbol)
                settings = research_settings(cfg, tf, mode)
                result = optimize_symbol(symbol, bars, spec, costs, settings, cfg.strategy_names(),
                                         workers or cfg.optimizer_workers, lower_priority, extra_params=extra)
                registry.finish_run(run_id, result)
                arms = apply_priors(registry, store, symbol, tf, result, bars, spec, costs, settings, run_id, clock)
                ok += 1
                out.append({"symbol": symbol, "timeframe": tf, "run_id": run_id, "status": "done",
                            "families": len(result.candidates), "arms_with_prior": arms,
                            "seconds": round(result.seconds, 1)})
                log.info("%s %s: research run %d finished in %.0fs (%d families, %d arm priors)", symbol, tf, run_id,
                         result.seconds, len(result.candidates), arms)
            except Exception as exc:  # noqa: BLE001 - record and continue with the other pairs
                log.warning("%s %s: research run %d failed: %s", symbol, tf, run_id, exc)
                registry.fail_run(run_id, f"{type(exc).__name__}: {exc}")
                out.append({"symbol": symbol, "timeframe": tf, "run_id": run_id, "status": "failed",
                            "error": str(exc)[:300]})
    if hypotheses:
        registry.set_kv("research_hypotheses", "[]")
        registry.log_event("*", "research", f"{len(hypotheses)} research hypothesis(es) evaluated through the normal "
                           "walk-forward and evidence pipeline", {"hypotheses": hypotheses[:20]})
    now = clock()
    registry.set_kv("last_research_attempt", str(now))
    registry.set_kv("last_research_status", json.dumps({"ok": ok, "failed": len(out) - ok, "mode": mode,
                                                        "trigger": trigger[:200]}))
    if ok:
        registry.set_kv("last_research_finished", str(now))
        if mode == "full":
            registry.set_kv("last_full_research", str(now))
    return out


def apply_priors(registry: Registry, store: Any, symbol: str, timeframe: str, result: Any, bars: np.ndarray,
                 spec: SymbolSpec, costs: CostModel, settings: OptimizerSettings, run_id: int,
                 clock: Callable[[], float]) -> int:
    """Research arms and priors from one walk-forward result."""
    count = 0
    now = clock()
    for cand in result.candidates:
        m = cand.metrics
        n = int(m.get("trades") or 0)
        mean, var = m.get("oos_r_mean"), m.get("oos_r_var")
        ps = registry.param_set_id(cand.strategy, cand.params)
        registry.conn.execute(
            "INSERT INTO research_results (run_id, ts, symbol, timeframe, strategy, param_set_id, kind, n_oos, mean_r,"
            " var_r, metrics_json, flags_json) VALUES (?, ?, ?, ?, ?, ?, 'walk_forward', ?, ?, ?, ?, ?)",
            (run_id, now, symbol, timeframe, cand.strategy, ps, n, mean, var,
             _json({k: v for k, v in m.items() if not isinstance(v, list)}), _json(cand.reasons)))
        if n > 0 and mean is not None and math.isfinite(mean) and cand.params:
            arm = store.upsert(symbol, timeframe, cand.strategy, cand.params, "research")
            store.set_prior(arm.id, n, float(mean), float(var) if var is not None else None)
            count += 1
        strat = strategy_lib.get(cand.strategy)
        default = strat.default_params()
        n_d, mean_d, var_d = fixed_params_prior(bars, cand.strategy, default, spec, costs, settings.equity,
                                                settings.risk_pct, m.get("oos_start_time"))
        row = registry.conn.execute("SELECT id FROM arms WHERE symbol = ? AND timeframe = ? AND param_set_id = ?",
                                    (symbol, timeframe, registry.param_set_id(cand.strategy, default))).fetchone()
        if row is not None and n_d > 0 and mean_d is not None:
            store.set_prior(int(row["id"]), n_d, mean_d, var_d)
            count += 1
    return count


class ResearchScheduler:
    """Decides when to launch a background research process, and launches it."""

    def __init__(self, cfg: Any, registry: Registry, state_dir: Path, config_path: str,
                 refresh_data: Callable[[str], bool], clock: Callable[[], float] = time.time,
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
        self._last_attempt = -math.inf
        self.last_reason: Optional[str] = None

    def request(self, symbol: str, reason: str) -> None:
        self.demand = f"{symbol}: {reason}"

    def running(self) -> bool:
        if self.process is None:
            return False
        code = self.process.poll()
        if code is None:
            return True
        log.info("Research process finished with exit code %s", code)
        self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return False

    def due(self, weekend: bool) -> Optional[tuple[str, str]]:
        """(reason, mode) to start research now, or None."""
        if self.demand:
            return self.demand, "full"
        now = self.clock()
        finished = self.registry.get_kv("last_research_finished")
        attempt = self.registry.get_kv("last_research_attempt")
        retry = self.cfg.research.retry_minutes * 60
        if finished is None:
            if attempt is not None and now - float(attempt) < retry:
                return None
            return ("cold start: no research results yet" if attempt is None
                    else "retry: the previous research run produced no result"), "startup"
        full = self.registry.get_kv("last_full_research")
        age_days = (now - float(full if full is not None else finished)) / 86400
        if full is None and age_days >= 1.0 and weekend:
            return "first full research (market closed)", "full"
        if age_days >= self.cfg.reoptimize_every_days and weekend:
            return "weekly research (market closed)", "full"
        if age_days >= self.cfg.reoptimize_every_days + 3:
            return "weekly research overdue (no weekend window seen)", "full"
        return None

    def maybe_launch(self, weekend: bool) -> Optional[str]:
        if self.running():
            return None
        due = self.due(weekend)
        if due is None or self.clock() - self._last_attempt < 600:
            return None
        reason, mode = due
        self._last_attempt = self.clock()
        months = self.cfg.research.startup_history_months if mode == "startup" else self.cfg.history_months
        if not self.refresh_data(mode):
            log.warning("Market data refresh failed; research postponed (%s)", reason)
            self.registry.set_kv("last_research_attempt", str(self.clock()))
            return None
        args = [sys.executable, self.script, "--research-once", "--offline", "--config", self.config_path,
                "--state-dir", str(self.state_dir), "--trigger", reason[:200], "--research-mode", mode]
        kwargs: dict[str, Any] = {"cwd": str(Path(self.config_path).resolve().parent)}
        self._log_handle = open(self.state_dir / "research.log", "a", encoding="utf-8")
        kwargs["stdout"] = self._log_handle
        kwargs["stderr"] = subprocess.STDOUT
        if os.name == "nt":  # pragma: no cover - Windows only
            kwargs["creationflags"] = 0x00004000 | 0x08000000  # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
        try:
            self.process = self.spawn(args, **kwargs)
        except OSError as exc:
            log.error("Could not start the research process: %s", exc)
            self._log_handle.close()
            self._log_handle = None
            return None
        self.demand = None
        self.last_reason = reason
        log.info("Research process started (low priority, %s mode, %d months): %s", mode, months, reason)
        return reason
