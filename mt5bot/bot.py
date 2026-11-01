"""Live trading loop: the ONE decision path from market data to an order.

For every symbol of the universe and every timeframe its arms trade on, each closed bar goes
through the same stages, each emitting reason codes into the decision funnel:

    data -> arms (shadow books, signals) -> allocation (tiers, exploration) -> sizing
    (capital, budget, affordability) -> team review (CIO protocol, optional advisor; can only
    shrink) -> hard risk layer -> execution with server-side SL and TP

This class is the single kernel: live, shadow, replay and simulation run this same code and
differ only in the adapters behind ``BrokerPort`` and ``MarketDataPort``. Every order passes
through ``risk`` and then ``execution.ExecutionEngine``, and carries SL and TP, so protection
lives on the broker server even if this process or the PC dies.
"""

from __future__ import annotations

import glob
import json
import logging
import math
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import numpy as np

from . import __version__, flow, funnel as funnel_mod, governance, limits
from . import regime as regime_mod
from .indicators import SeriesBuffer
from .adapters.mt5 import Mt5Adapter, load_terminal_api
from .backup import BackupSchedule
from .bot_gates import GatesMixin
from .bot_orders import (EntryOutcome, OrdersMixin, MARKET_CLOSED_BACKOFF_SECONDS, ORDER_COMMENT, QUARANTINE_AFTER,  # noqa: F401
                         QUARANTINE_MAX_MINUTES, QUARANTINE_MINUTES)
from .bot_oversight import OversightMixin
from .allocation import AllocationInputs, allocate
from .arms import ArmEngine, ArmSignal, EntryIntent
from .capital import CapitalState, SizingDecision, SizingInputs, affordability, anti_martingale_cap, size_risk, \
    split_volume
from .config import (TIMEFRAME_SECONDS, Config, apply_overrides, config_hash, config_snapshot, in_trading_hours,
                     reload_decision)
from .core import broker as br
from .core import orders as oc
from .core.broker import select_filling, side_of_position
from .core.costs import (schedule_block)
from .core.tail import (LadderState, update_ladder)
from .datastore import DataStore
from .core.stamp import make_stamp, merge_stamp, short_hash
from .db import db_path
from .eventlog import EventLog
from .execution import ExecutionEngine, OrderJournal
from .observability import AlertEngine, evaluate_rules
from .funnel import Funnel
from .market import ServerClock, fmt_server_time, is_weekend, market_expected_open, server_datetime
from .notify import Notifier
from .opportunity import STOP_ATR, affordability_rows, atr_and_spread, rank, score_symbol
from .persistence import account_key, account_state_dir, mask
from .ports import BrokerPort, MarketDataPort
from .registry import Registry
from .reports import trade_stats, write_health, write_report
from .research import ResearchScheduler, cost_model
from .risk import (EntryContext, EquityGuard, NewsCalendar, OpenRisk, RiskManager, SymbolSpec,
                   plan_trade)
from .tca import TcaStore
from .terminal import FreezeWatchdog, discover_terminal_paths, kill_terminal, terminal_exe_from_info
from .tiers import exploration_room, server_week_start

log = logging.getLogger("mt5_bot")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STARTUP = 2
EXIT_SAFETY_STOP = 3

MAX_SEND_ATTEMPTS = 3
RECONNECT_START_SECONDS = 5.0
RECONNECT_MAX_SECONDS = 120.0
CONNECT_WAIT_SECONDS = 60.0
HOUSEKEEPING_SECONDS = 60.0
CASH_FLOW_SECONDS = 600.0
FLOW_SECONDS = 300.0
FUNNEL_RETENTION_DAYS = 30.0

REAL_MONEY_HELP = (
    "This terminal is logged in to a REAL MONEY account. The bot only trades real money after one "
    "deliberate confirmation. Either start it once with:\n"
    "    python mt5_bot.py --confirm-real-money\n"
    "or set \"real_money_confirmed\": true in config.json. Read the README risk section first: "
    "losses are possible and no profit is guaranteed."
)

PAUSE_MARKER = "PAUSE_ENTRIES"
MEAN_REVERSION = frozenset({"rsi_reversion", "bollinger", "range_fade", "vwap_reversion"})


class StartupError(RuntimeError):
    """The bot cannot start safely; the message tells the user what to fix (exit code 2)."""


class AccountPolicyError(RuntimeError):
    """The logged-in account is not allowed by the account policy (exit code 3)."""


def select_filling_mode(filling_flags: int, api: Any = None) -> int:
    """Pick a filling type the symbol supports: IOC, then FOK, else RETURN."""
    return select_filling(filling_flags)


def account_policy_problem(mode: str, is_real: bool, confirmed: bool) -> Optional[str]:
    """Reason the account may not be used, or None."""
    if mode == "demo_only" and is_real:
        return "account_mode is \"demo_only\" but the terminal is logged in to a REAL account."
    if mode == "real_only" and not is_real:
        return "account_mode is \"real_only\" but the terminal is logged in to a DEMO account."
    if is_real and not confirmed:
        return REAL_MONEY_HELP
    return None


def resolve_symbol(market: MarketDataPort, wanted: str) -> Optional[str]:
    """Exact name if it exists, else the unique broker variant with a suffix (EURUSD.m, EURUSDm...)."""
    if market.symbol_info(wanted) is not None:
        return wanted
    candidates = market.symbols(f"*{wanted}*")
    base = wanted.upper()
    matches = []
    for info in candidates:
        name = getattr(info, "name", "")
        rest = name[len(base):] if name.upper().startswith(base) else None
        if rest is None or len(rest) > 4:  # suffixes such as .m, m, +, .s, _i, pro
            continue
        matches.append(info)
    if not matches:
        return None
    full = br.SYMBOL_TRADE_MODE_FULL
    tradable = [m for m in matches if getattr(m, "trade_mode", full) == full] or matches
    tradable.sort(key=lambda m: (len(m.name), m.name))
    return tradable[0].name


class Bot(OversightMixin, GatesMixin, OrdersMixin):
    """Trading loop wiring the arms, the team, the risk layer and the broker."""

    def __init__(
        self,
        cfg: Config,
        api: Any = None,
        wait: Optional[Callable[[float], None]] = None,
        env: Optional[Mapping[str, str]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], float] = time.time,
        config_path: str = "config.json",
        confirm_real_money: bool = False,
        signal_source: Any = None,
        notifier: Optional[Notifier] = None,
        spawn: Optional[Callable[..., Any]] = None,
        globber: Callable[[str], list[str]] = glob.glob,
        terminal_killer: Callable[[str], bool] = kill_terminal,
        cli_overrides: Optional[dict[str, Any]] = None,
        team: bool = True,
        advisor: Any = None,
        no_claude: bool = False,
        durable_files: bool = True,
        auto_migrate: bool = True,
        broker: Optional[BrokerPort] = None,
        market: Optional[MarketDataPort] = None,
    ) -> None:
        if broker is None:
            if api is None:
                try:
                    api = load_terminal_api()
                except ImportError as exc:  # pragma: no cover - machines without the package
                    raise StartupError("MetaTrader5 package is not installed. "
                                       "Run: pip install -r requirements.txt") from exc
            live = api is not None and getattr(api, "__name__", "") == "MetaTrader5"
            adapter = Mt5Adapter(api, cfg.ops.symbol_info_ttl_seconds if live else 0.0)
            broker, market = adapter, market or adapter
        self.base_cfg = cfg          # the file config plus command-line overrides
        self.cfg = cfg               # the effective config: base plus the Flow Doctor's bounded knobs
        self.broker: Any = broker
        self.market: Any = market if market is not None else broker
        self.events: Optional[EventLog] = None
        self.engine: Optional[ExecutionEngine] = None
        self.tca: Optional[TcaStore] = None
        self.data: Optional[DataStore] = None
        self.ladder = LadderState()
        self.research_only: Optional[dict[str, Any]] = None
        self._stress_factor = 1.0
        self._margin_freeze: Optional[str] = None
        self._portfolio_info: dict[str, float] = {}
        self._gap_cache: dict[str, tuple[int, np.ndarray]] = {}
        self._next_gap_check = 0.0
        self._next_governance = 0.0
        self._next_clock_check = 0.0
        self._next_bar_capture = 0.0
        self.backups: Optional[BackupSchedule] = None
        self.clock_drift: Optional[float] = None
        self._last_stress_day: Optional[str] = None
        self.schedule = cfg.costs.rules()
        self._next_calibration = 0.0
        self._stamp_base: dict[str, str] = {}
        self._env = os.environ if env is None else env
        self._stop = threading.Event()
        self._stop_signal: Optional[int] = None
        self._wait = wait if wait is not None else self._interruptible_sleep
        self._monotonic = monotonic
        self._utc_now = utc_now
        self.config_path = config_path
        self.confirm_real_money = confirm_real_money
        self._signal_source = signal_source   # test seam: an external proposer on the primary timeframe
        self.notifier = notifier or Notifier(cfg.telegram_enabled, self._env)
        self.alerts = AlertEngine(self.notifier.send, utc_now)
        self._spawn = spawn
        self._globber = globber
        self._terminal_killer = terminal_killer
        self._overrides = dict(cli_overrides or {})
        self._auto_migrate = auto_migrate
        self._kill_path = Path(cfg.kill_switch_file)
        self._last_bar: dict[tuple[str, str], int] = {}
        self._login: Optional[int] = None
        self.is_real = False
        self.hedging = True
        self.symbols: dict[str, str] = {}      # core symbols: configured name -> broker name
        self.universe: list[str] = []          # broker names of every traded symbol (core first)
        self.specs: dict[str, SymbolSpec] = {}
        self.commission: dict[str, float] = {}
        self._next_heartbeat = 0.0
        self._next_housekeeping = 0.0
        self._next_cash_flow = 0.0
        self._next_reload = 0.0
        self._next_flow = 0.0
        self._next_scan = 0.0
        self._next_prune = 0.0
        self._config_mtime: Optional[float] = None
        self._reconnect_delay: Optional[float] = None
        self._next_reconnect = 0.0
        self._slippage_pause: dict[str, float] = {}
        # Self-healing: consecutive execution failures per symbol, and quarantine (until, strikes).
        self._exec_failures: dict[str, int] = {}
        self._quarantine: dict[str, tuple[float, int]] = {}
        self._universe_skipped: set[str] = set()  # candidates the last universe selection rejected
        self._closed_until: dict[str, float] = {}
        self._market_state: dict[str, bool] = {}
        self._next_wait = cfg.poll_seconds
        self._init_path: Optional[str] = None
        self._last_loop_utc: Optional[float] = None
        self._first_loop = True
        self._started_utc = float(utc_now())
        self.news = NewsCalendar()
        self.clock = ServerClock(utc_now=utc_now, monotonic=monotonic)
        self.watchdog = FreezeWatchdog(cfg.terminal_freeze_minutes * 60, monotonic)
        self.registry: Optional[Registry] = None
        self.guard: Optional[EquityGuard] = None
        self.risk: Optional[RiskManager] = None
        self.arms: Optional[ArmEngine] = None
        self.scheduler: Optional[ResearchScheduler] = None
        self.state_dir: Optional[Path] = None
        self.funnel = Funnel(None, clock=utc_now)
        self.capital = CapitalState()
        self.flow_state = flow.FlowState()
        self.flow_active: list[flow.Adjustment] = []
        self.opportunity: list[dict[str, Any]] = []
        self.affordability: list[dict[str, Any]] = []
        self._spread_hourly: dict[str, dict[int, float]] = {}
        self._research_running = False
        self._rng = np.random.default_rng(int(cfg.tiers.seed))
        # Optional specialist team and advisor: a stage of the pipeline, never a separate path.
        self._durable_files = durable_files
        self._team_enabled = team
        self._advisor_override = advisor
        self._no_claude = no_claude
        self.team: Any = None
        self.advisor: Any = None
        self.office: Any = None
        self._closes: dict[str, np.ndarray] = {}
        self._series: dict[tuple[str, str], SeriesBuffer] = {}
        self.stage_ms: dict[str, float] = {}
        self._over_budget = False
        self._order_nonce = 0
        self._started_reconcile = False
        self.dossier_counter = 0
        self._guardian_freeze: Optional[str] = None
        self._entries_while_halted = 0
        self._pause_path: Optional[Path] = None
        self._loop_ms = 0.0
        self._mt5_latency_ms = 0.0

    # ------------------------------------------------------------------ lifecycle

    def request_stop(self, signum: Optional[int] = None) -> None:
        """Safe to call from a signal handler: only sets flags."""
        self._stop_signal = signum
        self._stop.set()

    def _interruptible_sleep(self, seconds: float) -> None:
        # Short slices: on Windows a long Event.wait is not interrupted by Ctrl+C.
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(1.0, remaining))

    def _last_error(self) -> Any:
        return self.broker.last_error()

    def _credentials(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        login = self._env.get("MT5_LOGIN", "").strip()
        password = self._env.get("MT5_PASSWORD", "")
        server = self._env.get("MT5_SERVER", "").strip()
        provided = [bool(login), bool(password), bool(server)]
        if any(provided) and not all(provided):
            raise StartupError(
                "Set all three of MT5_LOGIN, MT5_PASSWORD and MT5_SERVER, or none of them "
                "(to use the account already logged in on the terminal)."
            )
        if all(provided):
            try:
                kwargs["login"] = int(login)
            except ValueError as exc:
                raise StartupError("MT5_LOGIN must be the numeric account number") from exc
            kwargs["password"] = password
            kwargs["server"] = server
        return kwargs

    def _initialize(self) -> bool:
        """Attach to the running terminal, or launch it from the configured/discovered path."""
        creds = self._credentials()
        attempts: list[Optional[str]] = []
        if self._init_path:
            attempts.append(self._init_path)
        elif self.cfg.mt5_path:
            attempts.append(self.cfg.mt5_path)
        else:
            attempts.append(None)
            attempts.extend(discover_terminal_paths(self._env, None, self._globber))
        for path in attempts:
            kwargs = dict(creds)
            if path:
                kwargs["path"] = path
            kwargs["timeout"] = self.cfg.mt5_init_timeout_ms
            log.info("Connecting to MT5 terminal (path=%s, credentials from environment=%s)",
                     path or "already running", "yes" if "login" in creds else "no")
            if self.broker.connect(**kwargs):
                if path:
                    self._init_path = path
                return True
            log.warning("mt5.initialize(%s) failed: %s", path or "", self._last_error())
        return False

    def timeframes(self) -> tuple[str, ...]:
        """Timeframes processed each loop. An external (test) source only uses the primary one."""
        return (self.cfg.timeframe,) if self._signal_source is not None else self.cfg.arm_timeframes()

    def startup(self) -> None:
        """Connect and run every pre-trade safety check. Raises StartupError or AccountPolicyError."""
        if not self._initialize():
            raise StartupError(
                f"mt5.initialize() failed: {self._last_error()}. Is the MT5 terminal installed and logged in? "
                "If several terminals are installed, set mt5_path to terminal64.exe."
            )
        verify = getattr(self.broker, "verify_constants", None)
        wrong = verify() if verify is not None else []
        if wrong:
            raise StartupError("The MetaTrader5 package disagrees with the constants this bot was built on: "
                               + "; ".join(wrong) + ". Update the bot before trading.")
        self._verify_terminal()
        acc = self.broker.account()
        if acc is None:
            raise StartupError(f"account_info() returned nothing: {self._last_error()}. Log in to an account.")
        if not acc.trade_allowed:
            raise StartupError(
                "Trading is not allowed on this account (logged in with the investor/read-only password?)."
            )
        if not acc.trade_expert:
            raise StartupError("Automated (expert) trading is disabled for this account by the broker.")
        self.is_real = not self._is_demo(acc)
        problem = account_policy_problem(self.cfg.account_mode, self.is_real, self.real_money_confirmed)
        if problem:
            raise AccountPolicyError(problem)
        self._login = acc.login
        self.hedging = getattr(acc, "margin_mode", br.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) \
            == br.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

        self.state_dir = account_state_dir(self.cfg.state_root, acc.server, acc.login)
        self._migrate_if_needed()
        self.registry = Registry(db_path(self.state_dir), clock=self._utc_now)
        self.funnel = Funnel(self.registry.tx, clock=self._utc_now)
        self._start_ledger()
        self.guard = EquityGuard(self.state_dir, clock=self._utc_now)
        self.risk = RiskManager(self.cfg, self.is_real)
        self.capital = CapitalState.from_json(self.registry.get_kv("capital_state"))
        self.capital.observe(float(acc.equity), self._utc_now())
        self.flow_state = flow.FlowState.from_json(self.registry.get_kv("flow_state"))
        self.flow_active = self._load_flow_adjustments()
        offset = self.registry.get_kv("server_offset")
        if offset is not None:
            self.clock.offset = int(offset)

        for wanted in self.cfg.symbols:
            name = resolve_symbol(self.market, wanted)
            if name is None or not self.market.select_symbol(name) or self.market.symbol_info(name) is None:
                raise StartupError(
                    f"Symbol {wanted!r} not found (also tried broker suffixes). Open Market Watch (Ctrl+M), "
                    "right-click > Show All, and copy the exact name into config.json."
                )
            if name != wanted:
                log.info("Symbol %s resolved to broker name %s", wanted, name)
            self.symbols[wanted] = name
            self._refresh_spec(name)
        self.universe = list(self.symbols.values())
        if self._signal_source is None:
            self._select_universe(acc)
        self.registry.set_kv("resolved_symbols", json.dumps(self.symbols))
        self.registry.set_kv("universe", json.dumps({"symbols": self.universe, "timeframes": list(self.timeframes())}))
        self.registry.set_kv("is_real", "1" if self.is_real else "0")
        self.registry.set_kv("equity", str(float(acc.equity)))
        if self.cfg.news_filter_enabled:
            self._load_news()
        self._pause_path = self.state_dir / PAUSE_MARKER
        if self._signal_source is None:
            self.arms = ArmEngine(self.registry, self.cfg.tiers, self.cfg.research, clock=self._utc_now,
                                  notify=self.notifier.send, on_tier_change=self._on_tier_change,
                                  management_cfg=self.cfg.management)
            if self.cfg.universe.default_arms:
                created = self.arms.ensure_default_arms(self.universe, self.timeframes(), self.cfg.strategy_names(),
                                                        self.cfg.universe.max_arms)
                if created:
                    log.info("%d new arm(s) created in shadow (T0); evidence decides their tiers", created)
            self.scheduler = ResearchScheduler(self.cfg, self.registry, self.state_dir, self.config_path,
                                               self.refresh_market_data, clock=self._utc_now, spawn=self._spawn)
        self._apply_flow_knobs(log_changes=False)
        self._start_team()
        if self.arms is not None:
            self.arms.refresh(force=True)

        self.banner(acc)
        self.reconcile()
        self.notifier.send(f"Bot {__version__} started on {'REAL' if self.is_real else 'DEMO'} account "
                           f"{mask(acc.login)} ({acc.server}), equity {acc.equity:.2f} {acc.currency}")

    def _start_ledger(self) -> None:
        """Event log, reproducibility stamp and the execution engine (needs the database)."""
        from .stamping import source_hash

        self._stamp_base = make_stamp(source_hash(), config_hash(self.base_cfg))
        self.events = EventLog(self.registry, clock=self._utc_now, stamp=lambda: self._stamp_base)
        journal = OrderJournal(self.registry, self.events, self._utc_now)
        self.engine = ExecutionEngine(self.broker, journal, self.registry, self.cfg.magic,
                                      self.cfg.execution.settings(), self._utc_now, self._monotonic, self._wait,
                                      self.notifier.send)
        self.tca = TcaStore(self.registry, self._utc_now)
        self.data = DataStore(self.state_dir, self.registry, self._utc_now)
        self.ladder = LadderState.from_json(self.registry.get_kv("derisk_ladder"))
        self.research_only = governance.research_only_state(self.registry)
        self._stress_factor = float(self.registry.get_kv("stress_factor") or 1.0)
        learned = self.registry.get_kv("rollover_window")
        if learned:
            self.schedule = replace(self.schedule, **json.loads(learned))
        if self.cfg.ops.backup_enabled and self._durable_files:
            self.backups = BackupSchedule(self.state_dir, db_path(self.state_dir), self.cfg.ops.backup_keep_days,
                                          self.cfg.ops.backup_hour_server, self._utc_now)
        self._version_config()

    def _version_config(self) -> None:
        """Config versioning: every effective change is in the event log, whole (it holds no secrets)."""
        current = config_hash(self.base_cfg)
        previous = self.registry.get_kv("last_config_hash")
        self._stamp_base["config"] = current
        if previous != current:
            self.events.append("config.changed", "config", {"from": previous, "to": current,
                                                            "config": config_snapshot(self.base_cfg)})
            self.registry.set_kv("last_config_hash", current)

    def _ledger(self, kind: str, decision_id: str, payload: Mapping[str, Any], closed: Optional[np.ndarray] = None,
                posterior: Any = None) -> None:
        """One decision event, stamped with what produced it. A logging failure never blocks trading."""
        if self.events is None:
            return
        data = short_hash(closed[-64:].tobytes()) if closed is not None and len(closed) else None
        model = short_hash(repr(posterior.to_dict()).encode("utf-8")) if posterior is not None else None
        try:
            self.events.append(kind, decision_id, payload, decision_id, merge_stamp(self._stamp_base, data=data,
                                                                                    model=model))
        except Exception:  # noqa: BLE001 - the funnel still has the reason; orders have their own durable path
            log.exception("%s not written to the ledger", kind)

    def _migrate_if_needed(self) -> None:
        """First start of v3.1 on a v3.0 install: back up, then merge the old databases in place."""
        from . import upgrade

        if not self._auto_migrate or self.state_dir is None:
            return
        project = Path(self.config_path).resolve().parent
        if upgrade.needs_schema_backup(self.state_dir):
            backup = upgrade.backup_installation(project, Path(self.config_path), Path(self.cfg.state_root),
                                                 Path(self.cfg.health_file), reason="v4_schema")
            log.warning("Database upgrade to version %s: backup in %s. Rollback: python mt5_bot.py --rollback-upgrade",
                        __version__, backup)
        if not upgrade.needs_migration(self.state_dir):
            return
        log.warning("First start of version %s on an older install: backing up and migrating the state", __version__)
        report = upgrade.migrate_installation(project, Path(self.config_path), Path(self.cfg.state_root),
                                              Path(self.cfg.health_file), self.cfg.timeframe)
        log.warning("Migration done. Backup: %s. Rollback: python mt5_bot.py --rollback-upgrade", report["backup"])

    def _select_universe(self, acc: Any) -> None:
        """Core symbols plus the most attractive feasible candidates the broker offers."""
        cap = self.cfg.universe.max_symbols
        extra = int(self.flow_state.knobs.get("universe_extra", 0))
        scored: list[tuple[float, str]] = []
        self._universe_skipped = set()
        for wanted in self.cfg.universe.candidates:
            if wanted in self.symbols:
                continue
            name = resolve_symbol(self.market, wanted)
            if name is None or name in self.universe:
                continue
            try:
                if not self.market.select_symbol(name):
                    continue
                info = self.market.symbol_info(name)
                if info is None or info.trade_mode != br.SYMBOL_TRADE_MODE_FULL:
                    self._universe_skipped.add(name)
                    continue
                spec = self._refresh_spec(name)
                bars = self.market.rates(name, self.cfg.timeframe, 1, 300)
            except Exception:  # noqa: BLE001 - a candidate must never stop startup
                log.debug("candidate %s skipped", wanted, exc_info=True)
                continue
            atr, spread, vol_ratio = atr_and_spread(bars)
            lim = self.risk.limits
            afford = affordability(name, self.cfg.timeframe, (atr or 0.0) * STOP_ATR, spec.value_per_price,
                                   spec.volume_min, self.commission.get(name, 0.0), float(acc.equity),
                                   lim.t1_risk_pct, lim.explore_trade_pct) if atr else None
            if afford is not None and not afford.feasible:
                self._universe_skipped.add(name)
                log.info("Universe: %s skipped: its minimum lot risks %.3f%% of equity at a %.1f ATR stop "
                         "(needs equity >= %.0f for exploration)", name, afford.min_lot_risk_pct, STOP_ATR,
                         afford.min_equity)
                continue
            s = score_symbol(name, spread, atr, spec.point, vol_ratio, True, True, 0.0, None)
            scored.append((s.total, name))
        for _score, name in sorted(scored, reverse=True):
            if len(self.universe) >= cap + extra:
                break
            self.universe.append(name)
        log.info("Universe: %s on %s", ", ".join(self.universe), ", ".join(self.timeframes()))

    def _can_add_symbol(self) -> bool:
        """True when a candidate outside the universe could still be added, i.e. when relaxing
        ``universe_extra`` would change anything (regression: it was relaxed with none left)."""
        for wanted in self.cfg.universe.candidates:
            if wanted in self.symbols or wanted in self._universe_skipped:
                continue
            name = resolve_symbol(self.market, wanted)
            if name is not None and name not in self.universe and name not in self._universe_skipped:
                return True
        return False

    def _notify_advisor(self, text: str) -> None:
        """Advisor outage notifications, if ``notifications.advisor_state`` (read at send time: reloadable)."""
        if self.cfg.notifications.advisor_state:
            self.notifier.send(text)

    def _start_team(self) -> None:
        """Build the optional advisor and the specialist team. A failure here never stops trading:
        the decision pipeline then runs its team stage as neutral (recorded in the funnel)."""
        if not self._team_enabled:
            return
        try:
            from .advisor import build_advisor
            from .officedb import OfficeStore
            from .team.runtime import TeamRuntime
            from .teambridge import menu_lookup as _menu_lookup
            from .teambridge import strategy_menu as _strategy_menu

            self.office = OfficeStore(db_path(self.state_dir), clock=self._utc_now, shared=self.registry)
            self.advisor = self._advisor_override or build_advisor(
                self.cfg, store=self.office, env=self._env, notify=self._notify_advisor, no_claude=self._no_claude,
                strategy_menu=_strategy_menu, menu_lookup=_menu_lookup)
            self.team = TeamRuntime(self.cfg, self.state_dir, self.registry, self.advisor, self.notifier.send,
                                    clock=self._utc_now, monotonic=self._monotonic, store=self.office)
            if self.cfg.claude.enabled and not self._no_claude:
                log.info("Claude advisor: %s", self.advisor.snapshot().get("state"))
        except Exception:  # noqa: BLE001 - the team is optional; the pipeline keeps running without it
            log.exception("The specialist team could not start; its review stage is skipped (neutral)")
            self.team = None

    def _on_tier_change(self, arm: Any, decision: Any, audit_id: int) -> None:
        if decision.tier in ("T2", "T3") or decision.previous in ("T2", "T3"):
            self.notifier.send(f"{arm.symbol}: {arm.short()} {decision.previous} -> {decision.tier} (#{audit_id})",
                               key=f"tier-{arm.id}")
        if self.team is not None:
            from .team.events import ArmTierChanged

            post = arm.posterior
            self.team.bus.publish(ArmTierChanged(arm.id, arm.describe(), decision.previous, decision.tier,
                                                 decision.reason[:240], round(post.p_positive, 4) if post else None),
                                  agent="quant_researcher", symbol=arm.symbol,
                                  severity="notice" if decision.direction == "up" else "warning")

    def _entry_block_reason(self) -> Optional[str]:
        """Why NEW entries are blocked right now (positions are never closed for these reasons)."""
        if self.research_only and self.is_real:
            return f"research-only: {self.research_only.get('reason', 'stop rule')} (--reenable-real after the report)"
        if self._margin_freeze:
            return self._margin_freeze
        if self.guard is not None and self.guard.halted_reason:
            return self.guard.halted_reason
        if self._pause_path is not None and self._pause_path.exists():
            return "new entries paused from the panel (PAUSE_ENTRIES marker in the state folder)"
        if self._guardian_freeze:
            return f"Guardian freeze: {self._guardian_freeze}"
        if self.cfg.capital.floor_soft_stop and self.capital.soft_stop_day is not None and self.guard is not None \
                and self.capital.soft_stop_day == self.guard.state.day:
            return "profit floor reached: new entries stopped for today (soft breaker; --rebase-floor to override)"
        return None

    @property
    def real_money_confirmed(self) -> bool:
        return bool(self.confirm_real_money or self.cfg.real_money_confirmed)

    def banner(self, acc: Any) -> None:
        kind = "REAL MONEY" if self.is_real else "DEMO"
        lim = self.risk.limits
        lines = [
            "=" * 72,
            f"  ACCOUNT {mask(acc.login)} on {acc.server}: {kind}",
            f"  equity {acc.equity:.2f} {acc.currency}, leverage 1:{getattr(acc, 'leverage', '?')}, "
            f"{'hedging' if self.hedging else 'netting'} account",
            f"  risk profile {lim.profile}: per trade T1 {lim.t1_risk_pct:.2f}% / T2 {lim.t2_risk_pct:.2f}% / "
            f"T3 {lim.t3_risk_pct:.2f}%, daily loss {lim.daily_loss_pct:.2f}%, breaker {lim.drawdown_breaker_pct:.1f}%",
            f"  exploration budget {lim.explore_day_pct:.2f}% per day, {lim.explore_week_pct:.2f}% per week; "
            f"profit floor {self.capital.floor(lim):.2f}",
            f"  state folder {self.cfg.state_root}/{account_key(acc.server, acc.login).replace(str(acc.login), mask(acc.login))}",
            f"  mode: {'DRY RUN (no orders will be sent)' if self.cfg.dry_run else 'LIVE ORDERS'}",
            "=" * 72,
        ]
        level = logging.WARNING if self.is_real else logging.INFO
        for line in lines:
            log.log(level, line)
        arms = self.arms.counts() if self.arms is not None else {}
        log.info("Config: universe=%s timeframes=%s magic=%d arms=%s", ",".join(self.universe),
                 ",".join(self.timeframes()), self.cfg.magic, arms or "external source")

    def _verify_terminal(self) -> None:
        deadline = self._monotonic() + CONNECT_WAIT_SECONDS
        term = self.broker.terminal()
        while term is not None and not term.connected and self._monotonic() < deadline:
            log.info("Waiting for the terminal to connect to the broker...")
            self._wait(5)
            if self._stop.is_set():
                break
            term = self.broker.terminal()
        if term is None:
            raise StartupError(f"terminal_info() returned nothing: {self._last_error()}")
        if not term.connected:
            raise StartupError(
                "The terminal is not connected to the broker server. Check the connection status "
                "at the bottom-right of MT5 and log in again (File > Login to Trade Account)."
            )
        if not term.trade_allowed:
            raise StartupError(
                "Algo Trading is disabled in the terminal. Click the 'Algo Trading' toolbar button "
                "(it must be green) and enable Tools > Options > Expert Advisors > Allow algorithmic trading."
            )

    def _is_demo(self, acc: Any) -> bool:
        return acc.trade_mode in (br.ACCOUNT_TRADE_MODE_DEMO, br.ACCOUNT_TRADE_MODE_CONTEST)

    def _runtime_account_problem(self, acc: Any) -> Optional[str]:
        if self._login is not None and acc.login != self._login:
            return "The terminal switched to a different account while the bot was running; stopping."
        if (not self._is_demo(acc)) != self.is_real:
            return "The account type changed while the bot was running; stopping."
        return None

    def shutdown(self) -> None:
        try:
            self.broker.disconnect()
        except Exception:  # noqa: BLE001 - best effort during exit
            log.debug("broker disconnect raised", exc_info=True)

    def close(self) -> None:
        try:
            self.funnel.flush()
        except Exception:  # noqa: BLE001 - best effort during exit
            log.debug("funnel flush at close failed", exc_info=True)
        if self.registry is not None:
            try:
                self.registry.set_kv("capital_state", self.capital.to_json())
                if self.arms is not None:
                    self.arms.flush()
            except Exception:  # noqa: BLE001
                log.debug("capital state not saved at close", exc_info=True)
        if self.advisor is not None:
            self.advisor.close()
            self.advisor = None
        if self.team is not None:
            try:
                self.team.close()
            except Exception:  # noqa: BLE001 - best effort during exit
                log.debug("team close failed", exc_info=True)
            self.team = None
            self.office = None
        for series in self._series.values():
            series.release()
        self._series.clear()
        if self.data is not None:
            try:
                self.data.flush()
            except OSError:
                log.debug("tick flush at close failed", exc_info=True)
        if self.registry is not None:
            self.registry.close()

    def _refresh_spec(self, symbol: str) -> SymbolSpec:
        info = self.market.symbol_info(symbol)
        if info is None:
            if symbol in self.specs:
                return self.specs[symbol]
            raise StartupError(f"symbol_info({symbol}) returned nothing: {self._last_error()}")
        spec = SymbolSpec.from_info(info)
        self.specs[symbol] = spec
        if symbol not in self.commission:
            self.commission[symbol] = self._detect_commission(symbol)
        if self.registry is not None:
            self.registry.store_spec(symbol, spec.to_json(), self.commission[symbol])
        return spec

    def _detect_commission(self, symbol: str) -> float:
        """Round-turn commission per lot: config, else from this account's deal history, else fallback."""
        if self.cfg.commission_per_lot is not None:
            return float(self.cfg.commission_per_lot)
        try:
            now = float(self._utc_now()) + 2 * 86400
            deals = self.broker.deals_between(now - 120 * 86400, now) or ()
            lots = sum(d.volume for d in deals if d.symbol == symbol and d.entry == br.DEAL_ENTRY_IN)
            commission = sum(abs(d.commission) for d in deals if d.symbol == symbol)
            if lots > 0:
                value = commission / lots
                log.info("%s: commission detected from history: %.2f per lot round turn", symbol, value)
                return float(value)
        except Exception:  # noqa: BLE001 - fall back below
            log.debug("commission detection failed", exc_info=True)
        log.info("%s: no deal history to detect commission; using commission_fallback_per_lot %.2f (pessimistic)",
                 symbol, self.cfg.commission_fallback_per_lot)
        return float(self.cfg.commission_fallback_per_lot)

    def _load_news(self) -> None:
        path = Path(self.cfg.news_file)
        try:
            self.news = NewsCalendar.load(path)
            log.info("News filter: %d events loaded from %s", len(self.news.events), path)
        except (OSError, ValueError, KeyError) as exc:
            log.error("News filter enabled but %s could not be read (%s). Entries are blocked until fixed.", path, exc)
            self.news = NewsCalendar([(0.0, "*", "news file unreadable")])

    # ---------------------------------------------------------------- main loop

    def run(self) -> int:
        log.info("Bot running (poll every %.0fs). Create the file %s to stop.", self.cfg.poll_seconds, self._kill_path)
        while not self._stop.is_set():
            code = self.run_once()
            if code is not None:
                return code
            self._wait(self._next_wait)
        log.info("Stop requested (signal %s); exiting. Open positions keep their server-side SL/TP.",
                 self._stop_signal)
        self.notifier.send("Bot stopped (stop requested). Open positions keep their server-side SL/TP.")
        return EXIT_OK

    def run_once(self) -> Optional[int]:
        """One loop iteration. Returns an exit code when the bot must exit, else None."""
        assert self.guard is not None and self.risk is not None and self.registry is not None
        loop_started = time.perf_counter()
        self._last_loop_utc = self._utc_now()
        if self._kill_path.exists():
            log.warning("Kill switch file %s found: closing all bot positions and exiting.", self._kill_path)
            self.notifier.send("Kill switch: closing all bot positions and exiting.")
            closed = self.close_all_positions("kill switch")
            if not closed:
                log.error("Some positions could not be closed; they remain protected by server-side SL/TP.")
            return EXIT_OK if closed else EXIT_ERROR
        if not self._ensure_connected():
            self._next_wait = self.cfg.poll_seconds
            return None
        acc = self.broker.account()
        if acc is None:
            log.warning("account_info() returned nothing: %s", self._last_error())
            return None
        problem = self._runtime_account_problem(acc)
        if problem:
            log.critical(problem)
            self.notifier.send(problem)
            return EXIT_SAFETY_STOP

        self._settle_orders()
        any_fresh = self._timed("ticks", self._observe_ticks)
        server_now = self.clock.now()
        # Deposits/withdrawals must be accounted for BEFORE the loss limits are evaluated,
        # otherwise a withdrawal would trip the drawdown breaker or the daily limit.
        near_limit = (self.guard.drawdown_pct(acc.equity) >= self.risk.max_drawdown_pct
                      or self.guard.daily_loss_pct(acc.equity) >= self.risk.max_daily_loss_pct
                      or self.capital.cushion(float(acc.equity), self.risk.limits) <= 0)
        if near_limit or self._monotonic() >= self._next_cash_flow:
            self._next_cash_flow = self._monotonic() + CASH_FLOW_SECONDS
            self._check_cash_flow()
        events = self.guard.update(server_datetime(server_now).date().isoformat(), acc.equity,
                                   self.risk.max_daily_loss_pct, self.risk.max_drawdown_pct)
        self._handle_guard_events(events, acc, server_now)
        self._update_capital(acc)
        self._maybe_reload_config()
        self._timed("housekeeping", self._housekeeping, acc, server_now)
        self._maybe_heartbeat(acc)
        if self.guard.halted_reason:
            self._keep_flat_while_halted()
        self._weekend_flatten(acc, server_now)

        self._timed("symbols", self._process_universe, acc)
        self._next_wait = self.cfg.poll_seconds if any_fresh else self.cfg.closed_market_poll_seconds
        self._loop_ms = (time.perf_counter() - loop_started) * 1000.0
        over = self._loop_ms > self.cfg.ops.cycle_budget_ms
        if over and not self._over_budget:
            log.warning("Cycle took %.0f ms (budget %.0f ms); optional stages are skipped until it recovers. Stages: "
                        "%s", self._loop_ms, self.cfg.ops.cycle_budget_ms, self.stage_ms)
        self._over_budget = over
        return None

    def _timed(self, stage: str, fn: Callable[..., Any], *args: Any) -> Any:
        started = time.perf_counter()
        try:
            return fn(*args)
        finally:
            self.stage_ms[stage] = round((time.perf_counter() - started) * 1000.0, 2)

    def _process_universe(self, acc: Any) -> None:
        for symbol in list(self.universe):
            if self._stop.is_set():
                break
            try:
                self._process_symbol(symbol, acc)
            except Exception as exc:  # noqa: BLE001 - one symbol must never stop the others
                log.exception("%s: error while processing; continuing with other symbols", symbol)
                self.notifier.send(f"{symbol}: repeated processing error ({type(exc).__name__}); see bot.log",
                                   key=f"error-{symbol}")

    def _update_capital(self, acc: Any) -> None:
        """Risk capital is re-evaluated every loop: deposits, withdrawals and P&L move it at once."""
        equity = float(acc.equity)
        self.capital.observe(equity, self._utc_now())
        lim = self.risk.limits
        stepped = update_ladder(self.ladder, self.guard.drawdown_pct(equity), float(self._utc_now()),
                                self.cfg.tail.rules())
        if stepped.multiplier != self.ladder.multiplier:
            log.warning("De-risking ladder: risk multiplier %.2f -> %.2f at drawdown %.2f%%", self.ladder.multiplier,
                        stepped.multiplier, self.guard.drawdown_pct(equity))
            self.ladder = stepped
            self.registry.set_kv("derisk_ladder", stepped.to_json())
            self.events.append("adaptation.ladder", "risk", {"multiplier": stepped.multiplier,
                                                             "drawdown_pct": self.guard.drawdown_pct(equity)})
        if self.cfg.capital.floor_soft_stop and self.capital.cushion(equity, lim) <= 0 and self.guard.state.day:
            if self.capital.soft_stop_day != self.guard.state.day:
                self.capital.soft_stop_day = self.guard.state.day
                text = (f"Profit floor reached: equity {equity:.2f} <= floor {self.capital.floor(lim):.2f}. New "
                        "entries stop for today (soft breaker); open positions keep their SL/TP.")
                log.warning(text)
                self.notifier.send(text, key="floor")
                self.registry.set_kv("capital_state", self.capital.to_json())

    def _observe_ticks(self) -> bool:
        stale = self.cfg.stale_tick_minutes * 60
        any_fresh = False
        on_tick = getattr(self.broker, "on_tick", None)    # the shadow broker fills stops from live quotes
        for symbol in self.universe:
            tick = self.market.tick(symbol)
            if tick is not None:
                self.clock.observe(symbol, int(tick.time), float(tick.bid), float(tick.ask))
                if on_tick is not None:
                    on_tick(symbol)
                if self.data is not None and self.cfg.research.capture_ticks:
                    self.data.capture_tick(symbol, int(tick.time), float(tick.bid), float(tick.ask))
            fresh = self.clock.is_fresh(symbol, stale)
            any_fresh = any_fresh or fresh
            if self._market_state.get(symbol) != fresh:
                if symbol in self._market_state:
                    log.info("%s: market %s", symbol, "active (ticks moving)" if fresh else
                             f"quiet or closed (no ticks for {self.cfg.stale_tick_minutes:.0f}+ min)")
                self._market_state[symbol] = fresh
        if self.clock.offset is not None and self.registry is not None:
            if self.registry.get_kv("server_offset") != str(self.clock.offset):
                self.registry.set_kv("server_offset", str(self.clock.offset))
                log.info("Broker server time offset detected: UTC%+.2f h", self.clock.offset / 3600)
        return any_fresh

    def _ensure_connected(self) -> bool:
        started = time.perf_counter()
        term = self.broker.terminal()
        self._mt5_latency_ms = (time.perf_counter() - started) * 1000.0
        if term is not None and term.connected:
            if self._reconnect_delay is not None:
                log.info("Terminal connection restored.")
                self._reconnect_delay = None
            return True
        now = self._monotonic()
        if self._reconnect_delay is None:
            self._reconnect_delay = RECONNECT_START_SECONDS
            self._next_reconnect = now + self._reconnect_delay
            log.warning("Terminal disconnected (%s); reconnecting in %.0fs", self._last_error(), self._reconnect_delay)
            return False
        if now < self._next_reconnect:
            return False
        self.shutdown()
        try:
            ok = self._initialize()
        except StartupError as exc:
            log.error("Reconnect impossible: %s", exc)
            ok = False
        term = self.broker.terminal() if ok else None
        if term is not None and term.connected:
            log.info("Reconnected to the MT5 terminal.")
            self._reconnect_delay = None
            return True
        self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_SECONDS)
        self._next_reconnect = now + self._reconnect_delay
        log.warning("Reconnect failed (%s); next attempt in %.0fs", self._last_error(), self._reconnect_delay)
        if self._reconnect_delay >= RECONNECT_MAX_SECONDS:
            self.notifier.send("Terminal still disconnected; retrying every 2 minutes.", key="disconnected")
        return False

    # ------------------------------------------------------------ guard events

    def _handle_guard_events(self, events: list[str], acc: Any, server_now: float) -> None:
        assert self.guard is not None
        if "new_day" in events:
            log.info("New server day %s: start equity %.2f", self.guard.state.day, acc.equity)
            if not self._first_loop:  # nothing to report on the day the bot starts
                self._write_reports(server_now, acc)
        self._first_loop = False
        if "resumed" in events:
            # A human reviewed the drawdown and resumed: a new risk epoch starts, so the profit
            # floor (which sits below the old starting capital) does not keep blocking entries.
            self.capital.rebase(float(acc.equity), self._utc_now())
            self.registry.set_kv("capital_state", self.capital.to_json())
            log.warning("RESUME marker removed: drawdown circuit breaker cleared; peak and profit floor restart from "
                        "%.2f", acc.equity)
            self.notifier.send(f"Drawdown breaker cleared by a human; trading resumes. Equity {acc.equity:.2f}")
        if "drawdown_trip" in events:
            text = (f"DRAWDOWN CIRCUIT BREAKER: equity {acc.equity:.2f} is "
                    f"{self.guard.drawdown_pct(acc.equity):.2f}% below the peak {self.guard.state.peak_equity:.2f} "
                    f"(limit {self.risk.max_drawdown_pct:.1f}%). Closing all bot positions; trading halted until "
                    f"{self.guard.marker} is deleted or --resume is run.")
            log.critical(text)
            self.notifier.send(text)
            self.close_all_positions("drawdown circuit breaker")
        if "daily_limit" in events:
            text = (f"Daily loss limit hit: equity {acc.equity:.2f} vs start {self.guard.state.start_equity:.2f} "
                    f"(-{self.guard.daily_loss_pct(acc.equity):.2f}% >= {self.risk.max_daily_loss_pct:.2f}%). "
                    "Closing bot positions and halting until the next server day.")
            log.critical(text)
            self.notifier.send(text)
            self.close_all_positions("daily loss limit")

    def _keep_flat_while_halted(self) -> None:
        # A close may have failed (disconnect, market closed); retry until flat.
        if self.cfg.dry_run:
            return
        try:
            remaining = self._bot_positions()
        except RuntimeError as exc:
            log.error("%s", exc)
            return
        if remaining:
            self.close_all_positions(f"{self.guard.halted_reason} (retry)")

    def _maybe_heartbeat(self, acc: Any) -> None:
        now = self._monotonic()
        if now < self._next_heartbeat:
            return
        self._next_heartbeat = now + self.cfg.heartbeat_minutes * 60
        try:
            open_count: Any = len(self._bot_positions())
        except RuntimeError:
            open_count = "unknown"
        log.info("Heartbeat: equity %.2f %s, balance %.2f, bot positions %s, halted=%s, day start equity %s",
                 acc.equity, acc.currency, acc.balance, open_count, self.guard.halted_reason or False,
                 f"{self.guard.state.start_equity:.2f}" if self.guard.state.start_equity is not None else "n/a")
        log.info("Status: %s", self.status_line())

    def status_line(self, symbol: Optional[str] = None) -> str:
        """Plain-language status (PT-PT), the same text the panel and --status show."""
        if self.arms is None:
            return "fonte de sinais externa (teste)"
        return self.flow_state.status_text or flow.status_text(self.arms.counts(), None, 0, False,
                                                               self._entry_block_reason())

    # ------------------------------------------------------------ housekeeping

    def _housekeeping(self, acc: Any, server_now: float) -> None:
        now = self._monotonic()
        if now < self._next_housekeeping:
            return
        self._next_housekeeping = now + HOUSEKEEPING_SECONDS
        self.registry.set_kv("equity", str(float(acc.equity)))
        self.registry.set_kv("capital_state", self.capital.to_json())
        try:
            positions = self._bot_positions()
        except RuntimeError as exc:
            log.error("%s", exc)
            positions = None
        if positions is not None:
            if not self.cfg.dry_run and self.engine is not None and not self.engine.journal.unresolved():
                self._adopt_positions(positions, acc)   # an unresolved order explains a position first
            self.protect_positions(positions, acc)
            self._track_closed_live_trades(positions)
            self.registry.snapshot_equity(acc.equity, acc.balance, self.guard.state.peak_equity, len(positions))
            self._run_guardian(acc)
        self._check_terminal_freeze(positions)
        if self.arms is not None:
            try:
                self.arms.refresh()
            except Exception:  # noqa: BLE001 - evidence refresh must never break trading
                log.exception("Arm evidence refresh failed; tiers unchanged")
        self.funnel.flush()
        if self.team is not None:
            try:
                self.team.housekeeping(float(self._utc_now()), self._bot_status(acc, positions, server_now))
            except Exception:  # noqa: BLE001 - the team must never break the trading loop
                log.exception("Team housekeeping failed; trading continues")
        weekend = is_weekend(server_now) and not any(self._market_state.values())
        if self.scheduler is not None:
            try:
                was_running = self._research_running
                self._research_running = self.scheduler.running()
                if was_running and not self._research_running and self.arms is not None:
                    self.arms.reload()
                    self.arms.refresh(force=True)
                    log.info("Research results loaded: %s", self.arms.counts())
                if self.scheduler.maybe_launch(weekend=weekend):
                    self._research_running = True
            except Exception:  # noqa: BLE001 - research must never break trading
                log.exception("Could not schedule research")
        if self._monotonic() >= self._next_scan and self.arms is not None and not self._over_budget:
            self._next_scan = self._monotonic() + self.cfg.universe.scan_every_minutes * 60
            try:
                self._scan_opportunities(acc)
            except Exception:  # noqa: BLE001 - scanning is advisory
                log.exception("Opportunity scan failed")
        if self._monotonic() >= self._next_flow and self.arms is not None and not self._over_budget:
            self._next_flow = self._monotonic() + FLOW_SECONDS
            try:
                self._flow_doctor(acc, server_now, positions)
            except Exception:  # noqa: BLE001 - the Flow Doctor is advisory; bounds are enforced anyway
                log.exception("Flow Doctor failed; knobs unchanged")
        if self._monotonic() >= self._next_prune:
            self._next_prune = self._monotonic() + 6 * 3600
            try:
                funnel_mod.prune(self.registry.conn, self._utc_now() - FUNNEL_RETENTION_DAYS * 86400)
                if self.data is not None:
                    self.data.prune_ticks(self.cfg.research.tick_retention_days, float(self._utc_now()))
                self.registry.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:  # noqa: BLE001
                log.debug("funnel prune failed", exc_info=True)
        self._maybe_calibrate()
        self._tail_checks(acc, positions, server_now)
        self._check_reality_gap()
        self._check_governance(acc)
        self._collect_advisor_reports()
        self._ops_checks(server_now)
        self._capture_bars()
        if self.data is not None:
            self.data.flush()
        self._write_health(acc, positions)

    def _run_guardian(self, acc: Any) -> None:
        """Independent invariant watchdog. If it cannot run, new entries are frozen (fail closed)."""
        if self.team is None:
            self._guardian_freeze = None
            return
        from . import teambridge

        try:
            positions = self._bot_positions()  # after repairs
            report = self.team.guardian_check(teambridge.guardian_snapshot(self, positions, acc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Guardian check failed; freezing new entries until it works again")
            self._guardian_freeze = f"Guardian unavailable ({type(exc).__name__})"
            return
        if report.freeze_reason != self._guardian_freeze:
            if report.freeze_reason:
                log.warning("Guardian froze new entries: %s", report.freeze_reason)
            else:
                log.info("Guardian lifted the entry freeze: all invariants hold again")
        self._guardian_freeze = report.freeze_reason
        if report.kill_reason and not self._kill_path.exists():
            log.critical("Guardian triggered the kill switch: %s", report.kill_reason)
            self.notifier.send(f"Guardian triggered the kill switch: {report.kill_reason}")
            try:
                self._kill_path.write_text(f"Guardian: {report.kill_reason}\n", encoding="utf-8")
            except OSError as exc:
                log.error("Could not create the kill switch file %s: %s", self._kill_path, exc)

    def _bot_status(self, acc: Any, positions: Optional[list[Any]], server_now: float) -> dict[str, Any]:
        latest = self.clock.latest_tick_time
        return {"loop_ms": round(self._loop_ms, 2), "mt5_latency_ms": round(self._mt5_latency_ms, 3),
                "tick_age_s": round(max(0.0, server_now - latest), 1) if latest else None,
                "connected": self._reconnect_delay is None, "open_positions": len(positions or []),
                "weekend": is_weekend(server_now) and not any(self._market_state.values()),
                "flow_status": self.flow_state.status_text}

    def _check_cash_flow(self) -> None:
        credit_types = {br.DEAL_TYPE_CREDIT, br.DEAL_TYPE_CHARGE, br.DEAL_TYPE_CORRECTION, br.DEAL_TYPE_BONUS}
        checked = self.registry.get_kv("cash_flow_checked")
        # First check for this account: existing balance operations are history, already in equity.
        first_check = checked is None
        now = self._utc_now()
        last = float(checked) if checked is not None else now - 365 * 86400
        found = self.broker.deals_between(last - 86400, now + 86400 * 2)
        if found is None:
            return   # unknown: retried on the next pass
        deals = found
        seen = set(json.loads(self.registry.get_kv("cash_flow_seen") or "[]"))
        amount = 0.0
        for d in deals:
            kind = getattr(d, "type", None)
            if (kind == br.DEAL_TYPE_BALANCE or kind in credit_types) and d.ticket not in seen:
                seen.add(d.ticket)
                if not first_check:
                    value = float(d.profit)
                    amount += value
                    self.registry.conn.execute(
                        "INSERT OR IGNORE INTO cash_flows (ts, ticket, amount, kind, comment) VALUES (?, ?, ?, ?, ?)",
                        (float(getattr(d, "time", now) or now), int(d.ticket), value,
                         "deposit" if value > 0 else "withdrawal", str(getattr(d, "comment", ""))[:120]))
        if amount:
            log.info("Deposit/withdrawal of %.2f detected; daily start, drawdown peak, starting capital, high-water "
                     "mark and profit floor rebased (not counted as profit or loss)", amount)
            self.guard.adjust_for_cash_flow(amount)
            self.capital.apply_cash_flow(amount)
            self.registry.set_kv("capital_state", self.capital.to_json())
        self.registry.set_kv("cash_flow_seen", json.dumps(sorted(seen)[-500:]))
        self.registry.set_kv("cash_flow_checked", str(now))

    def _check_terminal_freeze(self, positions: Optional[list[Any]]) -> None:
        if not self.cfg.terminal_restart_enabled:
            return
        stale = self.clock.all_stale_for(self.universe)
        expected = market_expected_open(self.clock.now(), self.cfg.holidays)
        term = self.broker.terminal()
        protected = positions is not None and all(p.sl and p.tp for p in positions)
        ok, why = self.watchdog.should_restart(stale, expected, bool(term and term.connected), protected)
        if not ok:
            return
        exe = terminal_exe_from_info(getattr(term, "path", None)) or self._init_path or self.cfg.mt5_path
        if not exe:
            log.warning("Terminal looks frozen (%s) but its path is unknown; set mt5_path to enable restarts", why)
            self.watchdog.restarted()
            return
        log.warning("Terminal looks frozen (%s); restarting it safely (all bot positions have SL/TP)", why)
        self.notifier.send(f"Restarting a frozen MT5 terminal: {why}")
        self.watchdog.restarted()
        self.shutdown()
        self._terminal_killer(exe)
        self._init_path = exe
        self._reconnect_delay = RECONNECT_START_SECONDS
        self._next_reconnect = self._monotonic() + RECONNECT_START_SECONDS

    # ------------------------------------------------------------ flow doctor

    def _load_flow_adjustments(self) -> list[flow.Adjustment]:
        rows = self.registry.conn.execute(
            "SELECT * FROM flow_adjustments WHERE status = 'active' AND direction = 'relax' ORDER BY id")
        out = []
        for r in rows:
            try:
                evidence = json.loads(r["evidence_json"] or "{}")
            except ValueError:
                evidence = {}
            out.append(flow.Adjustment(int(r["id"]), float(r["ts"]), r["knob"], float(r["old_value"] or 0.0),
                                       float(r["new_value"] or 0.0), r["direction"], r["reason"],
                                       float(r["review_after"] or 0.0), r["status"], evidence))
        return out

    def _apply_flow_knobs(self, log_changes: bool = True) -> None:
        """Effective config = base config + the Flow Doctor's bounded knobs (re-validated)."""
        overrides = flow.overrides(self.base_cfg, self.flow_state)
        try:
            effective = apply_overrides(self.base_cfg, overrides) if overrides else self.base_cfg
        except ValueError as exc:
            log.error("Flow Doctor knobs rejected by validation (%s); using the configured values", exc)
            effective = self.base_cfg
        if effective != self.cfg and log_changes:
            log.info("Flow Doctor knobs applied: %s", overrides or "none (configured values)")
        self.cfg = effective
        if self.risk is not None:
            self.risk.update_config(effective)
        if self.arms is not None:
            self.arms.update_settings(effective.tiers, effective.research)
        if self.team is not None:
            self.team.update_config(effective)

    def trading_window(self) -> tuple[int, int]:
        return tuple(self.cfg.trading_hours_server)  # type: ignore[return-value]

    def _flow_doctor(self, acc: Any, server_now: float, positions: Optional[list[Any]]) -> None:
        fd = self.cfg.flow_doctor
        idle_hours = fd.max_idle_hours_real if self.is_real else fd.max_idle_hours_demo
        now = self._utc_now()
        self.funnel.flush()
        rep = funnel_mod.report(self.registry.conn, now - idle_hours * 3600, now)
        status_rep = funnel_mod.report(self.registry.conn, now - 6 * 3600, now)
        last_order = self.registry.conn.execute(
            "SELECT MAX(open_ts) AS t FROM trades WHERE mode = 'live'").fetchone()["t"]
        trades_since: dict[int, list[float]] = {}
        for adj in self.flow_active:
            if adj.id is None:
                continue
            rows = self.registry.trades(mode="live", status="closed", since=adj.ts)
            trades_since[adj.id] = [float(r.r) for r in rows if r.r is not None]
        lim = self.risk.limits
        inputs = flow.FlowInputs(
            now=now, is_real=self.is_real, markets_open=any(self._market_state.values()),
            last_order_ts=float(last_order) if last_order is not None else None, started_ts=self._started_utc,
            signals_in_window=int(rep.get("signals", 0) or 0), top_blockers=rep.get("top_blockers") or [],
            counterfactuals=rep.get("counterfactuals") or {}, trades_since=trades_since,
            daily_loss_used_frac=max(0.0, self.guard.daily_loss_pct(acc.equity)) / lim.daily_loss_pct
            if lim.daily_loss_pct else 0.0,
            drawdown_frac=self.guard.drawdown_pct(acc.equity) / lim.drawdown_breaker_pct
            if lim.drawdown_breaker_pct else 0.0,
            tier_counts=self.arms.counts(), min_agreement_cfg=self.base_cfg.team.min_agreement,
            can_add_symbol=self._can_add_symbol(),
            relaxations_last_week=int(self.registry.conn.execute(
                "SELECT COUNT(*) AS n FROM flow_adjustments WHERE direction = 'relax' AND ts >= ?",
                (now - 7 * 86400,)).fetchone()["n"]))
        adjustments = flow.evaluate(inputs, self.flow_state, self.flow_active, fd) if fd.enabled else []
        for adj in self.flow_active:
            if adj.status != "active" and adj.id is not None:
                self.registry.conn.execute("UPDATE flow_adjustments SET status = ?, closed_ts = ? WHERE id = ?",
                                           (adj.status, now, adj.id))
        self.flow_active = [a for a in self.flow_active if a.status == "active"]
        for adj in adjustments:
            cur = self.registry.conn.execute(
                "INSERT INTO flow_adjustments (ts, knob, old_value, new_value, direction, reason, evidence_json,"
                " review_after, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (adj.ts, adj.knob, adj.old, adj.new, adj.direction, adj.reason, json.dumps(adj.evidence, default=str),
                 adj.review_after, "active" if adj.direction == "relax" else "applied"))
            adj.id = int(cur.lastrowid)
            log.warning("Flow Doctor %s %s %g -> %g: %s", adj.direction, adj.knob, adj.old, adj.new, adj.reason)
            self.events.append("adaptation.flow", "flow", {"knob": adj.knob, "from": adj.old, "to": adj.new,
                                                           "direction": adj.direction, "evidence": adj.evidence})
            if adj.direction == "relax":
                self.flow_active.append(adj)
            self.registry.log_event("*", f"flow_{adj.direction}", adj.reason[:900],
                                    {"knob": adj.knob, "old": adj.old, "new": adj.new})
        if adjustments:
            flow.apply(self.flow_state, adjustments)
            self._apply_flow_knobs()
            if any(a.knob == "universe_extra" and a.new > a.old for a in adjustments):
                self._expand_universe(acc)
        top = funnel_mod.dominant_blocker(status_rep)
        trading = status_rep.get("orders", 0) > 0 or bool(positions)
        self.flow_state.status_text = flow.status_text(self.arms.counts(), top, 6, trading, self._entry_block_reason())
        self.registry.set_kv("flow_state", self.flow_state.to_json())
        if self.team is not None:
            doctor = self.team.agents.get("flow_doctor")
            if doctor is not None:
                doctor.report(self.flow_state.status_text, adjustments, top)  # type: ignore[attr-defined]

    def _expand_universe(self, acc: Any) -> None:
        before = list(self.universe)
        self._select_universe(acc)
        added = [s for s in self.universe if s not in before]
        if not added or self.arms is None:
            return
        created = self.arms.ensure_default_arms(added, self.timeframes(), self.cfg.strategy_names(),
                                                self.cfg.universe.max_arms)
        self.registry.set_kv("universe", json.dumps({"symbols": self.universe, "timeframes": list(self.timeframes())}))
        if self.scheduler is not None:
            self.scheduler.request(",".join(added), "universe expanded by the Flow Doctor")
        log.warning("Universe expanded with %s (%d new arms in shadow)", ", ".join(added), created)

    # ------------------------------------------------------- opportunity engine

    def _scan_opportunities(self, acc: Any) -> None:
        from .team.agents.market import session_view

        lim = self.risk.limits
        equity = float(acc.equity)
        tier_risk = {"T1": lim.t1_risk_pct, "T2": lim.t2_risk_pct, "T3": lim.t3_risk_pct}
        caps = {"T1": lim.explore_trade_pct, "T2": lim.affordability_cap_pct, "T3": lim.affordability_cap_pct}
        try:
            open_symbols = {p.symbol for p in self._bot_positions()}
        except RuntimeError:
            open_symbols = set()
        best_p: dict[str, float] = {}
        for arm in self.arms.arms.values():
            if arm.posterior is not None:
                best_p[arm.symbol] = max(best_p.get(arm.symbol, 0.0), arm.posterior.p_positive)
        session_open = bool(session_view(self._utc_now())["sessions"])
        scores, rows = [], []
        for symbol in self.universe:
            spec = self.specs.get(symbol)
            if spec is None:
                continue
            for tf in self.timeframes():
                bars = self.market.rates(symbol, tf, 1, 300)
                atr, spread, vol_ratio = atr_and_spread(bars)
                rows.extend(affordability_rows(symbol, tf, atr, spec.value_per_price, spec.volume_min,
                                               self.commission.get(symbol, 0.0), equity, tier_risk, caps))
                if tf != self.cfg.timeframe:
                    continue
                if bars is not None and len(bars) > 20:
                    self._spread_hourly[symbol] = _hourly_spread(bars)
                feasible = any(r["feasible"] for r in rows if r["symbol"] == symbol and r["tier"] == "T1")
                corr = self._max_correlation(symbol, open_symbols - {symbol}) if open_symbols else 0.0
                scores.append(score_symbol(symbol, spread, atr, spec.point, vol_ratio, session_open, feasible, corr,
                                           best_p.get(symbol)))
        self.opportunity = [s.to_dict() for s in rank(scores)]
        self.affordability = rows
        self.registry.set_kv("opportunity", json.dumps({"ts": self._utc_now(), "scores": self.opportunity}))
        self.registry.set_kv("affordability", json.dumps({"ts": self._utc_now(), "equity": equity, "rows": rows},
                                                         default=str))

    def _max_correlation(self, symbol: str, others: set[str]) -> float:
        mine = self._closes.get(symbol)
        best = 0.0
        if mine is None or len(mine) < 30:
            return best
        for other in others:
            theirs = self._closes.get(other)
            if theirs is None or len(theirs) < 30:
                continue
            n = min(len(mine), len(theirs))
            a, b = np.diff(np.log(mine[-n:])), np.diff(np.log(theirs[-n:]))
            if a.std() > 0 and b.std() > 0:
                best = max(best, abs(float(np.corrcoef(a, b)[0, 1])))
        return best

    def _write_health(self, acc: Any, positions: Optional[list[Any]]) -> None:
        lim = self.risk.limits
        equity = float(acc.equity)
        server_now = self.clock.now()
        ranked = self.arms.ranked(400) if self.arms else []
        best: dict[str, list[Any]] = {}
        for arm in ranked:
            rows = best.setdefault(arm.symbol, [])
            if len(rows) < 6:
                rows.append(arm)
        symbols = {}
        for symbol in self.universe:
            symbols[symbol] = {"market": "active" if self._market_state.get(symbol) else "quiet/closed",
                               "arms": [a.to_dict() for a in best.get(symbol, ())]}
        payload = {
            "version": __version__,
            "account": f"{mask(acc.login)} @ {acc.server}",
            "account_type": "REAL" if self.is_real else "DEMO",
            "currency": getattr(acc, "currency", ""),
            "state_dir": str(self.state_dir),
            "equity": round(equity, 2),
            "balance": round(float(acc.balance), 2),
            "peak_equity": self.guard.state.peak_equity,
            "day_start_equity": self.guard.state.start_equity,
            "drawdown_pct": round(self.guard.drawdown_pct(acc.equity), 2),
            "daily_loss_pct": round(max(0.0, self.guard.daily_loss_pct(acc.equity)), 3),
            "open_positions": len(positions) if positions is not None else None,
            "halted": self.guard.halted_reason,
            "dry_run": self.cfg.dry_run,
            "last_loop_utc": datetime.fromtimestamp(self._last_loop_utc or self._utc_now(), tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC"),
            "last_tick_server": fmt_server_time(self.clock.latest_tick_time) if self.clock.latest_tick_time else None,
            "server_utc_offset_hours": None if self.clock.offset is None else self.clock.offset / 3600,
            "universe": self.universe,
            "timeframes": list(self.timeframes()),
            "symbols": symbols,
            "limits": lim.to_dict(),
            "capital": self.capital.snapshot(equity, lim),
            "exploration": self._exploration_room(acc, server_now).to_dict(),
            "tiers": self.arms.counts() if self.arms else {},
            "status_line": self.status_line(),
            "mode": self._mode_chip(),
            "flow": {"knobs": self.flow_state.knobs, "active": [flow.adjustment_row(a) for a in self.flow_active]},
            "quarantined": {sym: round((until - self._monotonic()) / 60.0, 1)
                            for sym, (until, _strikes) in self._quarantine.items() if until > self._monotonic()},
            "research": {"running": bool(self.scheduler and self.scheduler.running()),
                         "last_finished": self.registry.get_kv("last_research_finished"),
                         "last_status": self.registry.get_kv("last_research_status")},
            "entry_block": self._entry_block_reason(),
            "stages_ms": self.stage_ms,
            "loop_ms": round(self._loop_ms, 2),
            "tick_age_seconds": round(max(0.0, server_now - self.clock.latest_tick_time), 1)
            if self.clock.latest_tick_time else None,
            "clock_drift_seconds": self.clock_drift,
            "clock_drift_alert": self.clock_drift is not None
            and abs(self.clock_drift) > self.cfg.ops.clock_drift_warn_seconds,
            "backup_failed": self.backups.failed if self.backups is not None else "",
            "execution": self.engine.snapshot() if self.engine else {},
            "tail": {"margin_level": getattr(acc, "margin_level", None), "ladder": self.ladder.multiplier,
                     "stress_factor": self._stress_factor, "portfolio": self._portfolio_info},
            "research_only": self.research_only,
            "promotions_frozen": self.arms.promotions_frozen if self.arms else None,
            "trading_hours_server": list(self.trading_window()),
            "team": self.team.snapshot() if self.team is not None else {"effective_mode": "baseline",
                                                                         "mode_reason": "team not running"},
            "advisor": {k: v for k, v in (self.advisor.snapshot() if self.advisor is not None else
                                          {"state": "DISABLED"}).items() if k != "history"},
        }
        self.alerts.process(evaluate_rules(payload))
        try:
            write_health(Path(self.cfg.health_file), payload, self._durable_files)
        except OSError as exc:
            log.warning("Could not write %s: %s", self.cfg.health_file, exc)

    def _mode_chip(self) -> str:
        """The panel's global mode: breaker, paused, active (T2/T3), probation (T1) or exploration."""
        if self.guard is not None and self.guard.state.dd_halted:
            return "breaker"
        if self._entry_block_reason():
            return "paused"
        counts = self.arms.counts() if self.arms else {}
        if counts.get("T2") or counts.get("T3"):
            return "active"
        if counts.get("T1"):
            return "probation"
        return "exploration"

    def _write_reports(self, server_now: float, acc: Any) -> None:
        """At a new server day: report on the previous day, and on the previous week on Mondays."""
        if self.state_dir is None:
            return
        offset = self.clock.offset if self.clock.offset is not None else 0
        day_start_utc = (int(server_now) // 86400) * 86400 - offset
        prev_day = server_datetime(server_now - 86400).date().isoformat()
        out = Path(self.cfg.reports_dir) / self.state_dir.name
        desc = f"{'REAL' if self.is_real else 'DEMO'} {mask(self._login)}"
        try:
            write_report(self.registry, out, f"daily_{prev_day}", day_start_utc - 86400, day_start_utc, desc,
                         self.universe)
            stats = trade_stats([r for r in self.registry.trades(mode="live", status="closed")
                                 if day_start_utc - 86400 <= (r.close_ts or 0) < day_start_utc])
            if self.advisor is not None:
                self.advisor.submit_report("daily", governance.report_facts(
                    self.registry.conn, day_start_utc - 86400, day_start_utc, governance.evaluate_account(
                        self.registry.conn, self.cfg.governance.rules(), float(self._utc_now())), float(acc.equity)))
            tiers = self.arms.counts() if self.arms else {}
            self.notifier.send(f"Daily summary {prev_day}: {stats['trades']} live trades, P&L {stats['net_pnl']:.2f}, "
                               f"expectancy {stats['expectancy_r']:+.2f}R, equity {acc.equity:.2f}. Tiers {tiers}",
                               key=f"daily-{prev_day}")
            if server_datetime(server_now).weekday() == 0:
                write_report(self.registry, out, f"weekly_{prev_day}", day_start_utc - 7 * 86400, day_start_utc,
                             desc, self.universe)
                verdict = governance.evaluate_account(self.registry.conn, self.cfg.governance.rules(),
                                                      float(self._utc_now()))
                governance.write_board_report(self.registry.conn, out, day_start_utc - 7 * 86400, day_start_utc,
                                              verdict, desc)
                if self.advisor is not None:
                    self.advisor.submit_report("weekly_board", governance.report_facts(
                        self.registry.conn, day_start_utc - 7 * 86400, day_start_utc, verdict, float(acc.equity)))
            if self.team is not None and self.cfg.notifications.daily_html_report:
                from .reports import write_html_report

                path = write_html_report(self.registry, self.office, out, f"daily_{prev_day}", day_start_utc - 86400,
                                         day_start_utc, desc, self.universe)
                reporter = self.team.agents.get("reporter")
                if reporter is not None:
                    reporter.reported(f"daily_{prev_day}", path.name)
        except OSError as exc:
            log.warning("Could not write reports: %s", exc)

    # ----------------------------------------------------------- config reload

    def _maybe_reload_config(self) -> None:
        now = self._monotonic()
        if now < self._next_reload:
            return
        self._next_reload = now + self.cfg.config_reload_seconds
        try:
            mtime = os.stat(self.config_path).st_mtime
        except OSError:
            return
        if self._config_mtime is None:
            self._config_mtime = mtime
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        self.apply_config_file()

    def apply_config_file(self) -> bool:
        """Validate and apply config.json. Keeps the running config if the new one is invalid."""
        try:
            new = Config.load(self.config_path)
        except ValueError as exc:
            log.error("config.json changed but is invalid; keeping the previous config. Reason: %s", exc)
            self.notifier.send(f"Invalid config.json ignored: {exc}", key="bad-config")
            return False
        if self._overrides:
            try:
                new = apply_overrides(new, self._overrides)
            except ValueError as exc:
                log.error("config.json reload rejected (command-line overrides): %s", exc)
                return False
        try:
            merged, notes = reload_decision(self.base_cfg, new)
        except ValueError as exc:
            log.error("config.json reload rejected: %s", exc)
            return False
        for note in notes:
            log.warning("Config reload: %s", note)
        if merged != self.base_cfg:
            self.base_cfg = merged
            self._version_config()
            self._apply_flow_knobs(log_changes=False)
            if self.scheduler is not None:
                self.scheduler.cfg = self.cfg
            if self.cfg.news_filter_enabled:
                self._load_news()
            if self.advisor is not None:
                self.advisor.update_config(self.cfg.claude)
            log.info("Config reloaded from %s", self.config_path)
        return True

    # ------------------------------------------------------------ per symbol

    def _costs(self, symbol: str) -> Any:
        slippage = self.tca.slippage_points(symbol, self.cfg.backtest_slippage_points) if self.tca else None
        return cost_model(self.cfg, self.commission.get(symbol, self.cfg.commission_fallback_per_lot), symbol, slippage)

    def _record(self, stage: str, outcome: str, reason: str, symbol: Optional[str] = None,
                timeframe: Optional[str] = None, **kwargs: Any) -> None:
        self.funnel.record(stage, outcome, reason, symbol=symbol, timeframe=timeframe, **kwargs)

    def _process_symbol(self, symbol: str, acc: Any) -> None:
        if not self._market_state.get(symbol, True):
            return
        for tf in self.timeframes():
            if self._stop.is_set():
                return
            self._process_bar(symbol, tf, acc)

    def _bar_due(self, key: tuple[str, str], tf: str) -> bool:
        """Event-driven: no broker call until the broker's clock says a bar can have closed."""
        previous = self._last_bar.get(key)
        latest = self.clock.latest_tick_time
        if previous is None or not latest:
            return True
        return latest >= previous + 2 * TIMEFRAME_SECONDS.get(tf, self.cfg.bar_seconds)

    def _closed_series(self, symbol: str, tf: str) -> Optional[np.ndarray]:
        """The closed bars of this symbol and timeframe, or None when no new bar closed.

        Only new rows are fetched and appended, so indicators extend in O(1). The series is rebuilt
        when bars were missed or the broker revised history, and compacted before it outgrows
        twice the configured length."""
        key = (symbol, tf)
        series = self._series.get(key)
        if series is not None:
            head = self.market.rates(symbol, tf, 0, 3)
            if head is None or len(head) < 2:
                log.warning("%s %s: no rate data (%s)", symbol, tf, self._last_error())
                return None
            closed = head[:-1]
            last = series.view()[-1]
            times = closed["time"].astype(np.int64)
            fresh = closed[times > int(last["time"])]
            if not len(fresh):
                return None
            joined = bool(np.any(times <= int(last["time"])))
            same = times == int(last["time"])
            revised = bool(np.any(same)) and float(closed["close"][same][0]) != float(last["close"])
            if joined and not revised and series.free >= len(fresh):
                series.append(fresh)
                return series.view()
            series.release()
            del self._series[key]
        rates = self.market.rates(symbol, tf, 0, self.cfg.bars + 1)
        if rates is None or len(rates) < 2:
            log.warning("%s %s: no rate data (%s)", symbol, tf, self._last_error())
            return None
        rows = rates[:-1]  # the last bar is still forming: never let a strategy see it
        previous = self._last_bar.get(key)
        if previous is not None and int(rows[-1]["time"]) == previous:
            return None
        self._series[key] = SeriesBuffer(np.asarray(rows), 2 * self.cfg.bars + 8)
        return self._series[key].view()

    def _process_bar(self, symbol: str, tf: str, acc: Any) -> None:
        key = (symbol, tf)
        if not self._bar_due(key, tf):
            return
        previous = self._last_bar.get(key)
        closed = self._closed_series(symbol, tf)
        if closed is None:
            return
        bar_time = int(closed[-1]["time"])
        # Recorded before acting, so a failure is not retried on every poll of the same bar.
        self._last_bar[key] = bar_time
        spec = self.specs.get(symbol) or self._refresh_spec(symbol)
        primary = tf == self.cfg.timeframe
        if primary:
            self._closes[symbol] = np.asarray(closed["close"][-(self.cfg.team.correlation_lookback_bars + 1):],
                                              dtype=float)
        self._record("scan", "info", "BAR_EVALUATED", symbol, tf, bar_time=bar_time)
        if self._signal_source is not None:
            intent = self._signal_source.on_bar(symbol, closed, spec, self._costs(symbol), float(acc.equity),
                                                self.risk.risk_pct)
            signals: list[ArmSignal] = []
        else:
            intent = None
            signals = self.arms.on_bar(symbol, tf, closed, spec, self._costs(symbol))
        if previous is None:
            log.info("%s %s: last closed bar %s recorded; waiting for the next one", symbol, tf,
                     fmt_server_time(bar_time))
            return
        if primary:
            self._team_observe(symbol, acc, closed)
        self._manage_positions(symbol, tf, acc, closed)
        self._exits(symbol, tf, closed)
        for sig in signals:
            post = sig.arm.posterior
            self._record("signal", "info", "SIGNAL", symbol, tf, arm_id=sig.arm.id, bar_time=bar_time,
                         values={"strategy": sig.arm.strategy, "side": sig.signal.side, "tier": sig.arm.tier,
                                 "p": None if post is None else round(post.p_positive, 4)})
        if signals:
            intent = self._allocate(symbol, tf, signals, acc, closed)
        elif intent is not None:
            if not isinstance(intent, EntryIntent):
                log.error("%s: signal source returned %r, expected EntryIntent or None", symbol, intent)
                return
            self._record("signal", "info", "SIGNAL", symbol, tf, bar_time=bar_time,
                         values={"strategy": intent.strategy, "side": intent.signal.side, "tier": intent.tier})
        if intent is None:
            log.debug("%s %s: no entry intent on bar %s", symbol, tf, fmt_server_time(bar_time))
            return
        if intent.bar_time is None or intent.timeframe is None:
            intent = replace(intent, bar_time=intent.bar_time or bar_time, timeframe=intent.timeframe or tf)
        sig = intent.signal
        log.info("%s %s: %s signal from %s [%s%s] on bar %s (ATR %.6f)", symbol, tf, sig.side, intent.description,
                 intent.tier, " exploration" if intent.exploration else "", fmt_server_time(bar_time), sig.atr)
        self._enter(symbol, intent, acc, closed)

    def _exits(self, symbol: str, tf: str, closed: np.ndarray) -> None:
        if not self.cfg.close_on_opposite:
            return
        for pos in self._bot_positions(symbol):
            row = self.registry.trade_by_ticket(int(pos.ticket))
            row_tf = (row.timeframe if row is not None else None) or self.cfg.timeframe
            if row_tf != tf:
                continue
            side = self._side_of(pos)
            if self._signal_source is not None:
                wants_exit = self._signal_source.exit_signal(symbol, row.param_set_id if row else None, side, closed)
            else:
                wants_exit = self.arms.exit_signal(row.arm_id if row else None, row.param_set_id if row else None,
                                                   side, closed)
            if wants_exit and not self._close_position(pos, "opposite signal from its own arm"):
                log.warning("%s: could not close position %s on opposite signal", symbol, pos.ticket)

    def _regime(self, symbol: str) -> Optional[str]:
        if self.team is None:
            return None
        detector = self.team.agents.get("regime_detector")
        info = (getattr(detector, "stats", {}) or {}).get("regimes", {}).get(symbol) if detector else None
        return info.get("label") if info else None

    def _allocate(self, symbol: str, tf: str, signals: list[ArmSignal], acc: Any,
                  closed: Optional[np.ndarray] = None) -> Optional[EntryIntent]:
        positions = self._bot_positions()
        busy = "ONE_PER_SYMBOL" if any(p.symbol == symbol for p in positions) else None
        block = self._entry_block_reason()
        if busy is None and block:
            busy = "FLOOR_SOFT_STOP" if "profit floor" in block else funnel_mod.risk_reason_code(
                f"trading halted: {block}")
        open_rows = self.registry.trades(mode="live", status="open")
        exploring = sum(1 for r in open_rows if (r.extra or {}).get("exploration"))
        spec = self.specs.get(symbol)
        lim = self.risk.limits
        equity = float(acc.equity)

        def affordable(sig: ArmSignal) -> Optional[str]:
            if spec is None:
                return None
            tier = "T1" if sig.arm.tier == "T0" else sig.arm.tier
            cap = lim.explore_trade_pct if tier == "T1" else lim.affordability_cap_pct
            stop = sig.signal.atr * (sig.signal.sl_mult or 2.0)
            a = affordability(symbol, tf, stop, spec.value_per_price, spec.volume_min,
                              self.commission.get(symbol, 0.0), equity, lim.tier_risk_pct(tier), cap)
            return None if a.feasible else a.reason

        regime = self._regime(symbol)

        def ineligible(strategy: str) -> Optional[str]:
            if strategy in MEAN_REVERSION and bar_regime not in (None, "range"):
                return f"{strategy} is mean reversion and this bar's regime is {bar_regime}"
            if self.team is None or regime is None:
                return None
            return self.team.regime_ineligible(strategy, regime)

        t = self.cfg.tiers
        bar_regime = None
        if closed is not None and len(closed):
            try:
                bar_regime = regime_mod.label_at(closed)
            except Exception:  # noqa: BLE001 - regime evidence is advisory
                log.debug("%s %s: regime label failed", symbol, tf, exc_info=True)
        inp = AllocationInputs(symbol, tf, signals, busy, exploring, t.exploration_slots, t.exploration_enabled,
                               t.exploration_threshold, affordable, ineligible, bar_regime,
                               lambda sig: self.arms.regime_view(sig.arm, bar_regime or ""),
                               self.cfg.team.regime_min_trades)

        def record(stage: str, outcome: str, reason: str, **kwargs: Any) -> None:
            self._record(stage, outcome, reason, symbol, tf, **kwargs)

        intent = allocate(inp, self._rng, record)
        if intent is not None and not self.cfg.management.policies_enabled and intent.policy != "fixed":
            intent = replace(intent, policy="fixed")
        return intent

    def _team_observe(self, symbol: str, acc: Any, closed: np.ndarray) -> None:
        """New bar: let the team scan the market and manage open positions. Never raises."""
        if self.team is None:
            return
        from . import teambridge

        try:
            self.team.observe_bar(teambridge.decision_context(self, symbol, None, acc, closed, purpose="observe"))
        except Exception:  # noqa: BLE001 - observation is optional
            log.debug("%s: team observation failed", symbol, exc_info=True)

    def _manage_positions(self, symbol: str, tf: str, acc: Any, closed: np.ndarray) -> None:
        """New bar on ``tf``: manage the open positions whose arm trades this timeframe. Never raises."""
        if self.team is None:
            return  # the Position Manager is a team member; without it the positions keep their SL/TP
        from . import teambridge

        try:
            teambridge.manage_positions(self, symbol, acc, closed, tf)
        except Exception:  # noqa: BLE001 - management is optional; SL/TP keep protecting the positions
            log.exception("%s: position management failed; positions keep their SL/TP", symbol)

    def spread_spiking(self, symbol: str) -> bool:
        """True while the live spread is above spread_spike_mult x this hour's typical spread."""
        hourly = self._spread_hourly.get(symbol)
        tick = self.market.tick(symbol)
        spec = self.specs.get(symbol)
        if not hourly or tick is None or spec is None or spec.point <= 0:
            return False
        typical = hourly.get(int((self.clock.now() % 86400) // 3600))
        if not typical or typical <= 0:
            return False
        return (float(tick.ask) - float(tick.bid)) / spec.point > self.cfg.universe.spread_spike_mult * typical

    # ----------------------------------------------------------------- sizing

    def _budget_refs(self, acc: Any, server_now: float) -> tuple[float, float]:
        """Equity at the start of the server day and week (exploration budget references)."""
        day_equity = float(self.guard.state.start_equity or acc.equity)
        week = str(int(server_week_start(server_now)))
        raw = self.registry.get_kv("week_ref")
        ref = json.loads(raw) if raw else {}
        if ref.get("week") != week:
            ref = {"week": week, "equity": day_equity}
            self.registry.set_kv("week_ref", json.dumps(ref))
        return day_equity, float(ref["equity"])

    def _exploration_room(self, acc: Any, server_now: float) -> Any:
        offset = self.clock.offset if self.clock.offset is not None else 0
        week_start_utc = server_week_start(server_now) - offset
        rows = self.registry.trades(mode="live", tier="T1", closed_since=week_start_utc)
        day_eq, week_eq = self._budget_refs(acc, server_now)
        lim = self.risk.limits
        return exploration_room(rows, server_now, offset, day_eq, week_eq, lim.explore_day_pct, lim.explore_week_pct)

    def size(self, symbol: str, intent: EntryIntent, acc: Any) -> SizingDecision:
        """The sizer's verdict for one proposal (the minimum of every constraint)."""
        equity = float(acc.equity)
        positions = self._bot_positions()
        open_risk = sum(r.risk_money for r in self._open_risks(positions, equity))
        budget = None
        if intent.tier == "T1":
            budget = self._exploration_room(acc, self.clock.now()).room
        amc = None
        if intent.arm_id is not None:
            amc = anti_martingale_cap(self.registry.trades(mode="live", arm_id=intent.arm_id, limit=20))
        kelly = None
        if intent.tier in ("T2", "T3") and intent.posterior is not None:
            kelly = intent.posterior.kelly_fraction
        factor = self._portfolio_factor(symbol, positions, equity, intent)
        derisk = min(self.ladder.multiplier, 1.0) * min(self._stress_factor, 1.0)
        inp = SizingInputs(intent.tier, equity, intent.exploration,
                           intent.posterior.mean if intent.posterior else None, kelly, open_risk, budget, amc,
                           portfolio_factor=factor, derisk_factor=derisk)
        return size_risk(inp, self.risk.limits, self.capital)

    def _exception_cap(self, intent: EntryIntent, sizing: Mapping[str, Any]) -> float:
        """Largest minimum-lot risk accepted as a conscious exception for this tier."""
        lim = self.risk.limits
        room = [float(v) for k, v in sizing.items() if k in ("floor_room", "portfolio_heat", "exploration_budget",
                                                             "anti_martingale")]
        base = lim.explore_trade_pct if intent.tier == "T1" else lim.affordability_cap_pct
        return min([base, limits.MAX_RISK_PER_TRADE_PCT] + room)

    def _enter(self, symbol: str, intent: EntryIntent, acc: Any, closed: np.ndarray) -> None:
        """Size the proposal, run the team review (if the team is up) and the hard risk gate."""
        tf = intent.timeframe
        decision_id = f"{symbol}:{tf}:{intent.bar_time}"
        self._ledger("decision.proposed", decision_id,
                     {"symbol": symbol, "timeframe": tf, "side": intent.signal.side, "strategy": intent.strategy,
                      "arm_id": intent.arm_id, "tier": intent.tier, "exploration": intent.exploration,
                      "atr": intent.signal.atr, "bar_time": intent.bar_time, "regime": intent.regime}, closed,
                     intent.posterior)
        open_groups = self._open_group_count()
        if open_groups >= self.cfg.portfolio.max_concurrent_arms:
            self._record("risk", "block", "PORTFOLIO_MAX_ARMS", symbol, tf, arm_id=intent.arm_id,
                         bar_time=intent.bar_time, values={"open": open_groups})
            self._ledger("decision.blocked", decision_id, {"stage": "portfolio", "code": "PORTFOLIO_MAX_ARMS"})
            return
        decision = self.size(symbol, intent, acc)
        self._ledger("decision.sized", decision_id, {"ok": decision.ok, "risk_pct": decision.risk_pct,
                                                     "binding": decision.binding, "constraints": decision.constraints,
                                                     "blocked": decision.blocked})
        if not decision.ok:
            code = decision.blocked or "SIZE_ZERO"
            self._record("sizing", "block", code, symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"binding": decision.binding, **{k: round(v, 4) for k, v in
                                                                 decision.constraints.items()}})
            log.info("%s: no entry: sizing blocked (%s, binding %s)", symbol, code, decision.binding)
            return
        sizing = dict(decision.constraints, binding=decision.binding)
        intent = replace(intent, risk_pct=decision.risk_pct, sizing=sizing)
        if intent.exploration:
            self._record("exploration", "pass", "EXPLORATION", symbol, tf, arm_id=intent.arm_id,
                         bar_time=intent.bar_time, values={"risk_pct": decision.risk_pct, "draw": intent.thompson})
        if self.team is None:
            self._record("team", "info", "TEAM_UNAVAILABLE", symbol, tf, arm_id=intent.arm_id,
                         bar_time=intent.bar_time)
            self._try_open(symbol, intent, acc)
            return
        from . import teambridge

        gateway = teambridge.BotGateway(self, symbol, intent, acc)
        try:
            ctx = teambridge.decision_context(self, symbol, intent, acc, closed)
            result = self.team.decide(ctx, gateway)
        except Exception:  # noqa: BLE001 - the team stage fails neutral, never to a crash or a double order
            log.exception("%s: team review failed; the pipeline continues without its reduction", symbol)
            self._record("team", "info", "TEAM_UNAVAILABLE", symbol, tf, arm_id=intent.arm_id,
                         bar_time=intent.bar_time)
            if not gateway.submitted:
                self._try_open(symbol, intent, acc)
            return
        self._ledger("decision.team", decision_id, {"mode": result.mode, "submitted": result.submitted,
                                                    "reasons": list(result.reasons)[-4:]})
        if not result.submitted:
            reason = result.reasons[-1] if result.reasons else "no trade"
            if "risk layer" in reason:
                code, stage = funnel_mod.risk_reason_code(reason), "risk"
            else:
                code = funnel_mod.team_reason_code(reason)
                stage = funnel_mod.REASONS.get(code, ("team",))[0]
            self._record(stage, "block", code, symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"detail": reason[:200], "mode": result.mode})

    def _open_group_count(self) -> int:
        try:
            positions = self._bot_positions()
        except RuntimeError:
            return 0
        groups = set()
        for risk in self._open_risks(positions, 1.0):
            groups.add(risk.group or risk.symbol)
        return len(groups)

    def _side_of(self, position: Any) -> str:
        return side_of_position(position.type)

    def _bot_positions(self, symbol: Optional[str] = None) -> list[Any]:
        positions = self.broker.positions(symbol)
        if positions is None:
            raise RuntimeError(f"positions_get failed: {self._last_error()}")
        return [p for p in positions if p.magic == self.cfg.magic]

    def _foreign_netting_position(self, symbol: str) -> bool:
        if self.hedging:
            return False
        positions = self.broker.positions(symbol) or ()
        return any(p.magic != self.cfg.magic for p in positions)

    def _loss_per_lot_fn(self, symbol: str) -> Callable[[str, float, float, float], Optional[float]]:
        def fn(side: str, entry: float, sl: float, volume: float) -> Optional[float]:
            profit = self.broker.calc_profit(side, symbol, volume, entry, sl)
            if profit is None or not math.isfinite(profit) or profit >= 0:
                return None
            return -float(profit)

        return fn

    def _open_risks(self, positions: list[Any], equity: float) -> list[OpenRisk]:
        out = []
        for p in positions:
            if p.sl:
                profit = self.broker.calc_profit(side_of_position(p.type), p.symbol, p.volume, p.price_open, p.sl)
                risk = max(-float(profit), 0.0) if profit is not None and math.isfinite(profit) else equity
            else:
                risk = equity  # unprotected: count it as everything at stake until repaired
            group = None
            if self.registry is not None:
                row = self.registry.trade_by_ticket(int(p.ticket))
                group = (row.extra or {}).get("group") if row is not None else None
            out.append(OpenRisk(p.symbol, risk, group))
        return out

    def _notional(self, symbol: str, side: str, volume: float, price: float, acc: Any) -> Optional[float]:
        leverage = float(getattr(acc, "leverage", 0) or 0)
        if leverage <= 0:
            return None
        margin = self.broker.calc_margin(side, symbol, volume, price)
        if margin is None or not math.isfinite(margin) or margin <= 0:
            return None
        return float(margin) * leverage

    def spread_limit(self, symbol: str, sig: Any, spec: SymbolSpec) -> tuple[Optional[float], str]:
        """Per-symbol spread limit in points: absolute cap (if configured), cost relative to the stop
        (unit free, valid for FX, gold, indices and crypto) and a spike filter vs this hour's norm."""
        found: list[tuple[float, str]] = []
        cap = self.cfg.universe.spread_cap(symbol)
        if cap is not None:
            found.append((cap, "configured cap"))
        stop_points = sig.atr * (sig.sl_mult or 2.0) / spec.point if spec.point > 0 else None
        if stop_points:
            found.append((self.cfg.universe.max_spread_to_stop * stop_points, "cost vs stop"))
        hourly = self._spread_hourly.get(symbol)
        if hourly:
            hour = int((self.clock.now() % 86400) // 3600)
            typical = hourly.get(hour)
            if typical and typical > 0:
                found.append((self.cfg.universe.spread_spike_mult * typical, "spike vs typical for this hour"))
        if not found:
            return None, ""
        value, why = min(found)
        return value, why

    def _try_open(self, symbol: str, intent: EntryIntent, acc: Any, multiplier: float = 1.0,
                  dossier_id: Optional[str] = None, preview: bool = False) -> EntryOutcome:
        """Plan, gate and (unless ``preview``) send one entry.

        ``multiplier`` (the team's and the advisor's combined verdict) can only SHRINK the
        sized risk: it is clamped to [0, 1] here again, so no caller can raise the size above
        the sizer's figure. Every hard rule of the risk layer still applies.
        """
        try:
            multiplier = float(multiplier)
        except (TypeError, ValueError):
            multiplier = 0.0
        multiplier = min(max(multiplier, 0.0), limits.MAX_SIZE_MULTIPLIER) if math.isfinite(multiplier) else 0.0
        tf = intent.timeframe

        def note(reason: str, allowed: Optional[bool] = None, level: int = logging.INFO, code: str = "",
                 **extra: Any) -> EntryOutcome:
            code = code or funnel_mod.risk_reason_code(reason)
            if not preview:
                log.log(level, "%s: %s", symbol, reason)
                stage = funnel_mod.REASONS.get(code, ("risk",))[0]
                self._record(stage, "block", code, symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                             values={"detail": reason[:200], "tier": intent.tier})
            return EntryOutcome(allowed, reason, code=code, **extra)

        sig = intent.signal
        info = self.market.symbol_info(symbol)
        tick = self.market.tick(symbol)
        if info is None or tick is None or info.point <= 0 or tick.bid <= 0 or tick.ask <= 0:
            return note(f"no symbol info or valid tick ({self._last_error()}); skipping", level=logging.WARNING,
                        code="NO_TICK")
        if self._closed_until.get(symbol, 0) > self._monotonic():
            return note("market reported closed recently; skipping", code="MARKET_CLOSED")
        until = self._quarantine.get(symbol, (0.0, 0))[0]
        if until > self._monotonic():
            return note(f"symbol quarantined after repeated execution failures ({(until - self._monotonic()) / 60:.0f} "
                        "min left); skipping", code="SYMBOL_QUARANTINED")
        if info.trade_mode != br.SYMBOL_TRADE_MODE_FULL:
            return note(f"trading mode is restricted (trade_mode={info.trade_mode}); skipping",
                        code="TRADE_MODE_RESTRICTED")
        hour = server_datetime(tick.time).hour
        window = self.trading_window()
        if not in_trading_hours(hour, window):
            return note(f"server hour {hour:02d} outside trading window {list(window)}; skipping", code="OUTSIDE_HOURS")
        if self.cfg.costs.gate_enabled:
            blocked = schedule_block(float(tick.time), self.schedule)
            if blocked:
                return note(funnel_mod.describe(blocked), code=blocked)
        if multiplier <= 0:
            return note("size multiplier is zero: no trade", allowed=False, code="TEAM_VETO")
        spec = SymbolSpec.from_info(info)
        self.specs[symbol] = spec
        spread_points = (tick.ask - tick.bid) / info.point
        price = tick.ask if sig.side == "BUY" else tick.bid
        equity = float(acc.equity)
        risk_pct = intent.risk_pct
        sizing = dict(intent.sizing or {})
        if risk_pct is None:  # an external source: sized here, by the same sizer
            decision = self.size(symbol, intent, acc)
            if not decision.ok:
                return note(f"no trade: sizing blocked ({decision.blocked}, binding {decision.binding})",
                            allowed=False, code=decision.blocked or "SIZE_ZERO")
            risk_pct, sizing = decision.risk_pct, dict(decision.constraints, binding=decision.binding)
        exception_cap = self._exception_cap(intent, sizing)
        gap_points = self._gap_points(symbol, tf, price, spec)

        def plan_for(r: float) -> Any:
            return plan_trade(sig.side, sig.atr, sig.sl_mult or 2.0, sig.tp_mult or 3.0, price, spread_points, spec,
                              equity, r, self.commission.get(symbol, 0.0), self.cfg.deviation_points + gap_points,
                              self._loss_per_lot_fn(symbol), min_lot_cap_pct=exception_cap,
                              max_volume=spec.volume_max * limits.MAX_SPLIT_ORDERS)

        base = plan_for(risk_pct)
        result = base if multiplier >= 1.0 else plan_for(risk_pct * multiplier)
        base_volume = base.plan.volume if base.plan is not None else 0.0
        if result.plan is None:
            reason = result.reason
            code = "SL_BELOW_BROKER_MIN" if "broker minimum" in reason else "MIN_LOT_EXCEEDS_RISK"
            if base.plan is not None:
                reason = f"reduced size (x{multiplier:.2f}) is below the minimum volume: {reason}"
                code = "TEAM_SIZE_BELOW_MIN"
            return note(f"no trade: {reason}", allowed=False, base_volume=base_volume, code=code)
        plan = result.plan
        refused = self._cost_gate(symbol, intent, plan, spread_points, spec, info)
        if refused is not None:
            return note(refused, allowed=False, base_volume=base_volume, code="COST_TO_EDGE")
        if plan.exception and not preview:
            self._record("affordability", "info", "MIN_LOT_EXCEPTION", symbol, tf, arm_id=intent.arm_id,
                         bar_time=intent.bar_time, values={"min_lot_risk_pct": round(plan.risk_pct, 4),
                                                           "tier_risk_pct": round(risk_pct * multiplier, 4),
                                                           "cap_pct": round(exception_cap, 4)})
            log.info("%s: minimum lot accepted as a conscious exception: it risks %.3f%% (tier %.3f%%, cap %.3f%%)",
                     symbol, plan.risk_pct, risk_pct * multiplier, exception_cap)
        positions = self._bot_positions()
        news = None
        if self.cfg.news_filter_enabled:
            news = self.news.blocked({spec.currency_base, spec.currency_profit, "*"}, self._utc_now(),
                                     self.cfg.news_minutes_before, self.cfg.news_minutes_after)
        spread_cap, spread_why = self.spread_limit(symbol, sig, spec)
        ctx = EntryContext(
            symbol=symbol, plan=plan, equity=equity, spread_points=spread_points,
            bot_positions=self._open_risks(positions, equity),
            symbol_has_foreign_netting_position=self._foreign_netting_position(symbol),
            notional_money=self._notional(symbol, sig.side, plan.volume, price, acc),
            halted_reason=self._entry_block_reason(), news_blocked=news,
            slippage_paused=self._slippage_pause.get(symbol, 0) > self._monotonic(),
            allowed_risk_pct=risk_pct * multiplier, spread_limit_points=spread_cap, spread_limit_reason=spread_why,
            exception_cap_pct=exception_cap,
        )
        if ctx.notional_money is None:
            return note("cannot compute exposure (order_calc_margin unavailable); skipping (fail closed)",
                        allowed=False, level=logging.WARNING, base_volume=base_volume, code="MARGIN_UNKNOWN")
        guard = self._capital_guards(symbol, spec, plan, ctx, acc)
        if guard is not None:
            return note(guard[1], allowed=False, base_volume=base_volume, code=guard[0])
        allowed, reason = self.risk.approve_entry(ctx)
        if not allowed:
            code = funnel_mod.risk_reason_code(reason)
            if code == "SPREAD_LIMIT" and "spike" in reason:
                code = "SPREAD_SPIKE"
            return note(f"entry rejected by the risk layer: {reason}", allowed=False, base_volume=base_volume,
                        code=code)
        if preview:
            return EntryOutcome(True, "ok", volume=plan.volume, base_volume=base_volume, risk_pct=plan.risk_pct)
        filling = select_filling(info.filling_mode)
        parts = split_volume(plan.volume, spec.volume_max, spec.volume_step, spec.volume_min) \
            if plan.volume > spec.volume_max else [plan.volume]
        if not parts:
            return note("no trade: the planned volume cannot be split into valid orders", allowed=False,
                        code="SIZE_ZERO")
        group = None
        if len(parts) > 1:
            group = oc.client_id("group", symbol, tf, intent.bar_time, intent.arm_id, sig.side)
        opened: list[EntryOutcome] = []
        for index, part in enumerate(parts):
            outcome = self._send_entry(symbol, intent, replace(plan, volume=part), info, tick, filling, multiplier,
                                       dossier_id, base_volume, group, index, len(parts))
            self._note_execution(symbol, outcome)
            if not outcome.ok:
                if not opened:
                    return outcome
                log.error("%s: split order %d/%d failed; %d part(s) already open keep their SL/TP", symbol,
                          index + 1, len(parts), len(opened))
                break
            opened.append(outcome)
            if outcome.dry_run:
                break
        return replace(opened[0], volume=sum(o.volume for o in opened))

    # ------------------------------------------------------- reconciliation

    # ------------------------------------------------------------------ closing

    # ---------------------------------------------------------- market data

    def refresh_market_data(self, mode: str = "full") -> bool:
        """Download history for every symbol and timeframe of the arms into the research cache."""
        months = self.cfg.research.startup_history_months if mode == "startup" else self.cfg.history_months
        ok_any = False
        for symbol in self.universe:
            for tf in self.timeframes():
                count = min(int(months * 30.4375 * 86400 / TIMEFRAME_SECONDS[tf] * 0.75) + 500, 200_000)
                try:
                    rates = self.market.rates(symbol, tf, 1, count)
                except Exception:  # noqa: BLE001
                    log.exception("%s %s: history download failed", symbol, tf)
                    rates = None
                if rates is None or len(rates) == 0:
                    log.warning("%s %s: no history returned (%s). Raise Tools > Options > Charts > Max bars in chart.",
                                symbol, tf, self._last_error())
                    continue
                self.registry.store_bars(symbol, tf, rates)
                ok_any = True
                if len(rates) < count * 0.5:
                    log.info("%s %s: only %d of %d requested bars (the terminal may still be downloading history, "
                             "or 'Max bars in chart' is low); research adapts its windows", symbol, tf, len(rates),
                             count)
            try:
                self._refresh_spec(symbol)
            except StartupError:
                log.warning("%s: symbol specification unavailable during the data refresh", symbol)
        acc = self.broker.account()
        if acc is not None:
            self.registry.set_kv("equity", str(float(acc.equity)))
        return ok_any


def _hourly_spread(bars: np.ndarray) -> dict[int, float]:
    """Median bar spread per server hour (points), needing at least 3 bars in an hour."""
    hours = (np.asarray(bars["time"], dtype=np.int64) % 86400) // 3600
    spreads = np.asarray(bars["spread"], dtype=float)
    out: dict[int, float] = {}
    for h in range(24):
        sel = spreads[hours == h]
        if sel.size >= 3:
            out[h] = float(np.median(sel))
    return out
