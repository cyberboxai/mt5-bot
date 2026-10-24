#!/usr/bin/env python3
"""Automated trading bot for the MetaTrader 5 desktop terminal (Windows).

Single-file runtime: copy this file plus ``config.json`` to another machine and run
``python mt5_bot.py``. The ``MetaTrader5`` package talks to a locally running,
logged-in MT5 terminal over IPC, so the terminal must be open.

Design notes:
* Demo first: the bot refuses real accounts unless ``allow_live`` is true.
* Every order carries SL and TP, so protection lives on the broker server even if
  this process or the PC dies.
* The example strategy (EMA crossover) has NO PROVEN EDGE. It only demonstrates the
  infrastructure. Replace ``generate_signal`` with your own logic.
* All MT5 access goes through an injectable ``api`` object so the pure logic and the
  bot behaviour can be tested on any OS with a fake module.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import logging.handlers
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional

import numpy as np

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - only hit on machines without the package
    mt5 = None  # type: ignore[assignment]

__version__ = "1.0.0"

log = logging.getLogger("mt5_bot")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STARTUP = 2
EXIT_SAFETY_STOP = 3

TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4")
ORDER_COMMENT = "mt5_bot"
MAX_SEND_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 0.5
RECONNECT_START_SECONDS = 5.0
RECONNECT_MAX_SECONDS = 120.0
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# Bit flags of symbol_info.filling_mode (MQL5 SYMBOL_FILLING_*). Defined locally because
# older MetaTrader5 package builds do not export them.
FILLING_FLAG_FOK = 1
FILLING_FLAG_IOC = 2

Side = Literal["BUY", "SELL"]


# --------------------------------------------------------------------------- config


def _check_int(name: str, value: Any, minimum: Optional[int] = None) -> None:
    # bool is a subclass of int; "true" must never silently become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")


def _check_number(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")


def _check_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")


def _check_text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")


@dataclass(frozen=True)
class Config:
    """Bot configuration. Load with ``Config.load``; every load is validated."""

    symbols: tuple[str, ...] = ("EURUSD",)
    timeframe: str = "M15"
    bars: int = 300
    magic: int = 510001

    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_open_positions_total: int = 3

    max_spread_points: float = 20
    deviation_points: int = 10
    trading_hours_server: tuple[int, int] = (7, 20)

    fast_ema: int = 20
    slow_ema: int = 50
    atr_period: int = 14
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0
    close_on_opposite: bool = True

    allow_live: bool = False
    dry_run: bool = False
    poll_seconds: float = 5.0
    heartbeat_minutes: float = 30.0
    kill_switch_file: str = "KILL"
    state_file: str = "bot_state.json"
    log_file: str = "bot.log"
    mt5_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        if not isinstance(data, Mapping):
            raise ValueError("Config must be a JSON object")
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(
                f"Unknown config key(s): {', '.join(unknown)}. Valid keys: {', '.join(sorted(known))}"
            )
        values = dict(data)
        # JSON has no tuples; tuples keep the frozen config hashable and immutable.
        for key in ("symbols", "trading_hours_server"):
            if isinstance(values.get(key), list):
                values[key] = tuple(values[key])
        cfg = cls(**values)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Config":
        try:
            # utf-8-sig: Windows Notepad may save a BOM.
            text = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"Cannot read config file {path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        return cls.from_dict(data)

    def required_bars(self) -> int:
        """Minimum history so EMA (SMA-seeded) and Wilder ATR have converged."""
        return 3 * max(self.slow_ema, self.atr_period) + 2

    def validate(self) -> None:
        """Raise ValueError with an actionable message on the first invalid field."""
        if not isinstance(self.symbols, tuple) or not self.symbols:
            raise ValueError("symbols must be a non-empty list, e.g. [\"EURUSD\"]")
        for sym in self.symbols:
            _check_text("symbols entry", sym)
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must not contain duplicates")
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}, got {self.timeframe!r}")
        _check_int("magic", self.magic, minimum=1)

        _check_number("risk_per_trade_pct", self.risk_per_trade_pct)
        if not 0 < self.risk_per_trade_pct <= 2:
            raise ValueError("risk_per_trade_pct must be > 0 and <= 2")
        _check_number("max_daily_loss_pct", self.max_daily_loss_pct)
        if not 0 < self.max_daily_loss_pct <= 10:
            raise ValueError("max_daily_loss_pct must be > 0 and <= 10")
        _check_int("max_open_positions_total", self.max_open_positions_total, minimum=1)

        _check_number("max_spread_points", self.max_spread_points)
        if self.max_spread_points <= 0:
            raise ValueError("max_spread_points must be > 0")
        _check_int("deviation_points", self.deviation_points, minimum=0)
        self._validate_hours()

        _check_int("fast_ema", self.fast_ema, minimum=1)
        _check_int("slow_ema", self.slow_ema, minimum=2)
        if self.fast_ema >= self.slow_ema:
            raise ValueError(f"fast_ema ({self.fast_ema}) must be smaller than slow_ema ({self.slow_ema})")
        _check_int("atr_period", self.atr_period, minimum=1)
        for name in ("sl_atr_mult", "tp_atr_mult"):
            value = getattr(self, name)
            _check_number(name, value)
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        _check_bool("close_on_opposite", self.close_on_opposite)

        _check_int("bars", self.bars, minimum=1)
        if self.bars < self.required_bars():
            raise ValueError(
                f"bars ({self.bars}) is too small for the indicators; need at least {self.required_bars()}"
            )

        _check_bool("allow_live", self.allow_live)
        _check_bool("dry_run", self.dry_run)
        _check_number("poll_seconds", self.poll_seconds)
        if self.poll_seconds < 1:
            raise ValueError("poll_seconds must be >= 1")
        _check_number("heartbeat_minutes", self.heartbeat_minutes)
        if self.heartbeat_minutes <= 0:
            raise ValueError("heartbeat_minutes must be > 0")
        for name in ("kill_switch_file", "state_file", "log_file"):
            _check_text(name, getattr(self, name))
        if self.mt5_path is not None:
            _check_text("mt5_path", self.mt5_path)

    def _validate_hours(self) -> None:
        window = self.trading_hours_server
        if not isinstance(window, tuple) or len(window) != 2:
            raise ValueError("trading_hours_server must be [start, end], e.g. [7, 20]")
        start, end = window
        _check_int("trading_hours_server start", start)
        _check_int("trading_hours_server end", end)
        if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
            raise ValueError(
                "trading_hours_server must be [start, end) with 0 <= start <= 23, "
                f"0 <= end <= 24 and start != end; got {list(window)}"
            )


def in_trading_hours(hour: int, window: tuple[int, int]) -> bool:
    """True if ``hour`` is in the half-open window [start, end). Supports overnight windows."""
    start, end = window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def server_datetime(timestamp: float) -> datetime:
    """Convert an MT5 timestamp to the broker server's wall-clock time.

    MT5 reports tick and bar times as seconds in the trade server's own timezone
    (not true UTC), so interpreting them as UTC yields the server's local clock.
    """
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def fmt_server_time(timestamp: float) -> str:
    return server_datetime(timestamp).strftime("%Y-%m-%d %H:%M server")


# ----------------------------------------------------------------------- indicators


def ema(values: Any, period: int) -> np.ndarray:
    """Exponential moving average seeded with the SMA of the first ``period`` values.

    Entries before index ``period - 1`` are NaN.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    x = np.asarray(values, dtype=float)
    out = np.full(x.shape, np.nan)
    if x.size < period:
        return out
    alpha = 2.0 / (period + 1)
    out[period - 1] = x[:period].mean()
    for i in range(period, x.size):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def atr(high: Any, low: Any, close: Any, period: int) -> np.ndarray:
    """Average True Range with Wilder smoothing.

    True range needs the previous close, so the first defined ATR is at index
    ``period`` (mean of the first ``period`` true ranges). Earlier entries are NaN.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    h = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    if not h.shape == lo.shape == c.shape:
        raise ValueError("high, low and close must have the same length")
    out = np.full(h.shape, np.nan)
    if h.size < period + 1:
        return out
    prev_close = c[:-1]
    tr = np.maximum.reduce(
        [h[1:] - lo[1:], np.abs(h[1:] - prev_close), np.abs(lo[1:] - prev_close)]
    )
    out[period] = tr[:period].mean()
    for i in range(period + 1, h.size):
        out[i] = (out[i - 1] * (period - 1) + tr[i - 1]) / period
    return out


# ------------------------------------------------------------------------- strategy


@dataclass(frozen=True)
class Signal:
    """Entry signal. ``atr`` is in price units and drives SL/TP distances."""

    side: Side
    atr: float

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Signal side must be BUY or SELL, got {self.side!r}")
        if not (isinstance(self.atr, (int, float)) and math.isfinite(self.atr) and self.atr > 0):
            raise ValueError(f"Signal atr must be a positive finite number, got {self.atr!r}")


def generate_signal(closed_bars: np.ndarray, cfg: Config) -> Optional[Signal]:
    """EXAMPLE strategy: EMA fast/slow crossover on the last two closed bars.

    WARNING: this strategy has NO PROVEN EDGE. It exists only to demonstrate the
    infrastructure. Replace this function with your own logic.

    Contract: ``closed_bars`` is the MT5 rates structured array with only CLOSED
    candles (oldest first; fields time, open, high, low, close, tick_volume, spread,
    real_volume). Return ``Signal`` or ``None``. Must not use any future data.
    """
    if closed_bars is None or len(closed_bars) < max(cfg.slow_ema, cfg.atr_period) + 2:
        return None
    close = np.asarray(closed_bars["close"], dtype=float)
    fast = ema(close, cfg.fast_ema)
    slow = ema(close, cfg.slow_ema)
    vol = atr(closed_bars["high"], closed_bars["low"], close, cfg.atr_period)

    d_prev = fast[-2] - slow[-2]
    d_now = fast[-1] - slow[-1]
    last_atr = float(vol[-1])
    if not (math.isfinite(d_prev) and math.isfinite(d_now) and math.isfinite(last_atr)) or last_atr <= 0:
        return None
    # Tolerance absorbs float noise so a flat market never produces a "cross".
    tol = 1e-9 * max(abs(float(slow[-1])), 1.0)
    if d_prev <= tol and d_now > tol:
        return Signal("BUY", last_atr)
    if d_prev >= -tol and d_now < -tol:
        return Signal("SELL", last_atr)
    return None


Strategy = Callable[[np.ndarray, Config], Optional[Signal]]


# -------------------------------------------------------------------------- sizing


def lots_for_risk(
    risk_money: float,
    loss_per_lot: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> float:
    """Volume whose loss at SL is <= ``risk_money``. Returns 0.0 if no valid volume.

    Always rounds DOWN to ``volume_step`` and caps at ``volume_max``. If the result is
    below ``volume_min`` it returns 0.0 (never rounds up into higher risk). Decimal
    arithmetic avoids float artifacts such as 0.3 / 0.1 == 2.9999999999999996.
    """
    try:
        risk = Decimal(str(risk_money))
        loss = Decimal(str(loss_per_lot))
        vmin = Decimal(str(volume_min))
        vmax = Decimal(str(volume_max))
        step = Decimal(str(volume_step))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    numbers = (risk, loss, vmin, vmax, step)
    if not all(n.is_finite() and n > 0 for n in numbers) or vmax < vmin:
        return 0.0
    lots = (risk / loss / step).to_integral_value(rounding=ROUND_FLOOR) * step
    cap = (vmax / step).to_integral_value(rounding=ROUND_FLOOR) * step
    lots = min(lots, cap)
    if lots < vmin:
        return 0.0
    return float(lots)


def select_filling_mode(filling_flags: int, api: Any) -> int:
    """Pick a filling type the symbol supports: IOC, then FOK, else RETURN."""
    if filling_flags & FILLING_FLAG_IOC:
        return api.ORDER_FILLING_IOC
    if filling_flags & FILLING_FLAG_FOK:
        return api.ORDER_FILLING_FOK
    return api.ORDER_FILLING_RETURN


# --------------------------------------------------------------------- daily state


class DailyState:
    """Start-of-day equity and halt flag, persisted as JSON so restarts keep the guard.

    The day key is the broker SERVER day (from tick time): that is the day the broker
    uses for swaps and daily statements, and it is the same no matter where the PC is.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self.day: Optional[str] = None
        self.start_equity: Optional[float] = None
        self.halted = False
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            day, start, halted = data["day"], data["start_equity"], data["halted"]
            if not isinstance(day, str) or not isinstance(halted, bool):
                raise TypeError("unexpected field types")
            _check_number("start_equity", start)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Refusing to start is safer than silently resetting today's loss limit.
            raise ValueError(
                f"State file {self._path} is unreadable or corrupt ({exc}). Inspect it; delete it "
                "only if you accept that the daily loss limit restarts from current equity."
            ) from exc
        self.day, self.start_equity, self.halted = day, float(start), halted

    def _save(self) -> None:
        payload = {"day": self.day, "start_equity": self.start_equity, "halted": self.halted}
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)  # atomic: a crash never leaves a half-written file

    def roll(self, day: str, equity: float) -> bool:
        """Start a new day if ``day`` differs from the stored one. Returns True if it did."""
        if self.day == day:
            return False
        self.day, self.start_equity, self.halted = day, float(equity), False
        self._save()
        return True

    def loss_pct(self, equity: float) -> float:
        if not self.start_equity or self.start_equity <= 0:
            return 0.0
        return (self.start_equity - equity) / self.start_equity * 100.0

    def limit_reached(self, equity: float, max_loss_pct: float) -> bool:
        return self.start_equity is not None and self.loss_pct(equity) >= max_loss_pct

    def halt(self) -> None:
        self.halted = True
        self._save()


# ----------------------------------------------------------------------------- bot


class StartupError(RuntimeError):
    """The bot cannot start safely; the message tells the user what to fix."""


def _mask(login: Any) -> str:
    text = str(login)
    return "***" + text[-3:] if len(text) > 3 else "***"


class Bot:
    """Trading loop. All terminal access goes through ``api`` (the MetaTrader5 module)."""

    def __init__(
        self,
        cfg: Config,
        api: Any = None,
        strategy: Strategy = generate_signal,
        wait: Optional[Callable[[float], None]] = None,
        env: Optional[Mapping[str, str]] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.mt5 = api if api is not None else mt5
        if self.mt5 is None:
            raise StartupError("MetaTrader5 package is not installed. Run: pip install -r requirements.txt")
        self.strategy = strategy
        self.state = DailyState(Path(cfg.state_file))
        self._stop = threading.Event()
        self._stop_signal: Optional[int] = None
        self._wait = wait if wait is not None else self._interruptible_sleep
        self._env = os.environ if env is None else env
        self._monotonic = monotonic
        self._timeframe = getattr(self.mt5, f"TIMEFRAME_{cfg.timeframe}")
        self._kill_path = Path(cfg.kill_switch_file)
        self._last_bar: dict[str, int] = {}
        self._login: Optional[int] = None
        self._next_heartbeat = 0.0
        self._reconnect_delay: Optional[float] = None
        self._next_reconnect = 0.0
        self._success_codes = {
            self.mt5.TRADE_RETCODE_DONE,
            self.mt5.TRADE_RETCODE_PLACED,
            self.mt5.TRADE_RETCODE_DONE_PARTIAL,
        }
        self._retry_codes = {
            self.mt5.TRADE_RETCODE_REQUOTE,
            self.mt5.TRADE_RETCODE_PRICE_CHANGED,
            self.mt5.TRADE_RETCODE_PRICE_OFF,
        }

    # ---- lifecycle

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

    def _init_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.cfg.mt5_path:
            kwargs["path"] = self.cfg.mt5_path
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
        kwargs = self._init_kwargs()
        log.info(
            "Connecting to MT5 terminal (path=%s, credentials from environment=%s)",
            kwargs.get("path", "auto-detect"),
            "yes" if "login" in kwargs else "no",
        )
        return bool(self.mt5.initialize(**kwargs))

    def startup(self) -> None:
        """Connect and run every pre-trade safety check. Raises StartupError."""
        if not self._initialize():
            raise StartupError(
                f"mt5.initialize() failed: {self._last_error()}. Is the MT5 terminal open and "
                "logged in? If several terminals are installed, set mt5_path to terminal64.exe."
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
        problem = self._account_problem(acc)
        if problem:
            raise StartupError(problem)
        self._login = acc.login
        for symbol in self.cfg.symbols:
            if not self.mt5.symbol_select(symbol, True) or self.mt5.symbol_info(symbol) is None:
                raise StartupError(
                    f"Symbol {symbol!r} not found. Open Market Watch (Ctrl+M), right-click > Show All, "
                    "and copy the exact name including any suffix (e.g. EURUSD.m) into config.json."
                )
        log.info(
            "Connected: account %s on %s (%s, %s), equity %.2f %s, mode=%s",
            _mask(acc.login),
            acc.server,
            "DEMO" if self._is_demo(acc) else "REAL",
            acc.currency,
            acc.equity,
            acc.currency,
            "DRY RUN (no orders will be sent)" if self.cfg.dry_run else "LIVE ORDERS",
        )
        log.info(
            "Config: symbols=%s timeframe=%s risk=%.2f%% daily_loss=%.2f%% magic=%d",
            ",".join(self.cfg.symbols),
            self.cfg.timeframe,
            self.cfg.risk_per_trade_pct,
            self.cfg.max_daily_loss_pct,
            self.cfg.magic,
        )

    def _verify_terminal(self) -> None:
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

    def _account_problem(self, acc: Any) -> Optional[str]:
        """Reason the account must not be traded, or None if it is acceptable."""
        if not self._is_demo(acc) and not self.cfg.allow_live:
            return (
                "Refusing to trade: this is a REAL account and allow_live is false in config.json. "
                "Validate on demo first; see the README section on going live."
            )
        if self._login is not None and acc.login != self._login:
            return "The terminal switched to a different account while the bot was running; stopping."
        return None

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001 - best effort during exit
            log.debug("mt5.shutdown() raised", exc_info=True)

    # ---- main loop

    def run(self) -> int:
        log.info("Bot running (poll every %.0fs). Create the file %s to stop.", self.cfg.poll_seconds, self._kill_path)
        while not self._stop.is_set():
            code = self.run_once()
            if code is not None:
                return code
            self._wait(self.cfg.poll_seconds)
        log.info(
            "Stop requested (signal %s); exiting. Open positions keep their server-side SL/TP.",
            self._stop_signal,
        )
        return EXIT_OK

    def run_once(self) -> Optional[int]:
        """One loop iteration. Returns an exit code when the bot must exit, else None."""
        if self._kill_path.exists():
            log.warning("Kill switch file %s found: closing all bot positions and exiting.", self._kill_path)
            closed = self.close_all_positions("kill switch")
            if not closed:
                log.error("Some positions could not be closed; they remain protected by server-side SL/TP.")
            return EXIT_OK if closed else EXIT_ERROR
        if not self._ensure_connected():
            return None
        acc = self.mt5.account_info()
        if acc is None:
            log.warning("account_info() returned nothing: %s", self._last_error())
            return None
        problem = self._account_problem(acc)
        if problem:
            log.critical(problem)
            return EXIT_SAFETY_STOP

        now = self._server_now()
        if now is not None:
            self._update_daily_guard(now, acc.equity)
        self._maybe_heartbeat(acc)
        if self.state.halted:
            self._keep_flat_while_halted()
            return None

        for symbol in self.cfg.symbols:
            if self._stop.is_set():
                break
            try:
                self._process_symbol(symbol, acc)
            except Exception:  # noqa: BLE001 - one symbol must never stop the others
                log.exception("%s: error while processing; continuing with other symbols", symbol)
        return None

    def _ensure_connected(self) -> bool:
        term = self.mt5.terminal_info()
        if term is not None and term.connected:
            if self._reconnect_delay is not None:
                log.info("Terminal connection restored.")
                self._reconnect_delay = None
            return True
        now = self._monotonic()
        if self._reconnect_delay is None:
            self._reconnect_delay = RECONNECT_START_SECONDS
            self._next_reconnect = now + self._reconnect_delay
            log.warning(
                "Terminal disconnected (%s); reconnecting in %.0fs", self._last_error(), self._reconnect_delay
            )
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
        return False

    def _server_now(self) -> Optional[datetime]:
        latest = 0
        for symbol in self.cfg.symbols:
            tick = self.mt5.symbol_info_tick(symbol)
            if tick is not None and tick.time > latest:
                latest = tick.time
        return server_datetime(latest) if latest else None

    def _update_daily_guard(self, now: datetime, equity: float) -> None:
        day = now.date().isoformat()
        if self.state.roll(day, equity):
            log.info("New server day %s: start equity %.2f", day, equity)
        if not self.state.halted and self.state.limit_reached(equity, self.cfg.max_daily_loss_pct):
            log.critical(
                "Daily loss limit hit: equity %.2f vs start %.2f (-%.2f%% >= %.2f%%). "
                "Closing bot positions and halting until the next server day.",
                equity,
                self.state.start_equity,
                self.state.loss_pct(equity),
                self.cfg.max_daily_loss_pct,
            )
            self.state.halt()
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
            self.close_all_positions("daily loss limit (retry)")

    def _maybe_heartbeat(self, acc: Any) -> None:
        now = self._monotonic()
        if now < self._next_heartbeat:
            return
        self._next_heartbeat = now + self.cfg.heartbeat_minutes * 60
        try:
            open_count: Any = len(self._bot_positions())
        except RuntimeError:
            open_count = "unknown"
        log.info(
            "Heartbeat: equity %.2f %s, balance %.2f, bot positions %s, halted=%s, day start equity %s",
            acc.equity,
            acc.currency,
            acc.balance,
            open_count,
            self.state.halted,
            f"{self.state.start_equity:.2f}" if self.state.start_equity is not None else "n/a",
        )

    # ---- per-symbol logic

    def _process_symbol(self, symbol: str, acc: Any) -> None:
        rates = self.mt5.copy_rates_from_pos(symbol, self._timeframe, 0, self.cfg.bars + 1)
        if rates is None or len(rates) < 2:
            log.warning("%s: no rate data (%s)", symbol, self._last_error())
            return
        closed = rates[:-1]  # the last bar is still forming: never let the strategy see it
        bar_time = int(closed[-1]["time"])
        previous = self._last_bar.get(symbol)
        # Recorded before acting, so a failure is not retried on every poll of the same bar.
        self._last_bar[symbol] = bar_time
        if previous is None:
            log.info("%s: last closed bar %s recorded; waiting for the next one", symbol, fmt_server_time(bar_time))
            return
        if bar_time == previous:
            return

        sig = self.strategy(closed, self.cfg)
        if sig is None:
            log.debug("%s: no signal on bar %s", symbol, fmt_server_time(bar_time))
            return
        if not isinstance(sig, Signal):
            log.error("%s: strategy returned %r, expected Signal or None", symbol, sig)
            return
        log.info("%s: %s signal on bar %s (ATR %.6f)", symbol, sig.side, fmt_server_time(bar_time), sig.atr)

        positions = self._bot_positions(symbol)
        if self.cfg.close_on_opposite:
            for pos in positions:
                if self._side_of(pos) != sig.side and not self._close_position(pos, "opposite signal"):
                    log.warning("%s: could not close opposite position; not opening a new one", symbol)
                    return
            positions = self._bot_positions(symbol)
        if positions:
            log.info("%s: bot already has a position on this symbol; skipping entry", symbol)
            return
        self._try_open(symbol, sig, acc)

    def _side_of(self, position: Any) -> Side:
        return "BUY" if position.type == self.mt5.POSITION_TYPE_BUY else "SELL"

    def _bot_positions(self, symbol: Optional[str] = None) -> list[Any]:
        positions = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if positions is None:
            raise RuntimeError(f"positions_get failed: {self._last_error()}")
        return [p for p in positions if p.magic == self.cfg.magic]

    def _entry_allowed(self, symbol: str, info: Any, tick: Any) -> bool:
        if info.trade_mode != self.mt5.SYMBOL_TRADE_MODE_FULL:
            log.info("%s: trading mode is restricted (trade_mode=%s); skipping", symbol, info.trade_mode)
            return False
        hour = server_datetime(tick.time).hour
        if not in_trading_hours(hour, self.cfg.trading_hours_server):
            log.info("%s: server hour %02d outside trading window %s; skipping", symbol, hour,
                     list(self.cfg.trading_hours_server))
            return False
        spread_points = (tick.ask - tick.bid) / info.point
        if spread_points > self.cfg.max_spread_points:
            log.info("%s: spread %.1f points > max %.1f; skipping", symbol, spread_points, self.cfg.max_spread_points)
            return False
        total = len(self._bot_positions())
        if total >= self.cfg.max_open_positions_total:
            log.info("%s: %d bot positions open (max %d); skipping", symbol, total, self.cfg.max_open_positions_total)
            return False
        return True

    def _try_open(self, symbol: str, sig: Signal, acc: Any) -> bool:
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if info is None or tick is None or info.point <= 0 or tick.bid <= 0 or tick.ask <= 0:
            log.warning("%s: no symbol info or valid tick (%s); skipping", symbol, self._last_error())
            return False
        if not self._entry_allowed(symbol, info, tick):
            return False

        # Whole points keep SL/TP on the price grid, so rounding never widens the stop.
        sl_points = math.floor(sig.atr * self.cfg.sl_atr_mult / info.point)
        tp_points = math.floor(sig.atr * self.cfg.tp_atr_mult / info.point)
        spread_points = (tick.ask - tick.bid) / info.point
        min_points = info.trade_stops_level + spread_points
        if sl_points < min_points or tp_points < min_points:
            log.info("%s: SL %d / TP %d points below broker minimum %.1f; skipping",
                     symbol, sl_points, tp_points, min_points)
            return False

        volume = self._volume_for(symbol, sig.side, info, tick, sl_points, acc.equity)
        if volume <= 0:
            return False
        filling = select_filling_mode(info.filling_mode, self.mt5)

        def build(t: Any) -> dict[str, Any]:
            return self._open_request(symbol, sig.side, volume, sl_points, tp_points, info, t, filling)

        request = build(tick)
        check = self.mt5.order_check(request)
        if check is None:
            log.warning("%s: order_check returned nothing (%s); skipping", symbol, self._last_error())
            return False
        if check.retcode not in (0, self.mt5.TRADE_RETCODE_DONE):
            log.warning("%s: order_check rejected the order: retcode=%s comment=%s",
                        symbol, check.retcode, check.comment)
            return False
        if self.cfg.dry_run:
            log.info("DRY RUN: would send %s %s %.2f lots at %s SL %s TP %s (order_check OK)",
                     sig.side, symbol, volume, request["price"], request["sl"], request["tp"])
            return True
        result = self._send_with_retry(build, tick, symbol)
        if result is None:
            return False
        log.info("%s: opened %s %.2f lots at %s SL %s TP %s (order %s, deal %s)",
                 symbol, sig.side, result.volume, result.price, request["sl"], request["tp"],
                 result.order, result.deal)
        return True

    def _volume_for(self, symbol: str, side: Side, info: Any, tick: Any, sl_points: int, equity: float) -> float:
        risk_money = equity * self.cfg.risk_per_trade_pct / 100.0
        # Size from the worst fill the deviation allows, so slippage within it cannot exceed risk.
        slip = self.cfg.deviation_points * info.point
        if side == "BUY":
            order_type, entry = self.mt5.ORDER_TYPE_BUY, tick.ask + slip
            stop = tick.ask - sl_points * info.point
        else:
            order_type, entry = self.mt5.ORDER_TYPE_SELL, tick.bid - slip
            stop = tick.bid + sl_points * info.point
        profit = self.mt5.order_calc_profit(order_type, symbol, 1.0, entry, round(stop, info.digits))
        if profit is None or not math.isfinite(profit) or profit >= 0:
            log.warning("%s: cannot compute loss per lot (order_calc_profit=%s, %s); skipping",
                        symbol, profit, self._last_error())
            return 0.0
        loss_per_lot = -profit
        volume = lots_for_risk(risk_money, loss_per_lot, info.volume_min, info.volume_max, info.volume_step)
        if volume <= 0:
            log.info("%s: minimum volume %.2f would risk %.2f > allowed %.2f; skipping",
                     symbol, info.volume_min, info.volume_min * loss_per_lot, risk_money)
        return volume

    def _open_request(
        self, symbol: str, side: Side, volume: float, sl_points: int, tp_points: int,
        info: Any, tick: Any, filling: int,
    ) -> dict[str, Any]:
        point, digits = info.point, info.digits
        if side == "BUY":
            order_type, price = self.mt5.ORDER_TYPE_BUY, tick.ask
            sl, tp = price - sl_points * point, price + tp_points * point
        else:
            order_type, price = self.mt5.ORDER_TYPE_SELL, tick.bid
            sl, tp = price + sl_points * point, price - tp_points * point
        return {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
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

    def _send_with_retry(self, build: Callable[[Any], dict[str, Any]], tick: Any, symbol: str) -> Any:
        """order_send, retrying only requote / price changed / price off with a fresh tick."""
        request = build(tick)
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            result = self.mt5.order_send(request)
            if result is None:
                log.error("%s: order_send returned nothing: %s", symbol, self._last_error())
                return None
            if result.retcode in self._success_codes:
                return result
            if result.retcode in self._retry_codes and attempt < MAX_SEND_ATTEMPTS:
                log.warning("%s: retcode %s (%s) on attempt %d; retrying with a fresh price",
                            symbol, result.retcode, result.comment, attempt)
                self._wait(RETRY_PAUSE_SECONDS)
                fresh = self.mt5.symbol_info_tick(symbol)
                if fresh is None:
                    log.error("%s: no fresh tick for retry (%s)", symbol, self._last_error())
                    return None
                request = build(fresh)
                continue
            log.error("%s: order_send failed: retcode=%s comment=%s", symbol, result.retcode, result.comment)
            return None
        return None

    # ---- closing

    def _close_position(self, position: Any, reason: str) -> bool:
        if position.magic != self.cfg.magic:  # defence in depth: never touch foreign positions
            return False
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
            return {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": position.volume,
                "type": self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY,
                "position": position.ticket,
                "price": t.bid if is_buy else t.ask,
                "deviation": self.cfg.deviation_points,
                "magic": self.cfg.magic,
                "comment": ORDER_COMMENT,
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }

        if self.cfg.dry_run:
            log.info("DRY RUN: would close position %s (%s %.2f %s) because: %s",
                     position.ticket, "BUY" if is_buy else "SELL", position.volume, symbol, reason)
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


# ----------------------------------------------------------------------------- CLI


def setup_logging(log_file: str) -> None:
    """Rotating UTF-8 file log plus a console handler that never crashes on encoding."""
    path = Path(log_file)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            # Legacy Windows consoles (cp850/cp1252) would raise on unencodable characters.
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    console = logging.StreamHandler(stream)
    console.setFormatter(formatter)
    log.handlers.clear()
    log.addHandler(file_handler)
    log.addHandler(console)
    log.setLevel(logging.INFO)
    log.propagate = False


def install_signal_handlers(bot: Bot) -> None:
    def handler(signum: int, _frame: Any) -> None:
        bot.request_stop(signum)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK = Ctrl+Break / console close on Windows
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError, RuntimeError):
            log.debug("Could not install handler for %s", name)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MetaTrader 5 trading bot (demo-first).")
    parser.add_argument("--config", default="config.json", help="path to config.json (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="run all checks but never send orders")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = Config.load(args.config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_STARTUP
    if args.dry_run:
        cfg = dataclasses.replace(cfg, dry_run=True)
    setup_logging(cfg.log_file)
    log.info("mt5_bot %s starting%s", __version__, " in DRY RUN mode" if cfg.dry_run else "")
    try:
        bot = Bot(cfg)
    except (StartupError, ValueError) as exc:
        log.error("Startup failed: %s", exc)
        return EXIT_STARTUP
    install_signal_handlers(bot)
    try:
        bot.startup()
    except StartupError as exc:
        log.error("Startup failed: %s", exc)
        bot.shutdown()
        return EXIT_STARTUP
    try:
        return bot.run()
    except Exception:  # noqa: BLE001 - last-resort log before exit
        log.exception("Unexpected fatal error; open positions keep their server-side SL/TP.")
        return EXIT_ERROR
    finally:
        bot.shutdown()
        log.info("MT5 connection closed.")


if __name__ == "__main__":
    sys.exit(main())
