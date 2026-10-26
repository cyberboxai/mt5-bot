"""Walk-forward research with anti-overfitting quality flags (Quant Research department).

For each strategy family:

1. draw a bounded random sample of parameter sets from its grid (seeded);
2. for every walk-forward window, choose the parameter set with the best TRAIN score and
   trade it on the following unseen TEST window (if nothing scores > 0 on train, the
   procedure stands aside for that window);
3. aggregate every out-of-sample window and apply the gates;
4. the parameters proposed for live trading are the best on the most recent training
   window, and their grid neighbours on that window must also be acceptable.

Candidates are ranked by a composite score that includes the deflated Sharpe ratio for
the total number of trials. Since v3.1 the gates are QUALITY FLAGS: they are reported (panel,
``--backtest``) but do not decide whether anything trades. The out-of-sample R series of each
family becomes a discounted prior of its arm (see ``research`` and ``evidence``); live and
shadow evidence then decide the arm's tier.
"""

from __future__ import annotations

import logging
import math
import multiprocessing
import os
import time
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from . import strategies as strategy_lib
from .backtest import CostModel, PreparedBars, Trade, compute_metrics, profit_factor, simulate
from .risk import SymbolSpec
from .stats import bootstrap_mean_interval, composite_score, deflated_sharpe
from .strategies.base import Strategy

log = logging.getLogger("mt5_bot")

MONTH_SECONDS = int(30.4375 * 86400)
MIN_TRAIN_TRADES = 15
MIN_ADAPTIVE_SPAN_MONTHS = 0.75  # below about three weeks of bars there is no meaningful walk-forward
SIM_EQUITY = 10_000.0


@dataclass(frozen=True)
class Gates:
    min_oos_trades: int = 100
    min_profit_factor: float = 1.3
    max_oos_drawdown_r: float = 12.0
    min_window_win_ratio: float = 0.6
    bootstrap_confidence: float = 0.90
    bootstrap_samples: int = 2000
    min_deflated_sharpe: float = 0.90
    min_neighbor_pass_ratio: float = 0.6

    @classmethod
    def from_config(cls, cfg: Any) -> "Gates":
        """Flags use fixed reference thresholds (the v3.0 values); they no longer gate trading."""
        return cls()


@dataclass(frozen=True)
class OptimizerSettings:
    train_months: float = 3
    test_months: float = 1
    step_months: float = 1
    trials_per_strategy: int = 30
    max_trials_per_cycle: int = 300
    seed: int = 12345
    risk_pct: float = 0.5
    equity: float = SIM_EQUITY
    bar_seconds: int = 900
    gates: Gates = field(default_factory=Gates)

    @classmethod
    def from_config(cls, cfg: Any, risk_pct: float, equity: float = SIM_EQUITY) -> "OptimizerSettings":
        return cls(cfg.wf_train_months, cfg.wf_test_months, cfg.wf_step_months, cfg.trials_per_strategy,
                   cfg.max_trials_per_cycle, cfg.optimizer_seed, risk_pct, equity, cfg.bar_seconds,
                   Gates.from_config(cfg))


@dataclass
class CandidateResult:
    strategy: str
    params: dict
    passed: bool
    reasons: list[str]
    metrics: dict
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OptimizationResult:
    symbol: str
    trials: int
    candidates: list[CandidateResult]
    data_from: int
    data_to: int
    seconds: float

    @property
    def survivors(self) -> list[CandidateResult]:
        return [c for c in self.candidates if c.passed]

    @property
    def best(self) -> Optional[CandidateResult]:
        survivors = self.survivors
        return survivors[0] if survivors else None


def adaptive_months(times: np.ndarray, train_months: float, test_months: float,
                    step_months: float) -> tuple[float, float, float]:
    """Walk-forward window lengths that fit the available history.

    With enough history the configured lengths are used. With less (a fresh terminal, a low
    "Max bars in chart" setting, or a short timeframe) the span is split proportionally into
    a training window of half the span and three test windows, so research still produces a
    prior instead of failing (the v3.0 optimiser refused to run below 4 months)."""
    if len(times) < 2:
        return train_months, test_months, step_months
    span = (int(times[-1]) - int(times[0])) / MONTH_SECONDS
    if span >= train_months + test_months or span < MIN_ADAPTIVE_SPAN_MONTHS:
        return train_months, test_months, step_months
    test = span / 6.0
    return span / 2.0, test, test


def walk_forward_windows(times: np.ndarray, train_months: float, test_months: float,
                         step_months: float) -> list[tuple[int, int, int, int]]:
    """(train_start, train_end, test_start, test_end) index ranges, half-open, by calendar time."""
    if len(times) < 2:
        return []
    t0, t_end = int(times[0]), int(times[-1]) + 1
    ragged = int(min(MONTH_SECONDS // 10, test_months * MONTH_SECONDS * 0.1))
    windows = []
    k = 0
    while True:
        train_from = int(t0 + k * step_months * MONTH_SECONDS)
        test_from = int(train_from + train_months * MONTH_SECONDS)
        test_to = int(test_from + test_months * MONTH_SECONDS)
        if test_to > t_end + ragged:  # allow a ragged edge (3 days at most) on the last window
            break
        idx = np.searchsorted(times, [train_from, test_from, test_to])
        windows.append((int(idx[0]), int(idx[1]), int(idx[1]), int(min(idx[2], len(times)))))
        k += 1
    return windows


def _bucket_r(trades: Sequence[Trade], start_time: int, end_time: int) -> tuple[list[float], list[float]]:
    """Monthly R buckets over [start_time, end_time) for the composite score."""
    months = max(1, int(round((end_time - start_time) / MONTH_SECONDS)))
    width = (end_time - start_time) / months
    sums = [0.0] * months
    for t in trades:
        b = min(months - 1, max(0, int((t.exit_time - start_time) // width)))
        sums[b] += t.r
    return sums, [width / MONTH_SECONDS] * months


def train_score(trades: Sequence[Trade], start_time: int, end_time: int) -> float:
    if len(trades) < MIN_TRAIN_TRADES:
        return -math.inf
    rs = np.array([t.r for t in trades])
    sums, months = _bucket_r(trades, start_time, end_time)
    curve = np.cumsum(rs)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:] - curve))
    return composite_score(sums, months, dd, dsr=1.0)


def _span(times: np.ndarray, start: int, end: int, bar_seconds: int) -> tuple[int, int]:
    return int(times[start]), int(times[end - 1]) + bar_seconds


def _neighbor_ok(trades: Sequence[Trade]) -> bool:
    if len(trades) < MIN_TRAIN_TRADES:
        return False
    rs = np.array([t.r for t in trades])
    return float(rs.mean()) > 0 and profit_factor(rs) >= 1.0


def evaluate_strategy(strategy: Strategy, bars: np.ndarray, spec: SymbolSpec, costs: CostModel,
                      settings: OptimizerSettings, trials: int, total_trials: int, symbol: str,
                      extra_params: Sequence[dict] = ()) -> CandidateResult:
    """Walk-forward evaluation of one strategy family. Pure function of its inputs (deterministic)."""
    gates = settings.gates
    times = bars["time"].astype(np.int64)
    train_months, test_months, step_months = adaptive_months(times, settings.train_months, settings.test_months,
                                                             settings.step_months)
    windows = walk_forward_windows(times, train_months, test_months, step_months)
    rng = np.random.default_rng([settings.seed, zlib.crc32(strategy.name.encode()), zlib.crc32(symbol.encode())])
    param_sets = strategy.sample_params(rng, trials)
    # Research hypotheses (vetted grid values) join the SAME walk-forward and gates; they are
    # counted in total_trials, so the multiple-testing penalty grows with them.
    for extra in extra_params:
        if extra not in param_sets:
            param_sets.append(extra)
    reasons: list[str] = []
    if not windows or not param_sets:
        return CandidateResult(strategy.name, strategy.default_params(), False,
                               ["insufficient history for walk-forward windows"], {"windows": 0}, -math.inf)

    sim = dict(spec=spec, costs=costs, equity0=settings.equity, risk_pct=settings.risk_pct, compound=False)
    prepared = PreparedBars(bars)
    # One continuous simulation per parameter set (fixed equity, no compounding). A window
    # owns the trades ENTERED inside it. For training, a trade must also have EXITED inside
    # the training window, so no bar after the training window can influence selection.
    runs = [simulate(prepared, strategy.signals(bars, p), **sim) for p in param_sets]

    def train_trades(trades: Sequence[Trade], start: int, end: int) -> list[Trade]:
        return [t for t in trades if start <= t.entry_index and t.exit_index < end and t.exit_reason != "window_end"]

    def test_trades(trades: Sequence[Trade], start: int, end: int) -> list[Trade]:
        return [t for t in trades if start <= t.entry_index < end]

    oos_trades: list[Trade] = []
    window_r: list[float] = []
    window_months: list[float] = []
    chosen: list[Optional[dict]] = []
    for tr_s, tr_e, te_s, te_e in windows:
        if tr_e - tr_s < 10 or te_e - te_s < 2:
            continue
        a, b = _span(times, tr_s, tr_e, settings.bar_seconds)
        scores = [train_score(train_trades(run, tr_s, tr_e), a, b) for run in runs]
        best = int(np.argmax(scores))
        c, d = _span(times, te_s, te_e, settings.bar_seconds)
        window_months.append((d - c) / MONTH_SECONDS)
        if not math.isfinite(scores[best]) or scores[best] <= 0:
            chosen.append(None)
            window_r.append(0.0)
            continue
        chosen.append(param_sets[best])
        trades = test_trades(runs[best], te_s, te_e)
        oos_trades.extend(trades)
        window_r.append(float(sum(t.r for t in trades)))

    # Parameters for live trading: best on the most recent training window.
    last_end = len(bars)
    last_start = int(np.searchsorted(times, int(times[-1]) + 1 - int(train_months * MONTH_SECONDS)))
    a, b = _span(times, last_start, last_end, settings.bar_seconds)
    final_scores = [train_score(train_trades(run, last_start, last_end), a, b) for run in runs]
    final_idx = int(np.argmax(final_scores))
    final_params = param_sets[final_idx]
    final_ok = math.isfinite(final_scores[final_idx]) and final_scores[final_idx] > 0

    neighbors = strategy.neighbors(final_params)
    neighbor_pass = 0
    for p in neighbors:
        trades = simulate(prepared, strategy.signals(bars, p), start=last_start, **sim)
        neighbor_pass += _neighbor_ok(train_trades(trades, last_start, last_end))
    neighbor_ratio = neighbor_pass / len(neighbors) if neighbors else 1.0

    rs = np.array([t.r for t in oos_trades], dtype=float)
    n = int(rs.size)
    oos_start = windows[0][2]
    metrics = compute_metrics(oos_trades, settings.equity, times, oos_start, len(bars), settings.bar_seconds)
    lo, hi = bootstrap_mean_interval(rs, gates.bootstrap_confidence, gates.bootstrap_samples,
                                     seed=settings.seed) if n >= 2 else (-math.inf, math.inf)
    dsr = deflated_sharpe(rs, total_trials) if n >= 3 else 0.0
    profitable_windows = sum(1 for r in window_r if r > 0)
    win_ratio = profitable_windows / len(window_r) if window_r else 0.0
    score = composite_score(window_r, window_months, metrics["max_drawdown_r"], dsr)

    if n < gates.min_oos_trades:
        reasons.append(f"only {n} out-of-sample trades (< {gates.min_oos_trades})")
    if n and metrics["expectancy_r"] <= 0:
        reasons.append(f"out-of-sample expectancy {metrics['expectancy_r']:.3f}R <= 0 after costs")
    if not (lo > 0):
        reasons.append(f"bootstrap {gates.bootstrap_confidence:.0%} lower bound of mean R {lo:.3f} <= 0")
    if metrics["profit_factor"] < gates.min_profit_factor:
        reasons.append(f"profit factor {metrics['profit_factor']:.2f} < {gates.min_profit_factor}")
    if metrics["max_drawdown_r"] > gates.max_oos_drawdown_r:
        reasons.append(f"max drawdown {metrics['max_drawdown_r']:.1f}R > {gates.max_oos_drawdown_r}R")
    if win_ratio < gates.min_window_win_ratio:
        reasons.append(f"profitable in {win_ratio:.0%} of windows (< {gates.min_window_win_ratio:.0%})")
    if dsr < gates.min_deflated_sharpe:
        reasons.append(f"deflated Sharpe {dsr:.2f} < {gates.min_deflated_sharpe} for {total_trials} trials")
    if not final_ok:
        reasons.append("no parameter set is profitable on the most recent training window")
    if neighbor_ratio < gates.min_neighbor_pass_ratio:
        reasons.append(f"only {neighbor_ratio:.0%} of neighbouring parameters acceptable "
                       f"(< {gates.min_neighbor_pass_ratio:.0%}): isolated spike")
    if score <= 0:
        reasons.append(f"composite score {score:.3f} <= 0")

    metrics.update({
        "windows": float(len(window_r)),
        "profitable_window_ratio": win_ratio,
        "window_r": window_r,
        "bootstrap_low": lo,
        "bootstrap_high": hi,
        "deflated_sharpe": dsr,
        "trials": float(trials),
        "total_trials": float(total_trials),
        "neighbor_pass_ratio": neighbor_ratio,
        "neighbors": float(len(neighbors)),
        "score": score,
        "windows_chosen": [c for c in chosen],
        "oos_r_mean": float(rs.mean()) if n else None,
        "oos_r_var": float(rs.var(ddof=1)) if n > 1 else None,
        "oos_start_time": int(times[oos_start]) if oos_start < len(times) else None,
        "train_months": round(train_months, 3),
        "test_months": round(test_months, 3),
    })
    return CandidateResult(strategy.name, final_params, not reasons, reasons, metrics, score)


def _task(args: tuple) -> CandidateResult:
    name, bars, spec, costs, settings, trials, total, symbol, extra = args
    _lower_priority()
    return evaluate_strategy(strategy_lib.get(name), bars, spec, costs, settings, trials, total, symbol, extra)


_PRIORITY_LOWERED = False


def _lower_priority() -> None:
    """Run optimisation at low OS priority so it never starves the live loop."""
    global _PRIORITY_LOWERED
    if _PRIORITY_LOWERED:
        return
    _PRIORITY_LOWERED = True
    try:
        if os.name == "nt":  # pragma: no cover - Windows only
            import ctypes

            below_normal = 0x00004000
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, below_normal)
        else:
            os.nice(10)
    except Exception:  # noqa: BLE001 - priority is best effort
        log.debug("could not lower process priority", exc_info=True)


def valid_extra_params(name: str, candidates: Sequence[dict]) -> list[dict]:
    """Only parameter sets that are exactly on the strategy's grid and valid; others are dropped."""
    strategy = strategy_lib.get(name)
    out: list[dict] = []
    for params in candidates:
        try:
            canon = strategy.validate_params(params)
        except ValueError:
            continue
        if canon not in out:
            out.append(canon)
    return out


def hypothesis_params(hypotheses: Sequence[dict]) -> dict[str, list[dict]]:
    """Turn (strategy, param, value) hypotheses into full parameter sets around the grid middle."""
    out: dict[str, list[dict]] = {}
    for h in hypotheses:
        try:
            strategy = strategy_lib.get(str(h.get("strategy")))
            params = strategy.default_params()
        except (KeyError, RuntimeError):
            continue
        if h.get("param") not in params:
            continue
        params[h["param"]] = h.get("value")
        out.setdefault(strategy.name, []).append(params)
    return out


def trial_plan(names: Sequence[str], settings: OptimizerSettings) -> dict[str, int]:
    """Trials per strategy, capped so the cycle total never exceeds ``max_trials_per_cycle``."""
    if not names:
        return {}
    per = max(1, min(settings.trials_per_strategy, settings.max_trials_per_cycle // len(names)))
    return {n: min(per, strategy_lib.get(n).grid_size()) for n in names}


def optimize_symbol(symbol: str, bars: np.ndarray, spec: SymbolSpec, costs: CostModel,
                    settings: OptimizerSettings, strategy_names: Sequence[str], workers: int = 1,
                    lower_priority: bool = False,
                    extra_params: Optional[Any] = None) -> OptimizationResult:
    """Evaluate every enabled strategy family on one symbol. Survivors first, by score."""
    started = time.monotonic()
    if lower_priority:
        _lower_priority()
    plan = trial_plan(strategy_names, settings)
    extras = {name: valid_extra_params(name, (extra_params or {}).get(name, ())) for name in strategy_names}
    total = sum(plan.values()) + sum(len(v) for v in extras.values())
    tasks = [(name, bars, spec, costs, settings, plan[name], total, symbol, extras[name]) for name in strategy_names]
    if workers > 1 and len(tasks) > 1:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=min(workers, len(tasks))) as pool:
            results = pool.map(_task, tasks)
    else:
        results = [evaluate_strategy(strategy_lib.get(t[0]), *t[1:]) for t in tasks]
    results.sort(key=lambda c: (not c.passed, -c.score if math.isfinite(c.score) else math.inf, c.strategy))
    return OptimizationResult(symbol, total, results, int(bars["time"][0]), int(bars["time"][-1]),
                              time.monotonic() - started)
