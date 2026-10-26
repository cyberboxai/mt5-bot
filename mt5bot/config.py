"""Configuration: loading (with migration of older keys), validation against the hard
ceilings, and safe hot reload.

There is one config schema. Keys from earlier versions are accepted: ``config_migration``
maps them to their v3.1 equivalents (or preserves them under ``_legacy_v30`` when the
mechanism they controlled no longer exists), logs a deprecation notice for each, and the
bot writes the migrated file next to the original. User values are never silently dropped.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Mapping, Optional

from . import limits
from .config_blocks import (BLOCKS, CapitalConfig, ClaudeConfig, FlowDoctorConfig, ManagementConfig, NewsConfig,
                            NotificationsConfig, PanelConfig, ResearchConfig, TeamConfig, TiersConfig, UniverseConfig,
                            parse_block)
from .profiles import PROFILE_NAMES

log = logging.getLogger("mt5_bot")

CONFIG_VERSION = "3.1"
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4")
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}
ACCOUNT_MODES = ("any", "demo_only", "real_only")

# v1 keys that no longer exist, with what replaces them. A clear message beats "unknown key".
REMOVED_KEYS = {
    "allow_live": "use account_mode and real_money_confirmed (or --confirm-real-money)",
    "risk_per_trade_pct": "use risk_per_trade_pct_demo and risk_per_trade_pct_real",
    "max_daily_loss_pct": "use max_daily_loss_pct_demo and max_daily_loss_pct_real",
    "fast_ema": "strategy parameters are now chosen by research inside bounds defined in code",
    "slow_ema": "strategy parameters are now chosen by research inside bounds defined in code",
    "atr_period": "strategy parameters are now chosen by research inside bounds defined in code",
    "sl_atr_mult": "strategy parameters are now chosen by research inside bounds defined in code",
    "tp_atr_mult": "strategy parameters are now chosen by research inside bounds defined in code",
    "state_file": "state now lives in state_root/<server>_<login>/",
}

# Keys whose change needs a restart: identity, account policy and storage locations.
RESTART_KEYS = frozenset({
    "symbols", "timeframe", "magic", "account_mode", "real_money_confirmed", "state_root",
    "reports_dir", "health_file", "log_file", "mt5_path", "kill_switch_file", "panel", "universe",
})

# Safety keys may be hot reloaded only in the stricter direction; loosening needs a restart.
# None means "no user cap" (the profile governs), which is the loosest value.
TIGHTEN_ONLY_KEYS = {
    "risk_per_trade_pct_demo": "lower",
    "risk_per_trade_pct_real": "lower",
    "max_daily_loss_pct_demo": "lower",
    "max_daily_loss_pct_real": "lower",
    "max_open_positions_total": "lower",
    "max_total_open_risk_pct": "lower",
    "max_symbol_exposure_x": "lower",
    "max_drawdown_from_peak_pct": "lower",
    "max_slippage_points": "lower",
}
PROFILE_ORDER = {name: i for i, name in enumerate(PROFILE_NAMES)}


def _check_int(name: str, value: Any, minimum: Optional[int] = None, maximum: Optional[int] = None) -> None:
    # bool is a subclass of int; "true" must never silently become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value}")


def _check_number(name: str, value: Any, low: Optional[float] = None, high: Optional[float] = None,
                  low_inclusive: bool = True) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if low is not None and (value < low or (not low_inclusive and value == low)):
        op = ">=" if low_inclusive else ">"
        raise ValueError(f"{name} must be {op} {low}, got {value}")
    if high is not None and value > high:
        raise ValueError(f"{name} must be <= {high}, got {value}")


def _check_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")


def _check_text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")


def _check_ceiling(name: str, value: Optional[float], ceiling: float) -> None:
    if value is None:
        return
    _check_number(name, value, low=0, low_inclusive=False)
    if value > ceiling:
        raise ValueError(f"{name} = {value} exceeds the hard ceiling {ceiling} defined in code (mt5bot/limits.py)")


@dataclass(frozen=True)
class Config:
    """Bot configuration. Load with ``Config.load``; every load is validated."""

    # ---- market: core symbols (always traded when the broker offers them) and primary timeframe
    symbols: tuple[str, ...] = ("EURUSD",)
    timeframe: str = "M15"
    bars: int = 1000
    history_months: int = 12
    magic: int = 510001

    # ---- account policy
    account_mode: str = "any"
    real_money_confirmed: bool = False

    # ---- risk: the profile sets every number; the optional caps below can only tighten it
    risk_profile: str = "balanced"
    risk_per_trade_pct_demo: Optional[float] = None   # cap on the per-trade risk of the highest tier
    risk_per_trade_pct_real: Optional[float] = None
    max_daily_loss_pct_demo: Optional[float] = None
    max_daily_loss_pct_real: Optional[float] = None
    max_open_positions_total: Optional[int] = None
    max_total_open_risk_pct: Optional[float] = None
    max_symbol_exposure_x: float = 5.0
    max_drawdown_from_peak_pct: Optional[float] = None

    # ---- execution filters and costs
    deviation_points: int = 10
    max_slippage_points: float = 20
    slippage_pause_minutes: float = 60
    trading_hours_server: tuple[int, int] = (2, 22)
    close_on_opposite: bool = True
    commission_per_lot: Optional[float] = None
    commission_fallback_per_lot: float = 7.0
    backtest_slippage_points: float = 2.0
    news_filter_enabled: bool = False
    news_file: str = "news.csv"
    news_minutes_before: int = 30
    news_minutes_after: int = 30

    # ---- research (Quant Research department: walk-forward priors for the arms)
    enabled_strategies: Optional[tuple[str, ...]] = None
    trials_per_strategy: int = 30
    max_trials_per_cycle: int = 300
    optimizer_workers: int = 1
    optimizer_seed: int = 12345
    wf_train_months: int = 3
    wf_test_months: int = 1
    wf_step_months: int = 1
    reoptimize_every_days: float = 7

    # ---- operations
    dry_run: bool = False
    poll_seconds: float = 5.0
    closed_market_poll_seconds: float = 60.0
    heartbeat_minutes: float = 30.0
    stale_tick_minutes: float = 5.0
    terminal_restart_enabled: bool = True
    terminal_freeze_minutes: float = 10.0
    holidays: tuple[str, ...] = ()
    config_reload_seconds: float = 60.0
    telegram_enabled: bool = False
    kill_switch_file: str = "KILL"
    state_root: str = "state"
    reports_dir: str = "reports"
    health_file: str = "health.json"
    log_file: str = "bot.log"
    mt5_path: Optional[str] = None
    mt5_init_timeout_ms: int = 60000
    config_version: str = CONFIG_VERSION

    # ---- blocks (see config_blocks.py)
    tiers: TiersConfig = dc_field(default_factory=TiersConfig)
    capital: CapitalConfig = dc_field(default_factory=CapitalConfig)
    universe: UniverseConfig = dc_field(default_factory=UniverseConfig)
    flow_doctor: FlowDoctorConfig = dc_field(default_factory=FlowDoctorConfig)
    management: ManagementConfig = dc_field(default_factory=ManagementConfig)
    research: ResearchConfig = dc_field(default_factory=ResearchConfig)
    claude: ClaudeConfig = dc_field(default_factory=ClaudeConfig)
    team: TeamConfig = dc_field(default_factory=TeamConfig)
    news: NewsConfig = dc_field(default_factory=NewsConfig)
    panel: PanelConfig = dc_field(default_factory=PanelConfig)
    notifications: NotificationsConfig = dc_field(default_factory=NotificationsConfig)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        """Strict parse of a (migrated) mapping. Keys starting with "_" are notes and ignored."""
        if not isinstance(data, Mapping):
            raise ValueError("Config must be a JSON object")
        data = {k: v for k, v in data.items() if not str(k).startswith("_")}
        removed = sorted(set(data) & set(REMOVED_KEYS))
        if removed:
            details = "; ".join(f"{k}: {REMOVED_KEYS[k]}" for k in removed)
            raise ValueError(f"Config key(s) removed: {details}")
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(
                f"Unknown config key(s): {', '.join(unknown)}. Valid keys: {', '.join(sorted(known))}"
            )
        values = dict(data)
        # JSON has no tuples; tuples keep the frozen config hashable and immutable.
        for key in ("symbols", "trading_hours_server", "enabled_strategies", "holidays"):
            if isinstance(values.get(key), list):
                values[key] = tuple(values[key])
        for block in BLOCKS:
            if block in values:
                values[block] = parse_block(block, values[block])
        cfg = cls(**values)
        cfg.validate()
        return cfg

    @classmethod
    def load_with_notices(cls, path: str | os.PathLike[str]) -> tuple["Config", list[str], dict]:
        """(config, deprecation notices, migrated mapping). The file itself is never modified."""
        from .config_migration import migrate_config_dict

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
        if not isinstance(data, Mapping):
            raise ValueError("Config must be a JSON object")
        migrated, notices = migrate_config_dict(data)
        return cls.from_dict(migrated), notices, migrated

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Config":
        cfg, notices, _migrated = cls.load_with_notices(path)
        for notice in notices:
            log.info("Config migration: %s", notice)
        return cfg

    # ---- derived values

    @property
    def bar_seconds(self) -> int:
        return TIMEFRAME_SECONDS[self.timeframe]

    def strategy_names(self) -> tuple[str, ...]:
        from .strategies import library_names

        return self.enabled_strategies if self.enabled_strategies is not None else library_names()

    def arm_timeframes(self) -> tuple[str, ...]:
        """Timeframes arms trade on: the universe's, always including the primary timeframe."""
        tfs = list(self.universe.timeframes)
        if self.timeframe not in tfs:
            tfs.insert(0, self.timeframe)
        return tuple(tfs)

    # ---- validation

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
        _check_int("bars", self.bars, minimum=200, maximum=20000)
        _check_int("history_months", self.history_months, minimum=4, maximum=60)
        _check_int("magic", self.magic, minimum=1)

        if self.account_mode not in ACCOUNT_MODES:
            raise ValueError(f"account_mode must be one of {', '.join(ACCOUNT_MODES)}, got {self.account_mode!r}")
        _check_bool("real_money_confirmed", self.real_money_confirmed)

        if self.risk_profile not in PROFILE_NAMES:
            raise ValueError(f"risk_profile must be one of {', '.join(PROFILE_NAMES)}, got {self.risk_profile!r}")
        for name in ("risk_per_trade_pct_demo", "risk_per_trade_pct_real"):
            _check_ceiling(name, getattr(self, name), limits.MAX_RISK_PER_TRADE_PCT)
        for name in ("max_daily_loss_pct_demo", "max_daily_loss_pct_real"):
            _check_ceiling(name, getattr(self, name), limits.MAX_DAILY_LOSS_PCT)
        if self.max_open_positions_total is not None:
            _check_int("max_open_positions_total", self.max_open_positions_total, minimum=1,
                       maximum=limits.MAX_OPEN_POSITIONS)
        _check_ceiling("max_total_open_risk_pct", self.max_total_open_risk_pct, limits.MAX_TOTAL_OPEN_RISK_PCT)
        _check_ceiling("max_symbol_exposure_x", self.max_symbol_exposure_x, limits.MAX_SYMBOL_EXPOSURE_X)
        _check_ceiling("max_drawdown_from_peak_pct", self.max_drawdown_from_peak_pct,
                       limits.MAX_DRAWDOWN_FROM_PEAK_PCT)
        trade_caps = [v for v in (self.risk_per_trade_pct_demo, self.risk_per_trade_pct_real) if v is not None]
        if self.max_total_open_risk_pct is not None and trade_caps and self.max_total_open_risk_pct < max(trade_caps):
            raise ValueError("max_total_open_risk_pct must be >= the per-trade risk cap, or no trade could ever open")

        _check_int("deviation_points", self.deviation_points, minimum=0, maximum=1000)
        _check_number("max_slippage_points", self.max_slippage_points, low=0, low_inclusive=False)
        _check_number("slippage_pause_minutes", self.slippage_pause_minutes, low=0)
        self._validate_hours()
        _check_bool("close_on_opposite", self.close_on_opposite)
        if self.commission_per_lot is not None:
            _check_number("commission_per_lot", self.commission_per_lot, low=0)
        _check_number("commission_fallback_per_lot", self.commission_fallback_per_lot, low=0)
        _check_number("backtest_slippage_points", self.backtest_slippage_points, low=0)
        _check_bool("news_filter_enabled", self.news_filter_enabled)
        _check_text("news_file", self.news_file)
        _check_int("news_minutes_before", self.news_minutes_before, minimum=0, maximum=1440)
        _check_int("news_minutes_after", self.news_minutes_after, minimum=0, maximum=1440)

        self._validate_research()

        _check_bool("dry_run", self.dry_run)
        _check_number("poll_seconds", self.poll_seconds, low=1)
        _check_number("closed_market_poll_seconds", self.closed_market_poll_seconds, low=1)
        _check_number("heartbeat_minutes", self.heartbeat_minutes, low=0, low_inclusive=False)
        _check_number("stale_tick_minutes", self.stale_tick_minutes, low=0.5)
        _check_bool("terminal_restart_enabled", self.terminal_restart_enabled)
        _check_number("terminal_freeze_minutes", self.terminal_freeze_minutes, low=1)
        if not isinstance(self.holidays, tuple):
            raise ValueError("holidays must be a list of \"YYYY-MM-DD\" server dates")
        for day in self.holidays:
            if not (isinstance(day, str) and len(day) == 10 and day[4] == "-" and day[7] == "-"):
                raise ValueError(f"holidays entries must be \"YYYY-MM-DD\", got {day!r}")
        _check_number("config_reload_seconds", self.config_reload_seconds, low=5)
        _check_bool("telegram_enabled", self.telegram_enabled)
        for name in ("kill_switch_file", "state_root", "reports_dir", "health_file", "log_file"):
            _check_text(name, getattr(self, name))
        if self.mt5_path is not None:
            _check_text("mt5_path", self.mt5_path)
        _check_int("mt5_init_timeout_ms", self.mt5_init_timeout_ms, minimum=1000, maximum=600000)
        _check_text("config_version", self.config_version)
        for block, cls in BLOCKS.items():
            value = getattr(self, block)
            if not isinstance(value, cls):
                raise ValueError(f"{block} must be a JSON object")
            value.validate()

    def _validate_research(self) -> None:
        from .strategies import library_names

        if self.enabled_strategies is not None:
            if not isinstance(self.enabled_strategies, tuple):
                raise ValueError("enabled_strategies must be a list of strategy names or null")
            valid = library_names()
            bad = [n for n in self.enabled_strategies if n not in valid]
            if bad:
                raise ValueError(f"Unknown strategy name(s) {bad}; valid: {', '.join(valid)}")
        _check_int("trials_per_strategy", self.trials_per_strategy, minimum=1, maximum=500)
        _check_int("max_trials_per_cycle", self.max_trials_per_cycle, minimum=1, maximum=5000)
        _check_int("optimizer_workers", self.optimizer_workers, minimum=1, maximum=64)
        _check_int("optimizer_seed", self.optimizer_seed, minimum=0)
        _check_int("wf_train_months", self.wf_train_months, minimum=1, maximum=24)
        _check_int("wf_test_months", self.wf_test_months, minimum=1, maximum=12)
        _check_int("wf_step_months", self.wf_step_months, minimum=1, maximum=12)
        if self.wf_train_months + self.wf_test_months > self.history_months:
            raise ValueError("history_months must be >= wf_train_months + wf_test_months")
        _check_number("reoptimize_every_days", self.reoptimize_every_days, low=1)

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


def widen_hours(window: tuple[int, int], extra: int) -> tuple[int, int]:
    """``window`` widened by ``extra`` hours on each side, never beyond ``limits.WIDEST_TRADING_HOURS``.
    Overnight windows are returned unchanged (they already span the night)."""
    start, end = window
    if extra <= 0 or start >= end:
        return window
    lo, hi = limits.WIDEST_TRADING_HOURS
    # A user window already wider than the bound is kept as is; widening never shrinks it.
    new_start = start if start <= lo else max(lo, start - extra)
    new_end = end if end >= hi else min(hi, end + extra)
    return (new_start, new_end)


def apply_overrides(cfg: Config, overrides: Mapping[str, Any]) -> Config:
    """Apply CLI overrides. Keys are field names, or ``block.field`` for the blocks
    (for example ``claude.enabled`` or ``team.team_mode``), so a hot reload keeps the rest
    of a block from the file while the override still wins."""
    top: dict[str, Any] = {}
    nested: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if "." in key:
            block, name = key.split(".", 1)
            if block not in BLOCKS:
                raise ValueError(f"unknown config block in override {key!r}")
            nested.setdefault(block, {})[name] = value
        else:
            top[key] = value
    for block, values in nested.items():
        top[block] = dataclasses.replace(getattr(cfg, block), **values)
    merged = dataclasses.replace(cfg, **top) if top else cfg
    merged.validate()
    return merged


def _looser(before: Any, after: Any) -> bool:
    """For a "lower is stricter" key: is ``after`` looser than ``before``? None = no cap."""
    if after is None:
        return before is not None
    if before is None:
        return False
    return after > before


def reload_decision(old: Config, new: Config) -> tuple[Config, list[str]]:
    """Merge a freshly validated config into the running one.

    Returns the config to apply plus human-readable notes about every change that
    was refused (restart-only keys, or safety keys loosened at runtime).
    """
    notes: list[str] = []
    updates: dict[str, Any] = {}
    for field in dataclasses.fields(Config):
        name = field.name
        before, after = getattr(old, name), getattr(new, name)
        if before == after:
            continue
        if name in RESTART_KEYS:
            notes.append(f"{name}: change from {before!r} to {after!r} needs a restart; ignored")
            continue
        if name in TIGHTEN_ONLY_KEYS and _looser(before, after):
            notes.append(f"{name}: loosening from {before!r} to {after!r} needs a restart; ignored")
            continue
        if name == "risk_profile" and PROFILE_ORDER.get(after, 0) > PROFILE_ORDER.get(before, 0):
            notes.append(f"risk_profile: {before} -> {after} is more aggressive and needs a restart; ignored")
            continue
        updates[name] = after
    merged = dataclasses.replace(old, **updates) if updates else old
    merged.validate()
    return merged, notes
