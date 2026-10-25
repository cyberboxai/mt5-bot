"""Worker functions for the optimiser property tests (importable by multiprocessing)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mt5bot import strategies as S  # noqa: E402
from mt5bot.backtest import CostModel  # noqa: E402
from mt5bot.optimizer import Gates, OptimizerSettings, optimize_symbol  # noqa: E402
from mt5bot.risk import SymbolSpec  # noqa: E402

SPEC = SymbolSpec("EURUSD", 0.00001, 5, 1.0, 0.00001, 100_000, 0.01, 100.0, 0.01)
ZERO_COSTS = CostModel(0.0, 0.0, 1e9, (0, 24))
REAL_COSTS = CostModel(7.0, 2.0, 30.0, (0, 24))
HOURS_PER_YEAR = 24 * 365


def run_case(args: tuple[int, float, bool]) -> dict:
    """One full optimisation of the whole library on one synthetic year of H1 bars."""
    from conftest import synthetic_bars  # noqa: PLC0415 - tests dir is on sys.path

    seed, trend, zero_costs = args
    bars = synthetic_bars(HOURS_PER_YEAR, seed=1000 + seed if not trend else 200 + seed, trend=trend)
    if zero_costs:
        bars["spread"] = 0
    settings = OptimizerSettings(bar_seconds=3600, trials_per_strategy=6, seed=seed,
                                 gates=Gates(bootstrap_samples=500))
    result = optimize_symbol("EURUSD", bars, SPEC, ZERO_COSTS if zero_costs else REAL_COSTS, settings,
                             S.library_names())
    return {
        "seed": seed,
        "best": result.best.strategy if result.best else None,
        "survivors": [c.strategy for c in result.survivors],
        "with_enough_trades": sum(1 for c in result.candidates if c.metrics.get("trades", 0) >= 100),
        "trials": result.trials,
    }
