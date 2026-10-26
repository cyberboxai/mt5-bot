"""Live trading loop: the ONE decision path from market data to an order.

For every symbol of the universe and every timeframe its arms trade on, each closed bar goes
through the same stages, each emitting reason codes into the decision funnel:

    data -> arms (shadow books, signals) -> allocation (tiers, exploration) -> sizing
    (capital, budget, affordability) -> team review (CIO protocol, optional advisor; can only
    shrink) -> hard risk layer -> execution with server-side SL and TP

All terminal access goes through ``api`` (the MetaTrader5 module). Every order passes
through ``risk`` before it reaches the broker and carries SL and TP, so protection lives on
the broker server even if this process or the PC dies.
"""

from __future__ import annotations

import glob
import json
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import numpy as np

from . import __version__, flow, funnel as funnel_mod, indicators, limits
from . import regime as regime_mod
from .allocation import AllocationInputs, allocate
from .arms import ArmEngine, ArmSignal, EntryIntent
from .capital import CapitalState, SizingDecision, SizingInputs, affordability, anti_martingale_cap, size_risk, \
    split_volume
from .config import Config, apply_overrides, in_trading_hours, reload_decision
from .db import db_path
from .funnel import Funnel
from .market import ServerClock, fmt_server_time, is_weekend, market_expected_open, server_datetime
from .notify import Notifier
from .opportunity import STOP_ATR, affordability_rows, atr_and_spread, rank, score_symbol
from .persistence import account_key, account_state_dir
from .registry import Registry
from .reports import trade_stats, write_health, write_report
from .research import ResearchScheduler, cost_model
from .risk import (EntryContext, EquityGuard, NewsCalendar, OpenRisk, RiskManager, RiskViolation, SymbolSpec,
                   TradePlan, plan_trade, validate_open_request, validate_sl_change)
from .terminal import FreezeWatchdog, discover_terminal_paths, kill_terminal, terminal_exe_from_info
from .tiers import exploration_room, server_week_start

log = logging.getLogger("mt5_bot")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STARTUP = 2
EXIT_SAFETY_STOP = 3

ORDER_COMMENT = "mt5_bot"
MAX_SEND_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 0.5
RECONNECT_START_SECONDS = 5.0
RECONNECT_MAX_SECONDS = 120.0
CONNECT_WAIT_SECONDS = 60.0
HOUSEKEEPING_SECONDS = 60.0
CASH_FLOW_SECONDS = 600.0
FLOW_SECONDS = 300.0
FUNNEL_RETENTION_DAYS = 30.0
MARKET_CLOSED_BACKOFF_SECONDS = 900.0
QUARANTINE_AFTER = 3          # consecutive execution failures before a symbol is paused
QUARANTINE_MINUTES = 30.0
QUARANTINE_MAX_MINUTES = 480.0
EMERGENCY_SL_ATR_MULT = 2.0
EMERGENCY_TP_R = 1.5

# Bit flags of symbol_info.filling_mode (MQL5 SYMBOL_FILLING_*). Defined locally because
# older MetaTrader5 package builds do not export them.
FILLING_FLAG_FOK = 1
FILLING_FLAG_IOC = 2

REAL_MONEY_HELP = (
    "This terminal is logged in to a REAL MONEY account. The bot only trades real money after one "
    "deliberate confirmation. Either start it once with:\n"
    "    python mt5_bot.py --confirm-real-money\n"
    "or set \"real_money_confirmed\": true in config.json. Read the README risk section first: "
    "losses are possible and no profit is guaranteed."
)

PAUSE_MARKER = "PAUSE_ENTRIES"


class StartupError(RuntimeError):
    """The bot cannot start safely; the message tells the user what to fix (exit code 2)."""


@dataclass(frozen=True)
class EntryOutcome:
    """What happened to one entry attempt. ``risk_allowed`` is None when the attempt stopped
    before the risk layer (no quote, market closed, outside trading hours...)."""

    risk_allowed: Optional[bool]
    reason: str
    sent: bool = False
    ok: bool = False
    volume: float = 0.0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    slippage_points: float = 0.0
    ticket: Optional[int] = None
    dry_run: bool = False
    base_volume: float = 0.0
    risk_pct: float = 0.0
    code: str = ""


class AccountPolicyError(RuntimeError):
    """The logged-in account is not allowed by the account policy (exit code 3)."""


def select_filling_mode(filling_flags: int, api: Any) -> int:
    """Pick a filling type the symbol supports: IOC, then FOK, else RETURN."""
    if filling_flags & FILLING_FLAG_IOC:
        return api.ORDER_FILLING_IOC
    if filling_flags & FILLING_FLAG_FOK:
        return api.ORDER_FILLING_FOK
    return api.ORDER_FILLING_RETURN


def account_policy_problem(mode: str, is_real: bool, confirmed: bool) -> Optional[str]:
    """Reason the account may not be used, or None."""
    if mode == "demo_only" and is_real:
        return "account_mode is \"demo_only\" but the terminal is logged in to a REAL account."
    if mode == "real_only" and not is_real:
        return "account_mode is \"real_only\" but the terminal is logged in to a DEMO account."
    if is_real and not confirmed:
        return REAL_MONEY_HELP
    return None


def mask(login: Any) -> str:
    text = str(login)
    return "***" + text[-3:] if len(text) > 3 else "***"


def resolve_symbol(api: Any, wanted: str) -> Optional[str]:
    """Exact name if it exists, else the unique broker variant with a suffix (EURUSD.m, EURUSDm...)."""
    if api.symbol_info(wanted) is not None:
        return wanted
    getter = getattr(api, "symbols_get", None)
    if getter is None:
        return None
    try:
        candidates = getter(group=f"*{wanted}*") or ()
    except TypeError:
        candidates = getter() or ()
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
    full = getattr(api, "SYMBOL_TRADE_MODE_FULL", 4)
    tradable = [m for m in matches if getattr(m, "trade_mode", full) == full] or matches
    tradable.sort(key=lambda m: (len(m.name), m.name))
    return tradable[0].name


class Bot:
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
    ) -> None:
        if api is None:
            try:
                import MetaTrader5 as api  # type: ignore[no-redef]
            except ImportError as exc:  # pragma: no cover - machines without the package
                raise StartupError("MetaTrader5 package is not installed. Run: pip install -r requirements.txt") from exc
        self.base_cfg = cfg          # the file config plus command-line overrides
        self.cfg = cfg               # the effective config: base plus the Flow Doctor's bounded knobs
        self.mt5 = api
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
        self._guardian_freeze: Optional[str] = None
        self._entries_while_halted = 0
        self._pause_path: Optional[Path] = None
        self._loop_ms = 0.0
        self._mt5_latency_ms = 0.0
        self._success_codes = {self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED,
                               self.mt5.TRADE_RETCODE_DONE_PARTIAL}
        self._retry_codes = {self.mt5.TRADE_RETCODE_REQUOTE, self.mt5.TRADE_RETCODE_PRICE_CHANGED,
                             self.mt5.TRADE_RETCODE_PRICE_OFF}
        self._market_closed_code = getattr(self.mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)

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
        try:
            return self.mt5.last_error()
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            return "unknown"

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
            if self.mt5.initialize(**kwargs):
                if path:
                    self._init_path = path
                return True
            log.warning("mt5.initialize(%s) failed: %s", path or "", self._last_error())
        return False

    def _tf(self, timeframe: str) -> int:
        return getattr(self.mt5, f"TIMEFRAME_{timeframe}")

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
        self._verify_terminal()
        acc = self.mt5.account_info()
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
        hedging_mode = getattr(self.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        self.hedging = getattr(acc, "margin_mode", hedging_mode) == hedging_mode

        self.state_dir = account_state_dir(self.cfg.state_root, acc.server, acc.login)
        self._migrate_if_needed()
        self.registry = Registry(db_path(self.state_dir), clock=self._utc_now)
        self.funnel = Funnel(self.registry.tx, clock=self._utc_now)
        self.guard = EquityGuard(self.state_dir)
        self.risk = RiskManager(self.cfg, self.is_real)
        self.capital = CapitalState.from_json(self.registry.get_kv("capital_state"))
        self.capital.observe(float(acc.equity), self._utc_now())
        self.flow_state = flow.FlowState.from_json(self.registry.get_kv("flow_state"))
        self.flow_active = self._load_flow_adjustments()
        offset = self.registry.get_kv("server_offset")
        if offset is not None:
            self.clock.offset = int(offset)

        for wanted in self.cfg.symbols:
            name = resolve_symbol(self.mt5, wanted)
            if name is None or not self.mt5.symbol_select(name, True) or self.mt5.symbol_info(name) is None:
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

    def _migrate_if_needed(self) -> None:
        """First start of v3.1 on a v3.0 install: back up, then merge the old databases in place."""
        from . import upgrade

        if not self._auto_migrate or self.state_dir is None or not upgrade.needs_migration(self.state_dir):
            return
        project = Path(self.config_path).resolve().parent
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
            name = resolve_symbol(self.mt5, wanted)
            if name is None or name in self.universe:
                continue
            try:
                if not self.mt5.symbol_select(name, True):
                    continue
                info = self.mt5.symbol_info(name)
                if info is None or info.trade_mode != self.mt5.SYMBOL_TRADE_MODE_FULL:
                    self._universe_skipped.add(name)
                    continue
                spec = self._refresh_spec(name)
                bars = self.mt5.copy_rates_from_pos(name, self._tf(self.cfg.timeframe), 1, 300)
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
            name = resolve_symbol(self.mt5, wanted)
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
        term = self.mt5.terminal_info()
        while term is not None and not term.connected and self._monotonic() < deadline:
            log.info("Waiting for the terminal to connect to the broker...")
            self._wait(5)
            if self._stop.is_set():
                break
            term = self.mt5.terminal_info()
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
        return acc.trade_mode in (self.mt5.ACCOUNT_TRADE_MODE_DEMO, self.mt5.ACCOUNT_TRADE_MODE_CONTEST)

    def _runtime_account_problem(self, acc: Any) -> Optional[str]:
        if self._login is not None and acc.login != self._login:
            return "The terminal switched to a different account while the bot was running; stopping."
        if (not self._is_demo(acc)) != self.is_real:
            return "The account type changed while the bot was running; stopping."
        return None

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001 - best effort during exit
            log.debug("mt5.shutdown() raised", exc_info=True)

    def close(self) -> None:
        try:
            self.funnel.flush()
        except Exception:  # noqa: BLE001 - best effort during exit
            log.debug("funnel flush at close failed", exc_info=True)
        if self.registry is not None:
            try:
                self.registry.set_kv("capital_state", self.capital.to_json())
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
        if self.registry is not None:
            self.registry.close()

    def _refresh_spec(self, symbol: str) -> SymbolSpec:
        info = self.mt5.symbol_info(symbol)
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
        getter = getattr(self.mt5, "history_deals_get", None)
        if getter is not None:
            try:
                now = datetime.now(timezone.utc) + timedelta(days=2)
                deals = getter(now - timedelta(days=120), now) or ()
                entry_in = getattr(self.mt5, "DEAL_ENTRY_IN", 0)
                lots = sum(d.volume for d in deals if d.symbol == symbol and d.entry == entry_in)
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
        acc = self.mt5.account_info()
        if acc is None:
            log.warning("account_info() returned nothing: %s", self._last_error())
            return None
        problem = self._runtime_account_problem(acc)
        if problem:
            log.critical(problem)
            self.notifier.send(problem)
            return EXIT_SAFETY_STOP

        any_fresh = self._observe_ticks()
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
        self._housekeeping(acc, server_now)
        self._maybe_heartbeat(acc)
        if self.guard.halted_reason:
            self._keep_flat_while_halted()

        for symbol in list(self.universe):
            if self._stop.is_set():
                break
            try:
                self._process_symbol(symbol, acc)
            except Exception as exc:  # noqa: BLE001 - one symbol must never stop the others
                log.exception("%s: error while processing; continuing with other symbols", symbol)
                self.notifier.send(f"{symbol}: repeated processing error ({type(exc).__name__}); see bot.log",
                                   key=f"error-{symbol}")
        self._next_wait = self.cfg.poll_seconds if any_fresh else self.cfg.closed_market_poll_seconds
        self._loop_ms = (time.perf_counter() - loop_started) * 1000.0
        return None

    def _update_capital(self, acc: Any) -> None:
        """Risk capital is re-evaluated every loop: deposits, withdrawals and P&L move it at once."""
        equity = float(acc.equity)
        self.capital.observe(equity, self._utc_now())
        lim = self.risk.limits
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
        for symbol in self.universe:
            tick = self.mt5.symbol_info_tick(symbol)
            if tick is not None:
                self.clock.observe(symbol, int(tick.time), float(tick.bid), float(tick.ask))
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
        term = self.mt5.terminal_info()
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
        term = self.mt5.terminal_info() if ok else None
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
        if self._monotonic() >= self._next_scan and self.arms is not None:
            self._next_scan = self._monotonic() + self.cfg.universe.scan_every_minutes * 60
            try:
                self._scan_opportunities(acc)
            except Exception:  # noqa: BLE001 - scanning is advisory
                log.exception("Opportunity scan failed")
        if self._monotonic() >= self._next_flow and self.arms is not None:
            self._next_flow = self._monotonic() + FLOW_SECONDS
            try:
                self._flow_doctor(acc, server_now, positions)
            except Exception:  # noqa: BLE001 - the Flow Doctor is advisory; bounds are enforced anyway
                log.exception("Flow Doctor failed; knobs unchanged")
        if self._monotonic() >= self._next_prune:
            self._next_prune = self._monotonic() + 6 * 3600
            try:
                funnel_mod.prune(self.registry.conn, self._utc_now() - FUNNEL_RETENTION_DAYS * 86400)
            except Exception:  # noqa: BLE001
                log.debug("funnel prune failed", exc_info=True)
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
        getter = getattr(self.mt5, "history_deals_get", None)
        if getter is None:
            return
        balance_type = getattr(self.mt5, "DEAL_TYPE_BALANCE", 2)
        credit_types = {getattr(self.mt5, name, code) for name, code in
                        (("DEAL_TYPE_CREDIT", 3), ("DEAL_TYPE_CHARGE", 4), ("DEAL_TYPE_CORRECTION", 5),
                         ("DEAL_TYPE_BONUS", 6))}
        checked = self.registry.get_kv("cash_flow_checked")
        # First check for this account: existing balance operations are history, already in equity.
        first_check = checked is None
        now = self._utc_now()
        last = float(checked) if checked is not None else now - 365 * 86400
        try:
            deals = getter(datetime.fromtimestamp(last - 86400, tz=timezone.utc),
                           datetime.fromtimestamp(now + 86400 * 2, tz=timezone.utc)) or ()
        except Exception:  # noqa: BLE001 - retry later
            log.debug("history_deals_get failed", exc_info=True)
            return
        seen = set(json.loads(self.registry.get_kv("cash_flow_seen") or "[]"))
        amount = 0.0
        for d in deals:
            kind = getattr(d, "type", None)
            if (kind == balance_type or kind in credit_types) and d.ticket not in seen:
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
        term = self.mt5.terminal_info()
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
            can_add_symbol=self._can_add_symbol())
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
                bars = self.mt5.copy_rates_from_pos(symbol, self._tf(tf), 1, 300)
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
        symbols = {}
        for symbol in self.universe:
            symbols[symbol] = {"market": "active" if self._market_state.get(symbol) else "quiet/closed",
                               "arms": [a.to_dict() for a in ranked if a.symbol == symbol][:6]}
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
            "trading_hours_server": list(self.trading_window()),
            "team": self.team.snapshot() if self.team is not None else {"effective_mode": "baseline",
                                                                         "mode_reason": "team not running"},
            "advisor": {k: v for k, v in (self.advisor.snapshot() if self.advisor is not None else
                                          {"state": "DISABLED"}).items() if k != "history"},
        }
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
            tiers = self.arms.counts() if self.arms else {}
            self.notifier.send(f"Daily summary {prev_day}: {stats['trades']} live trades, P&L {stats['net_pnl']:.2f}, "
                               f"expectancy {stats['expectancy_r']:+.2f}R, equity {acc.equity:.2f}. Tiers {tiers}",
                               key=f"daily-{prev_day}")
            if server_datetime(server_now).weekday() == 0:
                write_report(self.registry, out, f"weekly_{prev_day}", day_start_utc - 7 * 86400, day_start_utc,
                             desc, self.universe)
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
        return cost_model(self.cfg, self.commission.get(symbol, self.cfg.commission_fallback_per_lot), symbol)

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

    def _process_bar(self, symbol: str, tf: str, acc: Any) -> None:
        key = (symbol, tf)
        head = self.mt5.copy_rates_from_pos(symbol, self._tf(tf), 0, 2)
        if head is None or len(head) < 2:
            log.warning("%s %s: no rate data (%s)", symbol, tf, self._last_error())
            return
        previous = self._last_bar.get(key)
        if previous is not None and int(head[-2]["time"]) == previous:
            return
        rates = self.mt5.copy_rates_from_pos(symbol, self._tf(tf), 0, self.cfg.bars + 1)
        if rates is None or len(rates) < 2:
            log.warning("%s %s: no rate data (%s)", symbol, tf, self._last_error())
            return
        closed = rates[:-1]  # the last bar is still forming: never let a strategy see it
        bar_time = int(closed[-1]["time"])
        # Recorded before acting, so a failure is not retried on every poll of the same bar.
        self._last_bar[key] = bar_time
        if previous is not None and bar_time == previous:
            return
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
        tick = self.mt5.symbol_info_tick(symbol)
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
        inp = SizingInputs(intent.tier, equity, intent.exploration,
                           intent.posterior.mean if intent.posterior else None, kelly, open_risk, budget, amc)
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
        decision = self.size(symbol, intent, acc)
        tf = intent.timeframe
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
        if not result.submitted:
            reason = result.reasons[-1] if result.reasons else "no trade"
            if "risk layer" in reason:
                code, stage = funnel_mod.risk_reason_code(reason), "risk"
            else:
                code = funnel_mod.team_reason_code(reason)
                stage = funnel_mod.REASONS.get(code, ("team",))[0]
            self._record(stage, "block", code, symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"detail": reason[:200], "mode": result.mode})

    def _side_of(self, position: Any) -> str:
        return "BUY" if position.type == self.mt5.POSITION_TYPE_BUY else "SELL"

    def _bot_positions(self, symbol: Optional[str] = None) -> list[Any]:
        positions = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if positions is None:
            raise RuntimeError(f"positions_get failed: {self._last_error()}")
        return [p for p in positions if p.magic == self.cfg.magic]

    def _foreign_netting_position(self, symbol: str) -> bool:
        if self.hedging:
            return False
        positions = self.mt5.positions_get(symbol=symbol) or ()
        return any(p.magic != self.cfg.magic for p in positions)

    def _loss_per_lot_fn(self, symbol: str) -> Callable[[str, float, float, float], Optional[float]]:
        def fn(side: str, entry: float, sl: float, volume: float) -> Optional[float]:
            order_type = self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL
            profit = self.mt5.order_calc_profit(order_type, symbol, volume, entry, sl)
            if profit is None or not math.isfinite(profit) or profit >= 0:
                return None
            return -float(profit)

        return fn

    def _open_risks(self, positions: list[Any], equity: float) -> list[OpenRisk]:
        out = []
        for p in positions:
            if p.sl:
                order_type = self.mt5.ORDER_TYPE_BUY if p.type == self.mt5.POSITION_TYPE_BUY else self.mt5.ORDER_TYPE_SELL
                profit = self.mt5.order_calc_profit(order_type, p.symbol, p.volume, p.price_open, p.sl)
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
        calc = getattr(self.mt5, "order_calc_margin", None)
        leverage = float(getattr(acc, "leverage", 0) or 0)
        if calc is None or leverage <= 0:
            return None
        order_type = self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL
        margin = calc(order_type, symbol, volume, price)
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
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None or info.point <= 0 or tick.bid <= 0 or tick.ask <= 0:
            return note(f"no symbol info or valid tick ({self._last_error()}); skipping", level=logging.WARNING,
                        code="NO_TICK")
        if self._closed_until.get(symbol, 0) > self._monotonic():
            return note("market reported closed recently; skipping", code="MARKET_CLOSED")
        until = self._quarantine.get(symbol, (0.0, 0))[0]
        if until > self._monotonic():
            return note(f"symbol quarantined after repeated execution failures ({(until - self._monotonic()) / 60:.0f} "
                        "min left); skipping", code="SYMBOL_QUARANTINED")
        if info.trade_mode != self.mt5.SYMBOL_TRADE_MODE_FULL:
            return note(f"trading mode is restricted (trade_mode={info.trade_mode}); skipping",
                        code="TRADE_MODE_RESTRICTED")
        hour = server_datetime(tick.time).hour
        window = self.trading_window()
        if not in_trading_hours(hour, window):
            return note(f"server hour {hour:02d} outside trading window {list(window)}; skipping", code="OUTSIDE_HOURS")
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

        def plan_for(r: float) -> Any:
            return plan_trade(sig.side, sig.atr, sig.sl_mult or 2.0, sig.tp_mult or 3.0, price, spread_points, spec,
                              equity, r, self.commission.get(symbol, 0.0), self.cfg.deviation_points,
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
        allowed, reason = self.risk.approve_entry(ctx)
        if not allowed:
            code = funnel_mod.risk_reason_code(reason)
            if code == "SPREAD_LIMIT" and "spike" in reason:
                code = "SPREAD_SPIKE"
            return note(f"entry rejected by the risk layer: {reason}", allowed=False, base_volume=base_volume,
                        code=code)
        if preview:
            return EntryOutcome(True, "ok", volume=plan.volume, base_volume=base_volume, risk_pct=plan.risk_pct)
        filling = select_filling_mode(info.filling_mode, self.mt5)
        parts = split_volume(plan.volume, spec.volume_max, spec.volume_step, spec.volume_min) \
            if plan.volume > spec.volume_max else [plan.volume]
        if not parts:
            return note("no trade: the planned volume cannot be split into valid orders", allowed=False,
                        code="SIZE_ZERO")
        group = uuid.uuid4().hex[:12] if len(parts) > 1 else None
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

    def _note_execution(self, symbol: str, outcome: EntryOutcome) -> None:
        """Self-healing: QUARANTINE_AFTER consecutive execution failures pause new entries on the
        symbol (30 min, doubling per strike up to 8 h); one executed order clears it."""
        if outcome.code == "ORDER_OK":
            self._exec_failures.pop(symbol, None)
            self._quarantine.pop(symbol, None)
            return
        if outcome.code not in ("ORDER_CHECK_FAIL", "ORDER_SEND_FAIL"):
            return
        failures = self._exec_failures.get(symbol, 0) + 1
        self._exec_failures[symbol] = failures
        if failures < QUARANTINE_AFTER:
            return
        strikes = self._quarantine.get(symbol, (0.0, 0))[1] + 1
        minutes = min(QUARANTINE_MINUTES * 2 ** (strikes - 1), QUARANTINE_MAX_MINUTES)
        self._quarantine[symbol] = (self._monotonic() + minutes * 60, strikes)
        self._exec_failures[symbol] = 0
        text = (f"{symbol}: {failures} execution failures in a row; new entries on this symbol paused for "
                f"{minutes:.0f} min (open positions keep their SL/TP)")
        log.warning(text)
        self.notifier.send(text, key=f"quarantine-{symbol}")

    def _send_entry(self, symbol: str, intent: EntryIntent, plan: TradePlan, info: Any, tick: Any, filling: int,
                    multiplier: float, dossier_id: Optional[str], base_volume: float, group: Optional[str],
                    part: int, parts: int) -> EntryOutcome:
        sig = intent.signal
        tf = intent.timeframe

        def build(t: Any) -> dict[str, Any]:
            return self._open_request(symbol, plan, info, t, filling)

        def block(code: str, reason: str, level: int = logging.WARNING, **extra: Any) -> EntryOutcome:
            log.log(level, "%s: %s", symbol, reason)
            self._record("execution", "block", code, symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"detail": reason[:200]})
            return EntryOutcome(True, reason, code=code, base_volume=base_volume, **extra)

        request = build(tick)
        try:
            validate_open_request(request, plan)
        except RiskViolation as exc:
            return block("RISK_STOPS", f"request blocked by the risk layer: {exc}", logging.ERROR)
        check = self.mt5.order_check(request)
        if check is None:
            return block("ORDER_CHECK_FAIL", f"order_check returned nothing ({self._last_error()}); skipping")
        if check.retcode not in (0, self.mt5.TRADE_RETCODE_DONE):
            if check.retcode == self._market_closed_code:
                self._closed_until[symbol] = self._monotonic() + MARKET_CLOSED_BACKOFF_SECONDS
            return block("ORDER_CHECK_FAIL", f"order_check rejected the order: retcode={check.retcode} "
                                             f"comment={check.comment}")
        if self.cfg.dry_run:
            log.info("DRY RUN: would send %s %s %.2f lots at %s SL %s TP %s (order_check OK, risk %.2f, x%.2f, %s)",
                     sig.side, symbol, plan.volume, request["price"], request["sl"], request["tp"], plan.planned_risk,
                     multiplier, intent.tier)
            self._record("execution", "pass", "DRY_RUN", symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"tier": intent.tier, "volume": plan.volume})
            return EntryOutcome(True, "dry run: order_check OK, not sent", sent=False, ok=True, volume=plan.volume,
                                price=float(request["price"]), sl=float(request["sl"]), tp=float(request["tp"]),
                                dry_run=True, base_volume=base_volume, risk_pct=plan.risk_pct, code="DRY_RUN")
        if self.guard.halted_reason:
            self._entries_while_halted += 1  # defence in depth: the Guardian treats this as a kill condition
        sent = self._send_with_retry(build, tick, symbol, plan)
        if sent is None:
            self._record("execution", "block", "ORDER_SEND_FAIL", symbol, tf, arm_id=intent.arm_id,
                         bar_time=intent.bar_time)
            return EntryOutcome(True, "order_send failed (see log)", sent=True, ok=False, base_volume=base_volume,
                                code="ORDER_SEND_FAIL")
        res, final_request = sent
        slippage = abs(float(res.price) - float(final_request["price"])) / info.point if res.price else 0.0
        if slippage > self.cfg.max_slippage_points:
            self._slippage_pause[symbol] = self._monotonic() + self.cfg.slippage_pause_minutes * 60
            log.warning("%s: slippage %.1f points > max %.1f; new entries paused for %.0f min", symbol, slippage,
                        self.cfg.max_slippage_points, self.cfg.slippage_pause_minutes)
            self.notifier.send(f"{symbol}: slippage {slippage:.1f} points; entries paused", key=f"slip-{symbol}")
        volume = float(res.volume or plan.volume)
        risk_money = plan.loss_per_lot * volume
        acc_now = self.mt5.account_info()
        equity = float(acc_now.equity) if acc_now is not None else 0.0
        risk_pct = risk_money / equity * 100.0 if equity > 0 else plan.risk_pct
        extra: dict[str, Any] = {"deal": getattr(res, "deal", None), "strategy": intent.strategy,
                                 "slippage_points": slippage, "signal_bar": intent.bar_time,
                                 "exploration": intent.exploration, "risk_pct": round(risk_pct, 6),
                                 "policy": intent.policy, "regime": intent.regime,
                                 "min_lot_exception": plan.exception,
                                 "sizing_binding": (intent.sizing or {}).get("binding")}
        if group:
            extra.update(group=group, part=part + 1, parts=parts)
        if dossier_id:
            extra.update(dossier_id=dossier_id, size_multiplier=round(multiplier, 6))
        if intent.posterior is not None:
            extra.update(p_positive=round(intent.posterior.p_positive, 4),
                         posterior_mean=round(intent.posterior.mean, 4))
        self.registry.open_trade("live", symbol, intent.param_set_id, sig.side, float(res.price or request["price"]),
                                 final_request["sl"], final_request["tp"], volume, risk_money, ticket=int(res.order),
                                 open_bar_time=intent.bar_time, extra=extra, arm_id=intent.arm_id, timeframe=tf,
                                 tier=intent.tier, risk_pct=risk_pct)
        self._record("execution", "pass", "ORDER_OK", symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                     values={"tier": intent.tier, "volume": volume, "risk_pct": round(risk_pct, 4),
                             "exploration": intent.exploration})
        log.info("%s: opened %s %.2f lots at %s SL %s TP %s (order %s, deal %s, risk %.2f = %.3f%%, %s, size x%.2f)",
                 symbol, sig.side, volume, res.price, final_request["sl"], final_request["tp"], res.order, res.deal,
                 risk_money, risk_pct, intent.tier, multiplier)
        return EntryOutcome(True, "opened", sent=True, ok=True, volume=volume,
                            price=float(res.price or request["price"]), sl=float(final_request["sl"]),
                            tp=float(final_request["tp"]), slippage_points=slippage, ticket=int(res.order),
                            base_volume=base_volume, risk_pct=risk_pct, code="ORDER_OK")

    def _open_request(self, symbol: str, plan: TradePlan, info: Any, tick: Any, filling: int) -> dict[str, Any]:
        point, digits = info.point, info.digits
        if plan.side == "BUY":
            order_type, price = self.mt5.ORDER_TYPE_BUY, tick.ask
            sl, tp = price - plan.sl_points * point, price + plan.tp_points * point
        else:
            order_type, price = self.mt5.ORDER_TYPE_SELL, tick.bid
            sl, tp = price + plan.sl_points * point, price - plan.tp_points * point
        return {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": plan.volume,
            "type": order_type,
            "price": price,
            "sl": round(sl, digits),
            "tp": round(tp, digits),
            "deviation": self.cfg.deviation_points,
            "magic": self.cfg.magic,
            "comment": ORDER_COMMENT,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

    def _send_with_retry(self, build: Callable[[Any], dict[str, Any]], tick: Any, symbol: str,
                         plan: Optional[TradePlan] = None) -> Optional[tuple[Any, dict[str, Any]]]:
        """order_send, retrying only requote / price changed / price off with a fresh tick."""
        request = build(tick)
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            if plan is not None:
                try:
                    validate_open_request(request, plan)
                except RiskViolation as exc:
                    log.error("%s: retry blocked by the risk layer: %s", symbol, exc)
                    return None
            result = self.mt5.order_send(request)
            if result is None:
                log.error("%s: order_send returned nothing: %s", symbol, self._last_error())
                return None
            if result.retcode in self._success_codes:
                return result, request
            if result.retcode in self._retry_codes and attempt < MAX_SEND_ATTEMPTS:
                log.warning("%s: retcode %s (%s) on attempt %d; retrying with a fresh price", symbol, result.retcode,
                            result.comment, attempt)
                self._wait(RETRY_PAUSE_SECONDS)
                fresh = self.mt5.symbol_info_tick(symbol)
                if fresh is None:
                    log.error("%s: no fresh tick for retry (%s)", symbol, self._last_error())
                    return None
                request = build(fresh)
                continue
            if result.retcode == self._market_closed_code:
                self._closed_until[symbol] = self._monotonic() + MARKET_CLOSED_BACKOFF_SECONDS
            log.error("%s: order_send failed: retcode=%s comment=%s", symbol, result.retcode, result.comment)
            return None
        return None

    # ------------------------------------------------------- reconciliation

    def reconcile(self) -> None:
        """Rebuild state from the broker: journal every bot position, verify and repair SL/TP."""
        acc = self.mt5.account_info()
        try:
            positions = self._bot_positions()
        except RuntimeError as exc:
            log.error("Reconciliation: %s", exc)
            return
        recovered = 0
        for p in positions:
            if self.registry.trade_by_ticket(int(p.ticket)) is None:
                risk = self._open_risks([p], float(acc.equity))[0].risk_money if p.sl else \
                    float(acc.equity) * self.risk.risk_pct / 100
                self.registry.open_trade("live", p.symbol, None, self._side_of(p), float(p.price_open),
                                         float(p.sl or 0.0), float(p.tp or 0.0), float(p.volume), risk,
                                         ticket=int(p.ticket), extra={"recovered": True})
                recovered += 1
        orders_get = getattr(self.mt5, "orders_get", None)
        pending = [o for o in (orders_get() or ()) if o.magic == self.cfg.magic] if orders_get else []
        for order in pending:
            if not order.sl or not order.tp:
                log.warning("Pending order %s has no SL/TP; cancelling it (the bot only uses market orders)", order.ticket)
                self._remove_order(order)
            else:
                log.info("Pending order %s with this magic left in place (SL %s, TP %s)", order.ticket, order.sl, order.tp)
        repaired = self.protect_positions(positions, acc)
        self._track_closed_live_trades(positions)
        log.info("Reconciliation: %d bot position(s) open, %d recovered into the journal, %d repaired, "
                 "%d pending order(s)", len(positions), recovered, repaired, len(pending))

    def _remove_order(self, order: Any) -> bool:
        if self.cfg.dry_run:
            log.info("DRY RUN: would cancel pending order %s", order.ticket)
            return True
        request = {"action": getattr(self.mt5, "TRADE_ACTION_REMOVE", 8), "order": order.ticket,
                   "magic": self.cfg.magic, "comment": ORDER_COMMENT}
        result = self.mt5.order_send(request)
        return result is not None and result.retcode in self._success_codes

    def protect_positions(self, positions: list[Any], acc: Any) -> int:
        """Every bot position must carry SL and TP. Adds them when missing; never widens."""
        repaired = 0
        for p in positions:
            if p.sl and p.tp:
                continue
            try:
                if self._repair_position(p, acc):
                    repaired += 1
            except Exception:  # noqa: BLE001 - keep trying the others
                log.exception("%s: could not repair position %s", p.symbol, p.ticket)
        return repaired

    def _repair_position(self, p: Any, acc: Any) -> bool:
        symbol = p.symbol
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            log.error("%s: position %s lacks SL/TP but there is no quote to repair it", symbol, p.ticket)
            return False
        spec = SymbolSpec.from_info(info)
        side = self._side_of(p)
        sign = 1 if side == "BUY" else -1
        entry = float(p.price_open)
        current = tick.bid if side == "BUY" else tick.ask
        risk_money = float(acc.equity) * self.risk.risk_pct / 100.0
        dist = risk_money / (spec.value_per_price * float(p.volume))
        rates = self.mt5.copy_rates_from_pos(symbol, self._tf(self.cfg.timeframe), 0, 100)
        if rates is not None and len(rates) > 20:
            a = indicators.atr(rates["high"], rates["low"], rates["close"], 14)[-2]
            if np.isfinite(a) and a > 0:
                dist = min(dist, float(a) * EMERGENCY_SL_ATR_MULT)
        gap = (spec.stops_level + (tick.ask - tick.bid) / spec.point + 1) * spec.point
        new_sl = float(p.sl) if p.sl else round(entry - sign * dist, spec.digits)
        if not p.sl and (current - new_sl) * sign <= gap:
            log.critical("%s: position %s has no SL and is already beyond its allowed loss; closing it now",
                         symbol, p.ticket)
            self.notifier.send(f"{symbol}: unprotected position {p.ticket} beyond its risk limit was closed")
            return self._close_position(p, "unprotected and beyond the risk limit")
        new_tp = float(p.tp) if p.tp else round(entry + sign * dist * EMERGENCY_TP_R, spec.digits)
        if not p.tp and (new_tp - current) * sign <= gap:
            new_tp = round(current + sign * max(gap * 2, dist), spec.digits)
        validate_sl_change(side, float(p.sl or 0.0), new_sl)
        log.warning("%s: position %s missing %s; setting SL %s TP %s", symbol, p.ticket,
                    "SL and TP" if not p.sl and not p.tp else ("SL" if not p.sl else "TP"), new_sl, new_tp)
        if self.cfg.dry_run:
            log.info("DRY RUN: would modify position %s SL %s TP %s", p.ticket, new_sl, new_tp)
            return True
        request = {"action": getattr(self.mt5, "TRADE_ACTION_SLTP", 6), "symbol": symbol, "position": p.ticket,
                   "sl": new_sl, "tp": new_tp, "magic": self.cfg.magic, "comment": ORDER_COMMENT}
        result = self.mt5.order_send(request)
        if result is None or result.retcode not in self._success_codes:
            log.error("%s: SL/TP repair of %s failed: %s", symbol, p.ticket,
                      getattr(result, "retcode", self._last_error()))
            return False
        row = self.registry.trade_by_ticket(int(p.ticket))
        if row is not None:
            self.registry.update_trade_sl(row.id, new_sl, new_tp)
        self.notifier.send(f"{symbol}: protective SL/TP set on position {p.ticket}")
        return True

    def _track_closed_live_trades(self, positions: list[Any]) -> None:
        """Close journal rows whose broker position disappeared, using the deal history."""
        open_tickets = {int(p.ticket) for p in positions}
        getter = getattr(self.mt5, "history_deals_get", None)
        for row in self.registry.trades(mode="live", status="open"):
            if row.ticket is None or int(row.ticket) in open_tickets or getter is None:
                continue
            try:
                deals = getter(position=int(row.ticket)) or ()
            except Exception:  # noqa: BLE001 - retry on the next pass
                log.debug("history_deals_get(position=%s) failed", row.ticket, exc_info=True)
                continue
            out_types = {getattr(self.mt5, "DEAL_ENTRY_OUT", 1), getattr(self.mt5, "DEAL_ENTRY_OUT_BY", 3)}
            exits = [d for d in deals if getattr(d, "entry", None) in out_types]
            if not exits:
                continue
            pnl = sum(float(d.profit) + float(d.commission) + float(d.swap) + float(getattr(d, "fee", 0.0))
                      for d in deals)
            exit_price = float(exits[-1].price)
            r = pnl / row.risk_money if row.risk_money > 0 else 0.0
            self.registry.close_trade(row.id, exit_price, pnl, r, "closed at broker")
            log.info("%s: position %s closed at %s, P&L %.2f (%.2fR, %s)", row.symbol, row.ticket, exit_price, pnl, r,
                     row.tier or "?")
            if self.team is not None:
                from .team.events import TradeClosed

                self.team.bus.publish(TradeClosed("live", round(r, 4), round(pnl, 2), r > 0, "closed at broker"),
                                      agent="performance_analyst", symbol=row.symbol,
                                      correlation_id=(row.extra or {}).get("dossier_id"),
                                      severity="notice")

    # ------------------------------------------------------------------ closing

    def _close_position(self, position: Any, reason: str, volume: Optional[float] = None) -> bool:
        """Close a bot position (or ``volume`` lots of it, for a partial close)."""
        if position.magic != self.cfg.magic:  # defence in depth: never touch foreign positions
            return False
        close_volume = float(position.volume) if volume is None else min(float(volume), float(position.volume))
        symbol = position.symbol
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            log.error("%s: cannot close position %s: no symbol info/tick (%s)", symbol, position.ticket,
                      self._last_error())
            return False
        is_buy = position.type == self.mt5.POSITION_TYPE_BUY
        filling = select_filling_mode(info.filling_mode, self.mt5)

        def build(t: Any) -> dict[str, Any]:
            request = {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": close_volume,
                "type": self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY,
                "price": t.bid if is_buy else t.ask,
                "deviation": self.cfg.deviation_points,
                "magic": self.cfg.magic,
                "comment": ORDER_COMMENT,
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            # Hedging: close this exact position. Netting: one position per symbol, an opposite deal closes it.
            if self.hedging:
                request["position"] = position.ticket
            return request

        if self.cfg.dry_run:
            log.info("DRY RUN: would close position %s (%s %.2f %s) because: %s", position.ticket,
                     "BUY" if is_buy else "SELL", close_volume, symbol, reason)
            return True
        result = self._send_with_retry(build, tick, symbol)
        if result is None:
            return False
        log.info("%s: closed position %s (%s)", symbol, position.ticket, reason)
        return True

    def close_all_positions(self, reason: str) -> bool:
        """Close every position with this bot's magic. Returns True if all closed."""
        try:
            positions = self._bot_positions()
        except RuntimeError as exc:
            log.error("Cannot list positions to close: %s", exc)
            return False
        if not positions:
            log.info("No bot positions to close (%s).", reason)
            return True
        all_closed = True
        for pos in positions:
            all_closed = self._close_position(pos, reason) and all_closed
        return all_closed

    # ---------------------------------------------------------- market data

    def refresh_market_data(self, mode: str = "full") -> bool:
        """Download history for every symbol and timeframe of the arms into the research cache."""
        from .config import TIMEFRAME_SECONDS

        months = self.cfg.research.startup_history_months if mode == "startup" else self.cfg.history_months
        ok_any = False
        for symbol in self.universe:
            for tf in self.timeframes():
                count = min(int(months * 30.4375 * 86400 / TIMEFRAME_SECONDS[tf] * 0.75) + 500, 200_000)
                try:
                    rates = self.mt5.copy_rates_from_pos(symbol, self._tf(tf), 1, count)
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
        acc = self.mt5.account_info()
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
