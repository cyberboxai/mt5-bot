"""Research rigor: decide, with a correction for selection bias, whether a family has an edge.

For one strategy family on one symbol and timeframe:

1. simulate every sampled configuration with the calibrated costs, and COUNT it in the persistent
   trial ledger (trials are never deleted: forgetting failed attempts is how backtests lie);
2. CPCV: on each split, choose the configuration on purged training trades and collect what it does
   on the test groups. This is the honest out-of-sample distribution of the whole procedure;
3. compute the deflated Sharpe with every trial ever counted, the probability of backtest
   overfitting across configurations, the minimum track record and the cost-to-edge ratio;
4. give a verdict: ``edge_plausible``, ``no_edge`` or ``insufficient``.

Arms that end in ``no_edge`` are retired to research. Only verdicts feed the priors of the tiers.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

import numpy as np

from . import strategies as strategy_lib
from .backtest import CostModel, Trade, prepare, simulate
from .core.costs import all_in_cost_per_lot, cost_to_edge_ratio
from .core.stamp import json_hash
from .core.validation import (SharpeStats, cpcv_splits, deflated_sharpe, group_bounds, in_groups_mask, min_track_record,
                              pbo_cscv, purged_train_mask, sharpe_stats)
from .risk import SymbolSpec

log = logging.getLogger("mt5_bot")

EDGE = "edge_plausible"
NO_EDGE = "no_edge"
INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class RigorSettings:
    n_groups: int = 6
    k_test: int = 2
    embargo_bars: int = 20
    pbo_blocks: int = 10
    min_trades: int = 30
    min_train_trades: int = 8
    min_dsr: float = 0.95
    max_pbo: float = 0.5
    k_cost: float = 2.0
    credible_z: float = 1.645


@dataclass(frozen=True)
class CandidateTrial:
    params: Mapping[str, Any]
    trades: Sequence[Trade]
    stats: SharpeStats


@dataclass
class FamilyVerdict:
    strategy: str
    params: dict[str, Any]
    verdict: str
    n_trials: int
    sharpe: float
    dsr: float
    pbo: Optional[float]
    min_track_record: float
    track_record: int
    cpcv_n: int
    cpcv_mean_r: Optional[float]
    cpcv_var_r: Optional[float]
    cost_to_edge: Optional[float]
    reasons: list[str] = field(default_factory=list)
    entry_styles: dict[str, Any] = field(default_factory=dict)


def _arrays(trades: Sequence[Trade]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (np.array([t.entry_index for t in trades], dtype=np.int64),
            np.array([t.exit_index for t in trades], dtype=np.int64),
            np.array([t.r for t in trades], dtype=float))


def cpcv_oos(trials: Sequence[CandidateTrial], n_bars: int, s: RigorSettings) -> tuple[np.ndarray, list[int]]:
    """Out-of-sample R of the procedure "pick the best on purged training data", over all splits."""
    bounds = group_bounds(n_bars, s.n_groups)
    data = [_arrays(t.trades) for t in trials]
    oos: list[float] = []
    picks: list[int] = []
    for split in cpcv_splits(s.n_groups, s.k_test):
        test_bounds = [bounds[g] for g in split.test_groups]
        scores = []
        for entries, exits, rs in data:
            keep = purged_train_mask(entries, exits, test_bounds, s.embargo_bars)
            scores.append(float(rs[keep].mean()) if int(keep.sum()) >= s.min_train_trades else -math.inf)
        best = int(np.argmax(scores))
        if not math.isfinite(scores[best]):
            continue
        picks.append(best)
        entries, _exits, rs = data[best]
        oos.extend(rs[in_groups_mask(entries, test_bounds)].tolist())
    return np.asarray(oos, dtype=float), picks


def block_matrix(trials: Sequence[CandidateTrial], n_bars: int, n_rows: int = 40) -> np.ndarray:
    """R earned per time block, one column per configuration: the input of PBO."""
    edges = np.linspace(0, n_bars, n_rows + 1)
    out = np.zeros((n_rows, len(trials)))
    for j, trial in enumerate(trials):
        entries, _exits, rs = _arrays(trial.trades)
        if rs.size:
            rows = np.clip(np.searchsorted(edges, entries, side="right") - 1, 0, n_rows - 1)
            np.add.at(out[:, j], rows, rs)
    return out


def entry_style_report(bars: np.ndarray, trades: Sequence[Trade], spec: SymbolSpec, offset_spreads: float = 0.5) -> dict:
    """Would a limit or stop entry have beaten a market entry? Bar-level replay of the same signals.

    A limit at ``offset`` better than the market fills only if the bar trades through it and misses
    the rest; a stop pays ``offset`` and fills only if price moves that way. This reports evidence
    per arm, and nothing switches on it by default: market orders stay until the data says otherwise."""
    out: dict[str, Any] = {"market": {"signals": len(trades), "fill_rate": 1.0,
                                      "mean_r": float(np.mean([t.r for t in trades])) if trades else 0.0}}
    for name, sign in (("limit", 1.0), ("stop", -1.0)):
        filled, total = 0, 0.0
        for t in trades:
            side = 1 if t.side == "BUY" else -1
            risk = abs(t.entry_price - t.sl)
            bar = bars[t.entry_index]
            offset = float(bar["spread"]) * spec.point * offset_spreads
            level = t.entry_price - sign * side * offset
            touched = (float(bar["low"]) <= level) if side * sign > 0 else (float(bar["high"]) >= level)
            if touched and risk > 0:
                filled += 1
                total += t.r + sign * offset / risk
        out[name] = {"signals": len(trades), "fill_rate": filled / len(trades) if trades else 0.0,
                     "mean_r": total / len(trades) if trades else 0.0}
    return out


def evaluate_family(bars: np.ndarray, strategy: str, params_list: Sequence[Mapping[str, Any]], spec: SymbolSpec,
                    costs: CostModel, equity: float, risk_pct: float, settings: RigorSettings,
                    prior_trials: int = 0, count_trial: Optional[Callable[[Mapping[str, Any], SharpeStats], None]] = None,
                    swap_nights: float = 0.0) -> Optional[FamilyVerdict]:
    """The full verdict for one family. ``prior_trials`` are those already in the ledger."""
    strat = strategy_lib.get(strategy)
    pb = prepare(bars)
    trials: list[CandidateTrial] = []
    for params in params_list:
        signal = strat.signals(bars, params)
        trades = simulate(pb, signal, spec, costs, equity, risk_pct, compound=False)
        stats = sharpe_stats([t.r for t in trades])
        if count_trial is not None:
            count_trial(params, stats)
        trials.append(CandidateTrial(params, trades, stats))
    if not trials:
        return None
    total_trials = prior_trials + len(trials)
    scored = [t for t in trials if t.stats.n >= settings.min_train_trades]
    trial_var = float(np.var([t.stats.sharpe for t in scored], ddof=1)) if len(scored) > 1 else 0.0
    best = max(trials, key=lambda t: (t.stats.sharpe if t.stats.n >= settings.min_trades else -math.inf, t.stats.n))
    oos, _picks = cpcv_oos(trials, len(bars), settings)
    dsr = deflated_sharpe(best.stats, total_trials, trial_var)
    pbo = pbo_cscv(block_matrix(trials, len(bars)), settings.pbo_blocks)
    mtr = min_track_record(best.stats, 0.0, settings.min_dsr)
    mean = float(oos.mean()) if oos.size else None
    var = float(oos.var(ddof=1)) if oos.size > 1 else None
    ratio = None
    reasons: list[str] = []
    if mean is not None and var is not None and best.trades:
        lower = mean - settings.credible_z * math.sqrt(var / max(oos.size, 1))
        loss = float(np.mean([t.risk_money / t.volume for t in best.trades]))
        spread = float(np.median(np.asarray(bars["spread"], dtype=float)))
        swap = spec.swap_cost_per_lot_night("BUY")
        cost = all_in_cost_per_lot(spread, costs.slippage_points, spec.point, spec.value_per_price,
                                   costs.commission_per_lot, swap, swap_nights)
        ratio = cost_to_edge_ratio(lower, loss, cost)
    if best.stats.n < settings.min_trades or oos.size < settings.min_trades:
        verdict = INSUFFICIENT
        reasons.append(f"only {best.stats.n} trades (need {settings.min_trades})")
    else:
        if dsr < settings.min_dsr:
            reasons.append(f"deflated Sharpe {dsr:.3f} < {settings.min_dsr}")
        if pbo is not None and pbo > settings.max_pbo:
            reasons.append(f"overfitting probability {pbo:.2f} > {settings.max_pbo}")
        if best.stats.n < mtr:
            reasons.append(f"track record {best.stats.n} < minimum {mtr:.0f}")
        if mean is None or mean <= 0:
            reasons.append("out-of-sample expectancy is not positive")
        if ratio is None or ratio < settings.k_cost:
            shown = "n/a" if ratio is None else f"{ratio:.2f}"
            reasons.append(f"cost-to-edge {shown} < {settings.k_cost}")
        verdict = EDGE if not reasons else NO_EDGE
    styles = entry_style_report(bars, best.trades, spec) if best.trades else {}
    return FamilyVerdict(strategy, dict(best.params), verdict, total_trials, best.stats.sharpe, dsr, pbo, mtr,
                         best.stats.n, int(oos.size), mean, var, ratio, reasons, styles)


class TrialLedger:
    """Persistent count of every configuration ever evaluated, per account database."""

    def __init__(self, registry: Any, clock: Callable[[], float]) -> None:
        self.conn = registry.conn
        self.registry = registry
        self.clock = clock

    def count(self, strategy: Optional[str] = None) -> int:
        if strategy:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM trial_ledger WHERE strategy = ?", (strategy,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM trial_ledger").fetchone()
        return int(row["n"])

    def record(self, run_id: Optional[int], symbol: str, timeframe: str, strategy: str, params: Mapping[str, Any],
               snapshot_id: Optional[str], stats: SharpeStats) -> None:
        self.conn.execute(
            "INSERT INTO trial_ledger (ts, run_id, symbol, timeframe, strategy, params_hash, snapshot_id, sharpe, n_obs)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (float(self.clock()), run_id, symbol, timeframe, strategy, json_hash(dict(params)), snapshot_id,
             stats.sharpe, stats.n))

    def store_verdict(self, run_id: Optional[int], symbol: str, timeframe: str, param_set_id: Optional[int],
                      v: FamilyVerdict, snapshot_id: Optional[str]) -> None:
        from .eventlog import canonical

        def finite(x: Optional[float]) -> Optional[float]:
            return None if x is None or not math.isfinite(x) else float(x)

        detail = {"params": v.params, "reasons": v.reasons, "entry_styles": v.entry_styles}
        self.conn.execute(
            "INSERT INTO candidate_verdicts (ts, run_id, symbol, timeframe, strategy, param_set_id, n_trials, sharpe,"
            " dsr, pbo, min_track_record, track_record, cpcv_n, cpcv_mean_r, cpcv_var_r, cost_to_edge, verdict,"
            " snapshot_id, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (float(self.clock()), run_id, symbol, timeframe, v.strategy, param_set_id, v.n_trials, v.sharpe, v.dsr,
             finite(v.pbo), finite(v.min_track_record), v.track_record, v.cpcv_n, finite(v.cpcv_mean_r),
             finite(v.cpcv_var_r), finite(v.cost_to_edge), v.verdict, snapshot_id, canonical(detail)))


def sample_params(strategy: str, count: int, seed: int) -> list[dict[str, Any]]:
    strat = strategy_lib.get(strategy)
    rng = np.random.default_rng(seed)
    params = [strat.default_params()]
    for candidate in strat.sample_params(rng, count):
        if candidate not in params:
            params.append(candidate)
    return params[:max(count, 1)]
