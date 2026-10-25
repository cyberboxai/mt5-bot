"""Live trading loop. All terminal access goes through ``api`` (the MetaTrader5 module).

Every order passes through ``risk`` before it reaches the broker, and every order carries
SL and TP, so protection lives on the broker server even if this process or the PC dies.
"""

from __future__ import annotations

import glob
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import numpy as np

from . import __version__, indicators, limits
from .config import Config, apply_overrides, in_trading_hours, reload_decision
from .cycle import OptimizerScheduler, cost_model
from .engine import AdaptiveEngine, EngineSettings, EntryIntent
from .market import ServerClock, fmt_server_time, is_weekend, market_expected_open, server_datetime
from .notify import Notifier
from .persistence import account_key, account_state_dir
from .registry import Registry
from .reports import trade_stats, write_health, write_report
from .risk import (EntryContext, EquityGuard, NewsCalendar, OpenRisk, RiskManager, RiskViolation, SymbolSpec,
                   TradePlan, plan_trade, validate_open_request, validate_sl_change)
from .terminal import FreezeWatchdog, discover_terminal_paths, kill_terminal, terminal_exe_from_info

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
MARKET_CLOSED_BACKOFF_SECONDS = 900.0
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
    """Trading loop wiring the adaptive engine, the risk layer and the broker."""

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
    ) -> None:
        if api is None:
            try:
                import MetaTrader5 as api  # type: ignore[no-redef]
            except ImportError as exc:  # pragma: no cover - machines without the package
                raise StartupError("MetaTrader5 package is not installed. Run: pip install -r requirements.txt") from exc
        self.cfg = cfg
        self.mt5 = api
        self._env = os.environ if env is None else env
        self._stop = threading.Event()
        self._stop_signal: Optional[int] = None
        self._wait = wait if wait is not None else self._interruptible_sleep
        self._monotonic = monotonic
        self._utc_now = utc_now
        self.config_path = config_path
        self.confirm_real_money = confirm_real_money
        self._signal_source = signal_source
        self.notifier = notifier or Notifier(cfg.telegram_enabled, self._env)
        self._spawn = spawn
        self._globber = globber
        self._terminal_killer = terminal_killer
        self._overrides = dict(cli_overrides or {})
        self._timeframe = getattr(self.mt5, f"TIMEFRAME_{cfg.timeframe}")
        self._kill_path = Path(cfg.kill_switch_file)
        self._last_bar: dict[str, int] = {}
        self._login: Optional[int] = None
        self.is_real = False
        self.hedging = True
        self.symbols: dict[str, str] = {}
        self.specs: dict[str, SymbolSpec] = {}
        self.commission: dict[str, float] = {}
        self._next_heartbeat = 0.0
        self._next_housekeeping = 0.0
        self._next_cash_flow = 0.0
        self._next_reload = 0.0
        self._config_mtime: Optional[float] = None
        self._reconnect_delay: Optional[float] = None
        self._next_reconnect = 0.0
        self._slippage_pause: dict[str, float] = {}
        self._closed_until: dict[str, float] = {}
        self._market_state: dict[str, bool] = {}
        self._next_wait = cfg.poll_seconds
        self._init_path: Optional[str] = None
        self._last_loop_utc: Optional[float] = None
        self._first_loop = True
        self.news = NewsCalendar()
        self.clock = ServerClock(utc_now=utc_now, monotonic=monotonic)
        self.watchdog = FreezeWatchdog(cfg.terminal_freeze_minutes * 60, monotonic)
        self.registry: Optional[Registry] = None
        self.guard: Optional[EquityGuard] = None
        self.risk: Optional[RiskManager] = None
        self.engine: Optional[AdaptiveEngine] = None
        self.scheduler: Optional[OptimizerScheduler] = None
        self.state_dir: Optional[Path] = None
        # v3: optional specialist team and advisor. The deterministic core never depends on them.
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
        self.registry = Registry(self.state_dir / "registry.sqlite", clock=self._utc_now)
        self.guard = EquityGuard(self.state_dir)
        self.risk = RiskManager(self.cfg, self.is_real)
        if self._signal_source is None:
            self.scheduler = OptimizerScheduler(self.cfg, self.registry, self.state_dir, self.config_path,
                                                self.refresh_market_data, clock=self._utc_now, spawn=self._spawn)
            self.engine = AdaptiveEngine(self.registry, EngineSettings.from_config(self.cfg),
                                         notify=self.notifier.send, request_reoptimization=self.scheduler.request,
                                         clock=self._utc_now)
            self._signal_source = self.engine
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
        self.registry.set_kv("resolved_symbols", json.dumps(self.symbols))
        self.registry.set_kv("is_real", "1" if self.is_real else "0")
        self.registry.set_kv("equity", str(float(acc.equity)))
        if self.cfg.news_filter_enabled:
            self._load_news()
        self._pause_path = self.state_dir / PAUSE_MARKER
        self._start_team()

        self.banner(acc)
        self.reconcile()
        self.notifier.send(f"Bot {__version__} started on {'REAL' if self.is_real else 'DEMO'} account "
                           f"{mask(acc.login)} ({acc.server}), equity {acc.equity:.2f} {acc.currency}")

    def _notify_advisor(self, text: str) -> None:
        """Advisor outage notifications, if ``notifications.advisor_state`` (read at send time: reloadable)."""
        if self.cfg.notifications.advisor_state:
            self.notifier.send(text)

    def _start_team(self) -> None:
        """Build the optional advisor and the specialist team. A failure here never stops trading."""
        if not self._team_enabled:
            return
        try:
            from .advisor import build_advisor
            from .officedb import OfficeStore, office_path
            from .team.runtime import TeamRuntime
            from .teambridge import menu_lookup as _menu_lookup
            from .teambridge import strategy_menu as _strategy_menu

            self.office = OfficeStore(office_path(self.state_dir), clock=self._utc_now)
            self.advisor = self._advisor_override or build_advisor(
                self.cfg, store=self.office, env=self._env, notify=self._notify_advisor, no_claude=self._no_claude,
                strategy_menu=_strategy_menu, menu_lookup=_menu_lookup)
            self.team = TeamRuntime(self.cfg, self.state_dir, self.registry, self.advisor, self.notifier.send,
                                    clock=self._utc_now, monotonic=self._monotonic, store=self.office)
            if self.cfg.claude.enabled and not self._no_claude:
                log.info("Claude advisor: %s", self.advisor.snapshot().get("state"))
        except Exception:  # noqa: BLE001 - the team is optional; the deterministic core must keep running
            log.exception("The specialist team could not start; trading continues on the deterministic v2 path")
            self.team = None

    def _entry_block_reason(self) -> Optional[str]:
        """Why NEW entries are blocked right now (positions are never closed for these reasons)."""
        if self.guard is not None and self.guard.halted_reason:
            return self.guard.halted_reason
        if self._pause_path is not None and self._pause_path.exists():
            return "new entries paused from the panel (PAUSE_ENTRIES marker in the state folder)"
        if self._guardian_freeze:
            return f"Guardian freeze: {self._guardian_freeze}"
        return None

    @property
    def real_money_confirmed(self) -> bool:
        return bool(self.confirm_real_money or self.cfg.real_money_confirmed)

    def banner(self, acc: Any) -> None:
        kind = "REAL MONEY" if self.is_real else "DEMO"
        lines = [
            "=" * 72,
            f"  ACCOUNT {mask(acc.login)} on {acc.server}: {kind}",
            f"  equity {acc.equity:.2f} {acc.currency}, leverage 1:{getattr(acc, 'leverage', '?')}, "
            f"{'hedging' if self.hedging else 'netting'} account",
            f"  risk per trade {self.risk.risk_pct:.2f}%, daily loss limit {self.risk.max_daily_loss_pct:.2f}%, "
            f"drawdown breaker {self.risk.max_drawdown_pct:.1f}% from peak",
            f"  state folder {self.cfg.state_root}/{account_key(acc.server, acc.login).replace(str(acc.login), mask(acc.login))}",
            f"  mode: {'DRY RUN (no orders will be sent)' if self.cfg.dry_run else 'LIVE ORDERS'}",
            "=" * 72,
        ]
        level = logging.WARNING if self.is_real else logging.INFO
        for line in lines:
            log.log(level, line)
        log.info("Config: symbols=%s timeframe=%s magic=%d strategies=%s", ",".join(self.symbols.values()),
                 self.cfg.timeframe, self.cfg.magic, ",".join(self.cfg.strategy_names()))

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
        # otherwise a withdrawal would trip the drawdown breaker.
        near_limit = (self.guard.drawdown_pct(acc.equity) >= self.risk.max_drawdown_pct
                      or self.guard.daily_loss_pct(acc.equity) >= self.risk.max_daily_loss_pct)
        if near_limit or self._monotonic() >= self._next_cash_flow:
            self._next_cash_flow = self._monotonic() + CASH_FLOW_SECONDS
            self._check_cash_flow()
        events = self.guard.update(server_datetime(server_now).date().isoformat(), acc.equity,
                                   self.risk.max_daily_loss_pct, self.risk.max_drawdown_pct)
        self._handle_guard_events(events, acc, server_now)
        self._maybe_reload_config()
        self._housekeeping(acc, server_now)
        self._maybe_heartbeat(acc)
        if self.guard.halted_reason:
            self._keep_flat_while_halted()

        for wanted, symbol in self.symbols.items():
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

    def _observe_ticks(self) -> bool:
        stale = self.cfg.stale_tick_minutes * 60
        any_fresh = False
        for symbol in self.symbols.values():
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
            log.warning("RESUME marker removed: drawdown circuit breaker cleared; peak restarts at %.2f", acc.equity)
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
        for symbol in self.symbols.values():
            log.info("%s: %s", symbol, self.status_line(symbol))

    def status_line(self, symbol: str) -> str:
        if self.engine is None:
            return "external signal source"
        champion = self.registry.champion(symbol)
        if not champion.is_notrade:
            return f"champion {champion.describe()} trading"
        progress = self.engine.promotion_status(symbol)
        run = self.registry.latest_run(symbol)
        if run is None:
            running = self.scheduler is not None and self.scheduler.running()
            return ("OBSERVATION: collecting data; first optimisation "
                    f"{'running' if running else 'pending'}. No orders until a candidate is promoted.")
        if not progress:
            return (f"NO TRADE: no candidate passed the gates in run {run['id']}; standing aside by design. "
                    "Next cycle at the weekly re-optimisation.")
        best = max(progress, key=lambda p: (p["enough_evidence"], p["shadow_trades"]))
        boot = self.engine.bootstrap_left() / 86400
        return (f"OBSERVATION: best challenger {best['candidate']}: shadow {best['shadow_trades']}/"
                f"{self.cfg.min_shadow_trades} trades, {best['shadow_days']}/{self.cfg.min_shadow_days:g} days, "
                f"mean {best['shadow_expectancy_r']:+.3f}R"
                + (f", bootstrap {boot:.1f} days left" if boot > 0 else "")
                + ". No orders until a candidate is promoted.")

    # ------------------------------------------------------------ housekeeping

    def _housekeeping(self, acc: Any, server_now: float) -> None:
        now = self._monotonic()
        if now < self._next_housekeeping:
            return
        self._next_housekeeping = now + HOUSEKEEPING_SECONDS
        self.registry.set_kv("equity", str(float(acc.equity)))
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
        if self.team is not None:
            try:
                self.team.housekeeping(float(self._utc_now()), self._bot_status(acc, positions, server_now))
            except Exception:  # noqa: BLE001 - the team must never break the trading loop
                log.exception("Team housekeeping failed; trading continues")
        if self.scheduler is not None:
            try:
                self.scheduler.maybe_launch(weekend=is_weekend(server_now) and not any(self._market_state.values()))
            except Exception:  # noqa: BLE001 - the optimiser must never break trading
                log.exception("Could not schedule optimisation")
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
                "weekend": is_weekend(server_now) and not any(self._market_state.values())}

    def _check_cash_flow(self) -> None:
        getter = getattr(self.mt5, "history_deals_get", None)
        if getter is None:
            return
        balance_type = getattr(self.mt5, "DEAL_TYPE_BALANCE", 2)
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
            if getattr(d, "type", None) == balance_type and d.ticket not in seen:
                seen.add(d.ticket)
                if not first_check:
                    amount += float(d.profit)
        if amount:
            log.info("Deposit/withdrawal of %.2f detected; daily start and drawdown peak adjusted", amount)
            self.guard.adjust_for_cash_flow(amount)
        self.registry.set_kv("cash_flow_seen", json.dumps(sorted(seen)[-500:]))
        self.registry.set_kv("cash_flow_checked", str(now))

    def _check_terminal_freeze(self, positions: Optional[list[Any]]) -> None:
        if not self.cfg.terminal_restart_enabled:
            return
        stale = self.clock.all_stale_for(self.symbols.values())
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

    def _write_health(self, acc: Any, positions: Optional[list[Any]]) -> None:
        symbols = {}
        for symbol in self.symbols.values():
            champ = self.registry.champion(symbol)
            symbols[symbol] = {
                "champion": champ.describe(),
                "market": "active" if self._market_state.get(symbol) else "quiet/closed",
                "status_line": self.status_line(symbol),
                "champion_performance": self.engine.champion_report(symbol) if self.engine else {},
                "challengers": self.engine.promotion_status(symbol) if self.engine else [],
            }
        payload = {
            "version": __version__,
            "account": f"{mask(acc.login)} @ {acc.server}",
            "account_type": "REAL" if self.is_real else "DEMO",
            "state_dir": str(self.state_dir),
            "equity": round(float(acc.equity), 2),
            "balance": round(float(acc.balance), 2),
            "peak_equity": self.guard.state.peak_equity,
            "drawdown_pct": round(self.guard.drawdown_pct(acc.equity), 2),
            "open_positions": len(positions) if positions is not None else None,
            "halted": self.guard.halted_reason,
            "dry_run": self.cfg.dry_run,
            "last_loop_utc": datetime.fromtimestamp(self._last_loop_utc or self._utc_now(), tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC"),
            "last_tick_server": fmt_server_time(self.clock.latest_tick_time) if self.clock.latest_tick_time else None,
            "server_utc_offset_hours": None if self.clock.offset is None else self.clock.offset / 3600,
            "symbols": symbols,
            "optimizer": {"running": bool(self.scheduler and self.scheduler.running()),
                          "last_cycle_finished": self.registry.get_kv("last_cycle_finished")},
            "entry_block": self._entry_block_reason(),
            "team": self.team.snapshot() if self.team is not None else {"effective_mode": "baseline",
                                                                         "mode_reason": "team not running"},
            "advisor": {k: v for k, v in (self.advisor.snapshot() if self.advisor is not None else
                                          {"state": "DISABLED"}).items() if k != "history"},
        }
        try:
            write_health(Path(self.cfg.health_file), payload, self._durable_files)
        except OSError as exc:
            log.warning("Could not write %s: %s", self.cfg.health_file, exc)

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
                         list(self.symbols.values()))
            stats = trade_stats([r for r in self.registry.trades(mode="live", status="closed")
                                 if day_start_utc - 86400 <= (r.close_ts or 0) < day_start_utc])
            champions = ", ".join(f"{s}: {self.registry.champion(s).describe()}" for s in self.symbols.values())
            self.notifier.send(f"Daily summary {prev_day}: {stats['trades']} live trades, P&L {stats['net_pnl']:.2f}, "
                               f"expectancy {stats['expectancy_r']:+.2f}R, equity {acc.equity:.2f}. {champions}",
                               key=f"daily-{prev_day}")
            if server_datetime(server_now).weekday() == 0:
                write_report(self.registry, out, f"weekly_{prev_day}", day_start_utc - 7 * 86400, day_start_utc,
                             desc, list(self.symbols.values()))
            if self.team is not None and self.cfg.notifications.daily_html_report:
                from .reports import write_html_report

                path = write_html_report(self.registry, self.office, out, f"daily_{prev_day}", day_start_utc - 86400,
                                         day_start_utc, desc, list(self.symbols.values()))
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
            merged, notes = reload_decision(self.cfg, new)
        except ValueError as exc:
            log.error("config.json reload rejected: %s", exc)
            return False
        for note in notes:
            log.warning("Config reload: %s", note)
        if merged != self.cfg:
            self.cfg = merged
            self.risk.update_config(merged)
            if self.engine is not None:
                self.engine.update_settings(EngineSettings.from_config(merged))
            if self.scheduler is not None:
                self.scheduler.cfg = merged
            if merged.news_filter_enabled:
                self._load_news()
            if self.advisor is not None:
                self.advisor.update_config(merged.claude)
            if self.team is not None:
                self.team.update_config(merged)
            log.info("Config reloaded from %s", self.config_path)
        return True

    # ------------------------------------------------------------ per symbol

    def _costs(self, symbol: str) -> Any:
        return cost_model(self.cfg, self.commission.get(symbol, self.cfg.commission_fallback_per_lot))

    def _process_symbol(self, symbol: str, acc: Any) -> None:
        if not self._market_state.get(symbol, True):
            return
        rates = self.mt5.copy_rates_from_pos(symbol, self._timeframe, 0, self.cfg.bars + 1)
        if rates is None or len(rates) < 2:
            log.warning("%s: no rate data (%s)", symbol, self._last_error())
            return
        closed = rates[:-1]  # the last bar is still forming: never let a strategy see it
        bar_time = int(closed[-1]["time"])
        previous = self._last_bar.get(symbol)
        # Recorded before acting, so a failure is not retried on every poll of the same bar.
        self._last_bar[symbol] = bar_time
        if previous is not None and bar_time == previous:
            return
        spec = self.specs.get(symbol) or self._refresh_spec(symbol)
        self._closes[symbol] = np.asarray(closed["close"][-(self.cfg.team.correlation_lookback_bars + 1):],
                                          dtype=float)
        intent = self._signal_source.on_bar(symbol, closed, spec, self._costs(symbol), float(acc.equity),
                                            self.risk.risk_pct)
        if previous is None:
            log.info("%s: last closed bar %s recorded; waiting for the next one", symbol, fmt_server_time(bar_time))
            return
        self._team_observe(symbol, acc, closed)

        positions = self._bot_positions(symbol)
        if self.cfg.close_on_opposite:
            for pos in positions:
                row = self.registry.trade_by_ticket(int(pos.ticket))
                origin = row.param_set_id if row else None
                if self._signal_source.exit_signal(symbol, origin, self._side_of(pos), closed):
                    if not self._close_position(pos, "opposite signal from its own strategy"):
                        log.warning("%s: could not close position %s on opposite signal", symbol, pos.ticket)
        if intent is None:
            log.debug("%s: no entry intent on bar %s", symbol, fmt_server_time(bar_time))
            return
        if not isinstance(intent, EntryIntent):
            log.error("%s: signal source returned %r, expected EntryIntent or None", symbol, intent)
            return
        sig = intent.signal
        log.info("%s: %s signal from %s on bar %s (ATR %.6f)", symbol, sig.side, intent.description,
                 fmt_server_time(bar_time), sig.atr)
        self._enter(symbol, intent, acc, closed)

    def _team_observe(self, symbol: str, acc: Any, closed: np.ndarray) -> None:
        """New bar: let the team scan the market and manage open positions. Never raises."""
        if self.team is None:
            return
        from . import teambridge

        try:
            self.team.observe_bar(teambridge.decision_context(self, symbol, None, acc, closed, purpose="observe"))
        except Exception:  # noqa: BLE001 - observation is optional
            log.debug("%s: team observation failed", symbol, exc_info=True)
        try:
            teambridge.manage_positions(self, symbol, acc, closed)
        except Exception:  # noqa: BLE001 - management is optional; SL/TP keep protecting the positions
            log.exception("%s: position management failed; positions keep their SL/TP", symbol)

    def _enter(self, symbol: str, intent: EntryIntent, acc: Any, closed: np.ndarray) -> None:
        """Route a champion proposal through the team's decision protocol, or straight to the
        risk layer when there is no team. If the team fails BEFORE submitting, the
        deterministic v2 path takes over; it can never submit twice."""
        if self.team is None:
            self._try_open(symbol, intent, acc)
            return
        from . import teambridge

        gateway = teambridge.BotGateway(self, symbol, intent, acc)
        try:
            ctx = teambridge.decision_context(self, symbol, intent, acc, closed)
            self.team.decide(ctx, gateway)
        except Exception:  # noqa: BLE001 - fail open to the deterministic core, never to a crash
            log.exception("%s: team decision failed; falling back to the deterministic path", symbol)
            if not gateway.submitted:
                self._try_open(symbol, intent, acc)

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
            out.append(OpenRisk(p.symbol, risk))
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

    def _try_open(self, symbol: str, intent: EntryIntent, acc: Any, multiplier: float = 1.0,
                  dossier_id: Optional[str] = None, preview: bool = False) -> EntryOutcome:
        """Plan, gate and (unless ``preview``) send one entry.

        ``multiplier`` (the team's and the advisor's combined verdict) can only SHRINK the
        fixed-fraction risk: it is clamped to [0, 1] here again, so no caller can raise the
        size above the deterministic base. Every hard rule of the risk layer still applies.
        """
        try:
            multiplier = float(multiplier)
        except (TypeError, ValueError):
            multiplier = 0.0
        multiplier = min(max(multiplier, 0.0), limits.MAX_SIZE_MULTIPLIER) if math.isfinite(multiplier) else 0.0

        def note(reason: str, allowed: Optional[bool] = None, level: int = logging.INFO, **extra: Any) -> EntryOutcome:
            if not preview:
                log.log(level, "%s: %s", symbol, reason)
            return EntryOutcome(allowed, reason, **extra)

        sig = intent.signal
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None or info.point <= 0 or tick.bid <= 0 or tick.ask <= 0:
            return note(f"no symbol info or valid tick ({self._last_error()}); skipping", level=logging.WARNING)
        if self._closed_until.get(symbol, 0) > self._monotonic():
            return note("market reported closed recently; skipping")
        if info.trade_mode != self.mt5.SYMBOL_TRADE_MODE_FULL:
            return note(f"trading mode is restricted (trade_mode={info.trade_mode}); skipping")
        hour = server_datetime(tick.time).hour
        if not in_trading_hours(hour, self.cfg.trading_hours_server):
            return note(f"server hour {hour:02d} outside trading window {list(self.cfg.trading_hours_server)}; "
                        "skipping")
        if multiplier <= 0:
            return note("size multiplier is zero: no trade", allowed=False)
        spec = SymbolSpec.from_info(info)
        self.specs[symbol] = spec
        spread_points = (tick.ask - tick.bid) / info.point
        price = tick.ask if sig.side == "BUY" else tick.bid

        def plan_for(risk_pct: float) -> Any:
            return plan_trade(sig.side, sig.atr, sig.sl_mult or 2.0, sig.tp_mult or 3.0, price, spread_points, spec,
                              float(acc.equity), risk_pct, self.commission.get(symbol, 0.0),
                              self.cfg.deviation_points, self._loss_per_lot_fn(symbol))

        base = plan_for(self.risk.risk_pct)
        result = base if multiplier >= 1.0 else plan_for(self.risk.risk_pct * multiplier)
        base_volume = base.plan.volume if base.plan is not None else 0.0
        if result.plan is None:
            reason = result.reason
            if base.plan is not None:
                reason = f"reduced size (x{multiplier:.2f}) is below the minimum volume: {reason}"
            return note(f"no trade: {reason}", allowed=False, base_volume=base_volume)
        plan = result.plan
        positions = self._bot_positions()
        news = None
        if self.cfg.news_filter_enabled:
            news = self.news.blocked({spec.currency_base, spec.currency_profit, "*"}, self._utc_now(),
                                     self.cfg.news_minutes_before, self.cfg.news_minutes_after)
        ctx = EntryContext(
            symbol=symbol, plan=plan, equity=float(acc.equity), spread_points=spread_points,
            bot_positions=self._open_risks(positions, float(acc.equity)),
            symbol_has_foreign_netting_position=self._foreign_netting_position(symbol),
            notional_money=self._notional(symbol, sig.side, plan.volume, price, acc),
            halted_reason=self._entry_block_reason(), news_blocked=news,
            slippage_paused=self._slippage_pause.get(symbol, 0) > self._monotonic(),
        )
        if ctx.notional_money is None:
            return note("cannot compute exposure (order_calc_margin unavailable); skipping (fail closed)",
                        allowed=False, level=logging.WARNING, base_volume=base_volume)
        allowed, reason = self.risk.approve_entry(ctx)
        if not allowed:
            return note(f"entry rejected by the risk layer: {reason}", allowed=False, base_volume=base_volume)
        if preview:
            return EntryOutcome(True, "ok", volume=plan.volume, base_volume=base_volume, risk_pct=plan.risk_pct)
        filling = select_filling_mode(info.filling_mode, self.mt5)

        def build(t: Any) -> dict[str, Any]:
            return self._open_request(symbol, plan, info, t, filling)

        request = build(tick)
        try:
            validate_open_request(request, plan)
        except RiskViolation as exc:
            return note(f"request blocked by the risk layer: {exc}", allowed=False, level=logging.ERROR)
        check = self.mt5.order_check(request)
        if check is None:
            return note(f"order_check returned nothing ({self._last_error()}); skipping", allowed=True,
                        level=logging.WARNING)
        if check.retcode not in (0, self.mt5.TRADE_RETCODE_DONE):
            if check.retcode == self._market_closed_code:
                self._closed_until[symbol] = self._monotonic() + MARKET_CLOSED_BACKOFF_SECONDS
            return note(f"order_check rejected the order: retcode={check.retcode} comment={check.comment}",
                        allowed=True, level=logging.WARNING)
        if self.cfg.dry_run:
            log.info("DRY RUN: would send %s %s %.2f lots at %s SL %s TP %s (order_check OK, risk %.2f, x%.2f)",
                     sig.side, symbol, plan.volume, request["price"], request["sl"], request["tp"], plan.planned_risk,
                     multiplier)
            return EntryOutcome(True, "dry run: order_check OK, not sent", sent=False, ok=True, volume=plan.volume,
                                price=float(request["price"]), sl=float(request["sl"]), tp=float(request["tp"]),
                                dry_run=True, base_volume=base_volume, risk_pct=plan.risk_pct)
        if self.guard.halted_reason:
            self._entries_while_halted += 1  # defence in depth: the Guardian treats this as a kill condition
        sent = self._send_with_retry(build, tick, symbol, plan)
        if sent is None:
            return EntryOutcome(True, "order_send failed (see log)", sent=True, ok=False, base_volume=base_volume)
        res, final_request = sent
        slippage = abs(float(res.price) - float(final_request["price"])) / info.point if res.price else 0.0
        if slippage > self.cfg.max_slippage_points:
            self._slippage_pause[symbol] = self._monotonic() + self.cfg.slippage_pause_minutes * 60
            log.warning("%s: slippage %.1f points > max %.1f; new entries paused for %.0f min", symbol, slippage,
                        self.cfg.max_slippage_points, self.cfg.slippage_pause_minutes)
            self.notifier.send(f"{symbol}: slippage {slippage:.1f} points; entries paused", key=f"slip-{symbol}")
        volume = float(res.volume or plan.volume)
        risk_money = plan.loss_per_lot * volume
        extra = {"deal": getattr(res, "deal", None), "strategy": intent.strategy, "slippage_points": slippage}
        if dossier_id:
            extra.update(dossier_id=dossier_id, size_multiplier=round(multiplier, 6))
        self.registry.open_trade("live", symbol, intent.param_set_id, sig.side, float(res.price or request["price"]),
                                 final_request["sl"], final_request["tp"], volume, risk_money, ticket=int(res.order),
                                 open_bar_time=self._last_bar.get(symbol), extra=extra)
        log.info("%s: opened %s %.2f lots at %s SL %s TP %s (order %s, deal %s, risk %.2f, size x%.2f)", symbol,
                 sig.side, volume, res.price, final_request["sl"], final_request["tp"], res.order, res.deal, risk_money,
                 multiplier)
        return EntryOutcome(True, "opened", sent=True, ok=True, volume=volume,
                            price=float(res.price or request["price"]), sl=float(final_request["sl"]),
                            tp=float(final_request["tp"]), slippage_points=slippage, ticket=int(res.order),
                            base_volume=base_volume, risk_pct=plan.risk_pct)

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
        rates = self.mt5.copy_rates_from_pos(symbol, self._timeframe, 0, 100)
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
            log.info("%s: position %s closed at %s, P&L %.2f (%.2fR)", row.symbol, row.ticket, exit_price, pnl, r)
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

    def refresh_market_data(self) -> bool:
        """Download history for every symbol into the SQLite cache used by the optimiser."""
        count = int(self.cfg.history_months * 30.4375 * 86400 / self.cfg.bar_seconds * 0.75) + 500
        count = min(count, 200_000)
        ok = True
        for symbol in self.symbols.values():
            try:
                rates = self.mt5.copy_rates_from_pos(symbol, self._timeframe, 1, count)
            except Exception:  # noqa: BLE001
                log.exception("%s: history download failed", symbol)
                rates = None
            if rates is None or len(rates) == 0:
                log.warning("%s: no history returned (%s). Raise Tools > Options > Charts > Max bars in chart.",
                            symbol, self._last_error())
                ok = False
                continue
            self.registry.store_bars(symbol, self.cfg.timeframe, rates)
            self._refresh_spec(symbol)
            log.info("%s: cached %d bars of history for optimisation", symbol, len(rates))
        if self.mt5.account_info() is not None:
            self.registry.set_kv("equity", str(float(self.mt5.account_info().equity)))
        return ok
