#!/usr/bin/env python3
"""Reproducible benchmark of the trading loop and of one optimization cycle.

Runs on synthetic data with no terminal, broker or network, so the same command gives
comparable numbers before and after a change. Usage:

    python tools/benchmark.py --out docs/bench_after.json --bars 400
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import logging
import os
import pstats
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from mt5bot.config import Config  # noqa: E402


def percentiles(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    return {"n": int(arr.size), "p50": round(float(np.percentile(arr, 50)), 3),
            "p95": round(float(np.percentile(arr, 95)), 3), "p99": round(float(np.percentile(arr, 99)), 3),
            "max": round(float(arr.max()), 3)}


def table_sizes(db: Path) -> dict[str, int]:
    import sqlite3

    conn = sqlite3.connect(db)
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        out[name] = int(conn.execute(f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0])  # names come from the schema
    conn.close()
    return {k: v for k, v in out.items() if v}


def idle_cycle(bot: Any, out: list[float]) -> int:
    """One more loop with no new bar, counting the market data calls it makes."""
    source = getattr(bot, "market", None) or bot.mt5
    name = "rates" if hasattr(bot, "market") else "copy_rates_from_pos"
    original = getattr(source, name)
    count = [0]

    def counted(*args: Any, **kwargs: Any) -> Any:
        count[0] += 1
        return original(*args, **kwargs)

    setattr(source, name, counted)
    started = time.perf_counter()
    bot.run_once()
    out.append((time.perf_counter() - started) * 1000.0)
    setattr(source, name, original)
    return count[0]


def bench_loop(bars: int, warmup: int, profile_to: Path | None) -> dict[str, Any]:
    from mt5bot.sim.runner import run_simulation

    root = Path(tempfile.mkdtemp(prefix="bench_"))
    samples: list[float] = []
    kernel: list[float] = []
    idle: list[float] = []
    calls: list[int] = []
    writes = [0]
    last = [time.perf_counter()]

    def on_step(step: int, bot: Any) -> None:
        now = time.perf_counter()
        samples.append((now - last[0]) * 1000.0)
        kernel.append(float(bot._loop_ms))      # run_once alone: no bar generation by the simulator
        calls.append(idle_cycle(bot, idle))     # a poll with no new bar: almost every live cycle
        writes[0] = int(bot.registry.raw.total_changes)
        last[0] = time.perf_counter()

    cfg = Config.from_dict({"state_root": str(root), "bars": 1000})
    prof = cProfile.Profile()
    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    if profile_to:
        prof.enable()
    run_simulation(cfg, root, steps=bars, speed=0, seed=7, warmup_bars=warmup, advisor="none", on_step=on_step)
    if profile_to:
        prof.disable()
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(45)
        pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(30)
        profile_to.write_text(buf.getvalue(), encoding="utf-8")
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    db = next(root.rglob("mt5bot.sqlite"))
    wal = Path(str(db) + "-wal")
    size = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    out = {"bars": bars, "warmup_bars": warmup, "cycle_ms": percentiles(samples[5:]),
           "kernel_ms": percentiles(kernel[5:]), "idle_ms": percentiles(idle[5:]),
           "idle_rate_calls": round(sum(calls[5:]) / max(len(calls) - 5, 1), 2),
           "row_changes_per_bar": round(writes[0] / max(bars, 1), 1), "wall_s": round(wall, 2),
           "cpu_s": round(cpu, 2), "db_bytes": size, "db_kib_per_bar": round(size / 1024 / max(bars, 1), 2),
           "rows": table_sizes(db)}
    shutil.rmtree(root, ignore_errors=True)
    return out


def bench_optimizer(trials: int) -> dict[str, Any]:
    from mt5bot.backtest import CostModel
    from mt5bot.optimizer import OptimizerSettings, optimize_symbol
    from mt5bot.risk import SymbolSpec
    from mt5bot.sim.market import DEFAULT_SYMBOLS, SyntheticMarket
    from mt5bot import strategies

    market = SyntheticMarket(seed=11, symbols=DEFAULT_SYMBOLS[:1], bar_seconds=900)
    bars = market.history(9000)["EURUSD"]
    spec = SymbolSpec("EURUSD", 0.00001, 5, 1.0, 0.00001, 100_000, 0.01, 100.0, 0.01)
    costs = CostModel(commission_per_lot=7.0, slippage_points=2.0)
    settings = OptimizerSettings(2, 1, 1, trials, trials * 12, 7, 0.25, 10_000.0, 900)
    names = sorted(strategies.library())
    t0 = time.perf_counter()
    result = optimize_symbol("EURUSD", bars, spec, costs, settings, names, 1, False)
    return {"bars": len(bars), "trials_per_strategy": trials, "strategies": len(names),
            "seconds": round(time.perf_counter() - t0, 2), "families": len(result.candidates)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--bars", type=int, default=400)
    parser.add_argument("--warmup", type=int, default=3000)
    parser.add_argument("--trials", type=int, default=8)
    parser.add_argument("--profile", default=None, help="write cProfile text of the loop to this path")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {"python": sys.version.split()[0], "platform": sys.platform, "cpus": os.cpu_count(),
              "loop": bench_loop(args.bars, args.warmup, Path(args.profile) if args.profile else None),
              "optimizer": bench_optimizer(args.trials),
              "max_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "loop"}, indent=1))
    print(json.dumps({k: v for k, v in report["loop"].items() if k != "rows"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
