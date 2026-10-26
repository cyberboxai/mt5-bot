"""``--simulate``: the REAL bot (arms, tiers, risk layer, team, advisor path) on a synthetic market.

* State lives in its own folder (default ``state_sim``), never mixed with real accounts.
* Time is virtual: each step closes one synthetic M5 bar (M15 and H1 are aggregated from it),
  so a trading day runs in seconds at full speed.
* Nothing is seeded: arms start in shadow (T0) and the bot explores with micro risk from the
  first bars, exactly as on a real account. Research either runs for real in a separate
  process (``research="subprocess"``, the panel demo) or is skipped (``"skip"``, tests).
* Nothing learned in a simulation is evidence about real markets.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from ..config import Config, apply_overrides
from ..config_blocks import to_jsonable
from .advisor import SimulatedAdvisor
from .market import DEMO_SYMBOLS, SyntheticMarket
from .terminal import SimulatedTerminal

log = logging.getLogger("mt5_bot")

SIM_BAR_SECONDS = 300
SIM_WARMUP_BARS = 9000   # about a month of M5 bars, so H1 arms have enough history


class VirtualClock:
    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


class _FinishedProcess:
    def poll(self) -> int:
        return 0


def simulation_config(cfg: Config, state_root: Path, team_mode: Optional[str]) -> Config:
    state_root = Path(state_root)
    names = tuple(s.name for s in DEMO_SYMBOLS)
    overrides: dict[str, Any] = {
        "symbols": ("EURUSD",), "timeframe": "M15", "bars": 600, "trading_hours_server": (0, 24),
        "state_root": str(state_root), "health_file": str(state_root / "health.json"),
        "reports_dir": str(state_root / "reports"), "kill_switch_file": str(state_root / "KILL"),
        "log_file": str(state_root / "simulation.log"), "dry_run": False, "account_mode": "demo_only",
        "real_money_confirmed": False, "news_filter_enabled": False, "commission_per_lot": 7.0,
        "terminal_restart_enabled": False, "telegram_enabled": False, "risk_profile": "balanced",
        "team.team_mode": team_mode or "full", "universe.candidates": names, "universe.max_symbols": len(names),
        "universe.timeframes": ("M5", "M15", "H1"), "universe.max_arms": 400, "universe.scan_every_minutes": 30.0,
        "research.startup_history_months": 4, "research.startup_trials": 4, "history_months": 4,
        "wf_train_months": 2, "wf_test_months": 1,
    }
    return apply_overrides(cfg, overrides)


def run_simulation(cfg: Config, state_root: Path, steps: Optional[int] = None, speed: float = 2.0, seed: int = 7,
                   stop: Optional[threading.Event] = None, warmup_bars: int = SIM_WARMUP_BARS,
                   advisor: str = "simulated", team_mode: Optional[str] = None, fast_forward: int = 0,
                   on_step: Optional[Callable[[int, Any], None]] = None, research: str = "skip",
                   random_walk: bool = False, symbols: Optional[tuple] = None,
                   config_overrides: Optional[dict[str, Any]] = None) -> int:
    """Run until ``steps`` bars (None: until stopped or the kill switch). Returns the bot's exit code."""
    from ..bot import EXIT_OK, Bot
    from ..officedb import OfficeStore
    from ..registry import Registry

    # This is a separate, synthetic-data process: durability of every commit is irrelevant,
    # while the panel still needs WAL for concurrent reads.
    Registry.fast_writes = True
    OfficeStore.fast_writes = True
    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    stop = stop or threading.Event()
    sim_cfg = simulation_config(cfg, state_root, team_mode)
    models = symbols or DEMO_SYMBOLS
    if symbols is not None:
        sim_cfg = apply_overrides(sim_cfg, {"universe.candidates": tuple(s.name for s in models),
                                            "universe.max_symbols": len(models), "symbols": (models[0].name,)})
    if config_overrides:
        sim_cfg = apply_overrides(sim_cfg, config_overrides)
    config_path = state_root / "simulation-config.json"
    config_path.write_text(json.dumps(to_jsonable_config(sim_cfg), indent=1), encoding="utf-8")
    market = SyntheticMarket(seed, symbols=models, bar_seconds=SIM_BAR_SECONDS, random_walk=random_walk)
    terminal = SimulatedTerminal(market, warmup_bars)
    clock = VirtualClock(terminal.utc_now())
    holder = SimpleNamespace(bot=None)

    def skip_research(args: list[str], **kwargs: Any) -> _FinishedProcess:
        if holder.bot is not None and holder.bot.registry is not None:
            reg = holder.bot.registry
            reg.set_kv("last_research_attempt", str(clock()))
            reg.set_kv("last_research_finished", str(clock()))
            reg.set_kv("last_full_research", str(clock()))
            reg.set_kv("last_research_status", json.dumps({"ok": 0, "failed": 0, "mode": "skipped",
                                                           "trigger": "simulation without research"}))
            log.info("SIMULATION: research skipped; arms learn from shadow and exploration evidence only")
        return _FinishedProcess()

    spawn = None if research == "subprocess" else skip_research
    sim_advisor = SimulatedAdvisor(clock) if advisor == "simulated" else None
    bot = Bot(sim_cfg, api=terminal, wait=lambda seconds: None, env={}, monotonic=clock, utc_now=clock,
              config_path=str(config_path), spawn=spawn, globber=lambda pattern: [],
              terminal_killer=lambda exe: False, advisor=sim_advisor, no_claude=advisor == "none",
              durable_files=False, auto_migrate=False)
    holder.bot = bot
    code = EXIT_OK
    try:
        bot.startup()
        if sim_advisor is not None:
            sim_advisor.store = bot.office
        bot.registry.set_kv("simulation", "1")
        log.info("SIMULATION started in %s (synthetic data, virtual time, no broker, no API key)", state_root)
        for step in itertools.count():
            if (steps is not None and step >= steps) or stop.is_set():
                break
            terminal.advance_bar()
            clock.t = terminal.utc_now()
            result = bot.run_once()
            if result is not None:
                code = result
                break
            if on_step is not None:
                on_step(step, bot)
            if speed > 0 and step >= fast_forward:
                stop.wait(1.0 / speed)
    finally:
        bot.shutdown()
        bot.close()
    return code


def to_jsonable_config(cfg: Config) -> dict[str, Any]:
    import dataclasses

    return {f.name: to_jsonable(getattr(cfg, f.name)) for f in dataclasses.fields(cfg)}


def main_simulate(cfg: Config, args: Any) -> int:  # pragma: no cover - thin CLI wrapper
    from ..cli import install_signal_handlers_for, setup_logging

    state_root = Path(args.sim_state)
    setup_logging(str(state_root / "simulation.log"))
    stop = threading.Event()
    install_signal_handlers_for(stop.set)
    started = time.monotonic()
    code = run_simulation(cfg, state_root, steps=args.sim_bars, speed=args.sim_speed, seed=args.sim_seed, stop=stop,
                          advisor=args.sim_advisor, team_mode=args.team_mode, fast_forward=args.sim_fast_forward,
                          research="subprocess")
    log.info("SIMULATION finished after %.0f s (exit code %s)", time.monotonic() - started, code)
    return code
