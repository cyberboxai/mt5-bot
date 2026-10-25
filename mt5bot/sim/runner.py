"""``--simulate``: the REAL bot (risk layer, engine, team, advisor path) on a synthetic market.

* State lives in its own folder (default ``state_sim``), never mixed with real accounts.
* Time is virtual: each step closes one synthetic bar, so a trading day of M15 bars runs in
  under a minute at the default speed.
* The champion is SEEDED (the optimiser would need months of data); the decision log says
  so explicitly. Nothing learned in a simulation is evidence about real markets.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from ..config import Config, apply_overrides
from ..registry import NOTRADE_CANDIDATE, Candidate
from .advisor import SimulatedAdvisor
from .market import SyntheticMarket
from .terminal import SimulatedTerminal

log = logging.getLogger("mt5_bot")

DEMO_CHAMPIONS = (
    ("donchian", {"channel": 40, "sl_mult": 2.0, "tp_mult": 3.0}),
    ("ema_cross", {"fast": 15, "slow": 80, "sl_mult": 2.0, "tp_mult": 3.0}),
    ("bollinger", {"period": 20, "width": 2.0, "mode": "reversion", "sl_mult": 1.5, "tp_mult": 2.0}),
    ("rsi_reversion", {"rsi_period": 14, "oversold": 30, "trend_ema": 150, "sl_mult": 1.5, "tp_mult": 2.0}),
)
SEED_REASON = "SIMULATION: demo champion seeded on synthetic data (not validated, not a recommendation)"
RESEED_AFTER_BARS = 48


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
    overrides: dict[str, Any] = {
        "symbols": ("EURUSD", "GBPUSD", "USDJPY"), "timeframe": "M15", "bars": 600, "trading_hours_server": (0, 24),
        "state_root": str(state_root), "health_file": str(state_root / "health.json"),
        "reports_dir": str(state_root / "reports"), "kill_switch_file": str(state_root / "KILL"),
        "log_file": str(state_root / "simulation.log"), "dry_run": False, "account_mode": "demo_only",
        "real_money_confirmed": False, "news_filter_enabled": False, "commission_per_lot": 7.0,
        "terminal_restart_enabled": False, "max_spread_points": 40, "telegram_enabled": False,
        "team.team_mode": team_mode or "full", "max_open_positions_total": 3,
    }
    return apply_overrides(cfg, overrides)


def seed_champion(registry: Any, symbol: str, index: int) -> None:
    from .. import strategies as strategy_lib

    name, params = DEMO_CHAMPIONS[index % len(DEMO_CHAMPIONS)]
    params = strategy_lib.get(name).validate_params(params)
    ps = registry.param_set_id(name, params)
    candidate = Candidate(None, ps, name, params, {}, 0.0, True)
    registry.record_switch(symbol, "promote", registry.champion(symbol) if not registry.champion(symbol).is_notrade
                           else NOTRADE_CANDIDATE, candidate, SEED_REASON, {"simulation": True})


def run_simulation(cfg: Config, state_root: Path, steps: Optional[int] = None, speed: float = 2.0, seed: int = 7,
                   stop: Optional[threading.Event] = None, warmup_bars: int = 1500, advisor: str = "simulated",
                   team_mode: Optional[str] = None, fast_forward: int = 0,
                   on_step: Optional[Callable[[int, Any], None]] = None) -> int:
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
    market = SyntheticMarket(seed)
    terminal = SimulatedTerminal(market, warmup_bars)
    clock = VirtualClock(terminal.utc_now())
    holder = SimpleNamespace(bot=None)

    def spawn(args: list[str], **kwargs: Any) -> _FinishedProcess:
        # The walk-forward optimiser needs months of history; the simulation skips it and says so.
        if holder.bot is not None and holder.bot.registry is not None:
            holder.bot.registry.set_kv("last_cycle_finished", str(clock()))
            log.info("SIMULATION: optimisation skipped (seeded demo champions instead)")
        return _FinishedProcess()

    sim_advisor = SimulatedAdvisor(clock) if advisor == "simulated" else None
    bot = Bot(sim_cfg, api=terminal, wait=lambda seconds: None, env={}, monotonic=clock, utc_now=clock,
              config_path=str(state_root / "simulation-config.json"), spawn=spawn, globber=lambda pattern: [],
              terminal_killer=lambda exe: False, advisor=sim_advisor, no_claude=advisor == "none",
              durable_files=False)
    holder.bot = bot
    code = EXIT_OK
    try:
        bot.startup()
        if sim_advisor is not None:
            sim_advisor.store = bot.office
        bot.registry.set_kv("simulation", "1")
        for i, symbol in enumerate(bot.symbols.values()):
            if bot.registry.champion(symbol).is_notrade:
                seed_champion(bot.registry, symbol, i)
        notrade_bars: dict[str, int] = {}
        rotation = itertools.count(len(bot.symbols))
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
            for symbol in bot.symbols.values():
                if bot.registry.champion(symbol).is_notrade:
                    notrade_bars[symbol] = notrade_bars.get(symbol, 0) + 1
                    if notrade_bars[symbol] >= RESEED_AFTER_BARS:
                        seed_champion(bot.registry, symbol, next(rotation))
                        notrade_bars[symbol] = 0
            if on_step is not None:
                on_step(step, bot)
            if speed > 0 and step >= fast_forward:
                stop.wait(1.0 / speed)
    finally:
        bot.shutdown()
        bot.close()
    return code


def main_simulate(cfg: Config, args: Any) -> int:  # pragma: no cover - thin CLI wrapper
    from ..cli import install_signal_handlers_for, setup_logging

    state_root = Path(args.sim_state)
    setup_logging(str(state_root / "simulation.log"))
    stop = threading.Event()
    install_signal_handlers_for(stop.set)
    started = time.monotonic()
    code = run_simulation(cfg, state_root, steps=args.sim_bars, speed=args.sim_speed, seed=args.sim_seed, stop=stop,
                          advisor=args.sim_advisor, team_mode=args.team_mode, fast_forward=args.sim_fast_forward)
    log.info("SIMULATION finished after %.0f s (exit code %s)", time.monotonic() - started, code)
    return code
