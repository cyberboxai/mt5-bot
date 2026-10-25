"""v3 configuration blocks: ``claude``, ``team``, ``news``, ``panel`` and ``notifications``.

Each block is a frozen dataclass parsed from a JSON object, validated on load, with hard
ceilings from ``limits.py``. Unknown keys are rejected with the list of valid ones, the
same way the top-level config does it. Secrets never live here: the Claude API key and
the panel token are read from environment variables whose NAMES may be configured.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from . import limits

TEAM_MODES = ("auto", "full", "baseline")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
PANEL_LANGUAGES = ("pt-PT", "en")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,63}$")

# Defaults verified against the Claude API model table (2026-10): Haiku 5.5 is the small,
# fast model; Opus 5.5 the stronger one. Prices are USD per million tokens and are only
# used for the local cost estimate; check the current price list before relying on them.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
}
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")


def _num(name: str, value: Any, low: Optional[float] = None, high: Optional[float] = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if low is not None and value < low:
        raise ValueError(f"{name} must be >= {low}, got {value}")
    if high is not None and value > high:
        raise ValueError(f"{name} must be <= {high}, got {value}")


def _int(name: str, value: Any, low: Optional[int] = None, high: Optional[int] = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    _num(name, value, low, high)


def _bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")


def _choice(name: str, value: Any, options: tuple[str, ...]) -> None:
    if value not in options:
        raise ValueError(f"{name} must be one of {', '.join(options)}, got {value!r}")


def _optional_text(name: str, value: Any) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be a non-empty string or null, got {value!r}")


def _build(cls: type, block: str, data: Any) -> Any:
    """Instantiate a block dataclass from a JSON object, rejecting unknown keys."""
    if isinstance(data, cls):
        return data
    if not isinstance(data, Mapping):
        raise ValueError(f"{block} must be a JSON object")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"Unknown key(s) in {block}: {', '.join(unknown)}. Valid keys: {', '.join(sorted(known))}")
    return cls(**dict(data))


# --------------------------------------------------------------------------- claude


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class ClaudeConfig:
    """Optional Claude advisor. Off by default; the bot is complete without it."""

    enabled: bool = False
    model_fast: str = "claude-haiku-5-5"
    model_deep: str = "claude-opus-5-5"
    effort_fast: str = "low"
    effort_deep: str = "medium"
    max_tokens_fast: int = 1024
    max_tokens_deep: int = 4096
    timeout_seconds: float = 10.0
    latency_budget_ms: int = 1500
    max_calls_per_hour: int = 20
    max_daily_cost_usd: float = 0.50
    cache_ttl_seconds: float = 300.0
    # Stored as sorted (model, price) pairs so the frozen config stays hashable.
    prices: tuple[tuple[str, ModelPrice], ...] = tuple(
        (model, ModelPrice(**p)) for model, p in sorted(DEFAULT_PRICES.items()))
    probe_degraded_base_seconds: float = 30.0
    probe_degraded_max_seconds: float = 900.0
    probe_offline_seconds: float = 1800.0
    probe_offline_max_seconds: float = 21600.0
    key_recheck_seconds: float = 300.0
    review_trades: bool = True
    manage_positions: bool = False
    daily_briefing: bool = True
    post_trade_reviews: bool = True
    weekly_research: bool = True
    explain_decisions: bool = True
    refusal_fallback_deep: bool = True
    use_keyring: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "ClaudeConfig":
        if isinstance(data, Mapping) and "prices" in data:
            data = dict(data)
            data["prices"] = _parse_prices(data["prices"])
        cfg = _build(cls, "claude", data)
        cfg.validate()
        return cfg

    def price(self, model: str) -> tuple[ModelPrice, bool]:
        """Price for ``model`` and whether it was known. Unknown models use the most expensive entry."""
        table = dict(self.prices)
        if model in table:
            return table[model], True
        worst = max(table.values(), key=lambda p: (p.output, p.input))
        return worst, False

    def validate(self) -> None:
        _bool("claude.enabled", self.enabled)
        for name in ("model_fast", "model_deep"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _MODEL_ID.match(value):
                raise ValueError(f"claude.{name} must be a model id such as claude-haiku-5-5, got {value!r}")
        _choice("claude.effort_fast", self.effort_fast, EFFORT_LEVELS)
        _choice("claude.effort_deep", self.effort_deep, EFFORT_LEVELS)
        _int("claude.max_tokens_fast", self.max_tokens_fast, 64, limits.MAX_CLAUDE_MAX_TOKENS)
        _int("claude.max_tokens_deep", self.max_tokens_deep, 256, limits.MAX_CLAUDE_MAX_TOKENS)
        _num("claude.timeout_seconds", self.timeout_seconds, 1.0, limits.MAX_ADVISOR_TIMEOUT_SECONDS)
        _int("claude.latency_budget_ms", self.latency_budget_ms, 50, limits.MAX_ADVISOR_LATENCY_BUDGET_MS)
        _int("claude.max_calls_per_hour", self.max_calls_per_hour, 1, limits.MAX_CLAUDE_CALLS_PER_HOUR)
        _num("claude.max_daily_cost_usd", self.max_daily_cost_usd, 0.0, limits.MAX_CLAUDE_DAILY_COST_USD)
        _num("claude.cache_ttl_seconds", self.cache_ttl_seconds, 0.0, 86400.0)
        _num("claude.probe_degraded_base_seconds", self.probe_degraded_base_seconds, 5.0, 3600.0)
        _num("claude.probe_degraded_max_seconds", self.probe_degraded_max_seconds,
             self.probe_degraded_base_seconds, 86400.0)
        # Sticky offline states must never hammer the API: at least 5 minutes between probes.
        _num("claude.probe_offline_seconds", self.probe_offline_seconds, 300.0, 86400.0)
        _num("claude.probe_offline_max_seconds", self.probe_offline_max_seconds, self.probe_offline_seconds, 86400.0)
        _num("claude.key_recheck_seconds", self.key_recheck_seconds, 30.0, 86400.0)
        for name in ("review_trades", "manage_positions", "daily_briefing", "post_trade_reviews", "weekly_research",
                     "explain_decisions", "refusal_fallback_deep", "use_keyring"):
            _bool(f"claude.{name}", getattr(self, name))
        if not self.prices:
            raise ValueError("claude.prices must list at least one model")


def _parse_prices(raw: Any) -> tuple[tuple[str, ModelPrice], ...]:
    if isinstance(raw, tuple):
        return raw
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("claude.prices must be an object such as {\"claude-haiku-5-5\": {\"input\": 0.1, ...}}")
    out = []
    for model, entry in raw.items():
        if not isinstance(model, str) or not _MODEL_ID.match(model):
            raise ValueError(f"claude.prices: invalid model id {model!r}")
        if not isinstance(entry, Mapping) or set(entry) != set(PRICE_FIELDS):
            raise ValueError(f"claude.prices.{model} must have exactly {', '.join(PRICE_FIELDS)}")
        for key in PRICE_FIELDS:
            _num(f"claude.prices.{model}.{key}", entry[key], 0.0, 1000.0)
        out.append((model, ModelPrice(**{k: float(entry[k]) for k in PRICE_FIELDS})))
    return tuple(sorted(out))


# ----------------------------------------------------------------------------- team


@dataclass(frozen=True)
class PositionManagerConfig:
    """Risk-reducing trade management. Every rule is OFF by default: the validated
    backtests and shadow books do not model them, so enabling one changes the exits the
    strategy was validated with."""

    breakeven_after_r: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    time_stop_bars: Optional[int] = None
    partial_close_at_r: Optional[float] = None
    partial_close_fraction: float = 0.5
    pre_news_close_minutes: Optional[int] = None

    def validate(self) -> None:
        if self.breakeven_after_r is not None:
            _num("team.position_manager.breakeven_after_r", self.breakeven_after_r, 0.25, 10.0)
        if self.trailing_atr_mult is not None:
            _num("team.position_manager.trailing_atr_mult", self.trailing_atr_mult, limits.MIN_SL_ATR_MULT,
                 limits.MAX_SL_ATR_MULT)
        if self.time_stop_bars is not None:
            _int("team.position_manager.time_stop_bars", self.time_stop_bars, 1, 10000)
        if self.partial_close_at_r is not None:
            _num("team.position_manager.partial_close_at_r", self.partial_close_at_r, 0.25, 10.0)
        _num("team.position_manager.partial_close_fraction", self.partial_close_fraction, 0.1, 0.9)
        if self.pre_news_close_minutes is not None:
            _int("team.position_manager.pre_news_close_minutes", self.pre_news_close_minutes, 1, 240)

    @property
    def any_enabled(self) -> bool:
        return any(v is not None for v in (self.breakeven_after_r, self.trailing_atr_mult, self.time_stop_bars,
                                           self.partial_close_at_r, self.pre_news_close_minutes))


@dataclass(frozen=True)
class TeamConfig:
    team_mode: str = "auto"
    # Sorted (agent_id, enabled) pairs; JSON form is {"agent_id": true|false}.
    agents: tuple[tuple[str, bool], ...] = ()
    weight_min: float = 0.25
    weight_max: float = 2.0
    learning_rate: float = 0.05
    weight_decay: float = 0.01
    warmup_outcomes: int = 30
    min_agreement: int = 1
    # Aggregate confidence = weighted mean probability, across voters, that the proposed
    # direction is right (0.5 = no information). Below the minimum: no trade; at or above
    # full_size_confidence: no reduction from confidence.
    min_aggregate_confidence: float = 0.52
    full_size_confidence: float = 0.65
    max_disagreement: float = 0.5
    flat_vote_weight: float = 0.5
    agent_timeout_ms: int = 250
    agent_max_failures: int = 3
    agent_failure_window_seconds: float = 600.0
    agent_restart_backoff_seconds: float = 60.0
    calibration_min_samples: int = 50
    bench_min_samples: int = 100
    regime_min_trades: int = 20
    ablation_every_hours: float = 24.0
    ablation_min_samples: int = 50
    ablation_confidence: float = 0.90
    meta_min_train: int = 100
    meta_retrain_every: int = 25
    meta_holdout_fraction: float = 0.3
    meta_l2: float = 1.0
    correlation_threshold: float = 0.8
    correlation_lookback_bars: int = 500
    guardian_kill_enabled: bool = True
    guardian_freeze_clear_checks: int = 3
    events_retention_days: float = 14.0
    max_event_rows: int = 200_000
    dossier_retention_days: float = 365.0
    position_manager: PositionManagerConfig = field(default_factory=PositionManagerConfig)

    @classmethod
    def from_dict(cls, data: Any) -> "TeamConfig":
        if isinstance(data, Mapping):
            data = dict(data)
            if "agents" in data and isinstance(data["agents"], Mapping):
                data["agents"] = tuple(sorted((str(k), v) for k, v in data["agents"].items()))
            if "position_manager" in data:
                data["position_manager"] = _build(PositionManagerConfig, "team.position_manager",
                                                  data["position_manager"])
        cfg = _build(cls, "team", data)
        cfg.validate()
        return cfg

    def agent_enabled(self, agent_id: str) -> bool:
        return dict(self.agents).get(agent_id, True)

    def validate(self) -> None:
        from .team.roster import AGENT_IDS, NEVER_DISABLED

        _choice("team.team_mode", self.team_mode, TEAM_MODES)
        if not isinstance(self.agents, tuple):
            raise ValueError("team.agents must be an object such as {\"news_sentinel\": false}")
        for agent_id, enabled in self.agents:
            if agent_id not in AGENT_IDS:
                raise ValueError(f"team.agents: unknown agent {agent_id!r}; valid: {', '.join(AGENT_IDS)}")
            _bool(f"team.agents.{agent_id}", enabled)
            if not enabled and agent_id in NEVER_DISABLED:
                raise ValueError(f"team.agents.{agent_id} cannot be disabled (safety-critical agent)")
        _num("team.weight_min", self.weight_min, limits.MIN_AGENT_WEIGHT, 1.0)
        _num("team.weight_max", self.weight_max, 1.0, limits.MAX_AGENT_WEIGHT)
        _num("team.learning_rate", self.learning_rate, 0.0, limits.MAX_LEARNING_RATE)
        _num("team.weight_decay", self.weight_decay, 0.0, 0.5)
        _int("team.warmup_outcomes", self.warmup_outcomes, 1, 100000)
        _int("team.min_agreement", self.min_agreement, 1, 21)
        _num("team.min_aggregate_confidence", self.min_aggregate_confidence, 0.5, 0.95)
        _num("team.full_size_confidence", self.full_size_confidence, 0.5, 1.0)
        _num("team.flat_vote_weight", self.flat_vote_weight, 0.0, 1.0)
        if self.full_size_confidence <= self.min_aggregate_confidence:
            raise ValueError("team.full_size_confidence must be > team.min_aggregate_confidence")
        _num("team.max_disagreement", self.max_disagreement, 0.0, 1.0)
        _int("team.agent_timeout_ms", self.agent_timeout_ms, 10, limits.MAX_AGENT_TIMEOUT_MS)
        _int("team.agent_max_failures", self.agent_max_failures, 1, 100)
        _num("team.agent_failure_window_seconds", self.agent_failure_window_seconds, 10.0, 86400.0)
        _num("team.agent_restart_backoff_seconds", self.agent_restart_backoff_seconds, 1.0, 86400.0)
        _int("team.calibration_min_samples", self.calibration_min_samples, 10, 100000)
        _int("team.bench_min_samples", self.bench_min_samples, 20, 100000)
        _int("team.regime_min_trades", self.regime_min_trades, 5, 100000)
        _num("team.ablation_every_hours", self.ablation_every_hours, 1.0, 24.0 * 30)
        _int("team.ablation_min_samples", self.ablation_min_samples, 20, 100000)
        _num("team.ablation_confidence", self.ablation_confidence, 0.5, 0.999)
        _int("team.meta_min_train", self.meta_min_train, 30, 100000)
        _int("team.meta_retrain_every", self.meta_retrain_every, 1, 100000)
        _num("team.meta_holdout_fraction", self.meta_holdout_fraction, 0.1, 0.5)
        _num("team.meta_l2", self.meta_l2, 0.0, 1000.0)
        _num("team.correlation_threshold", self.correlation_threshold, 0.3, 0.99)
        _int("team.correlation_lookback_bars", self.correlation_lookback_bars, 50, 20000)
        _bool("team.guardian_kill_enabled", self.guardian_kill_enabled)
        _int("team.guardian_freeze_clear_checks", self.guardian_freeze_clear_checks, 1, 100)
        _num("team.events_retention_days", self.events_retention_days, 1.0, 3650.0)
        _int("team.max_event_rows", self.max_event_rows, 1000, 5_000_000)
        _num("team.dossier_retention_days", self.dossier_retention_days, 7.0, 3650.0)
        if not isinstance(self.position_manager, PositionManagerConfig):
            raise ValueError("team.position_manager must be an object")
        self.position_manager.validate()


# ----------------------------------------------------------------------------- news


@dataclass(frozen=True)
class NewsConfig:
    """News and Calendar Sentinel. Works with no feed at all (it then only reports "no calendar")."""

    sentinel_enabled: bool = True
    calendar_file: Optional[str] = None
    impact_levels: tuple[str, ...] = ("high",)
    blackout_minutes_before: int = 30
    blackout_minutes_after: int = 15
    headlines_file: Optional[str] = None
    advisor_headlines: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "NewsConfig":
        if isinstance(data, Mapping) and isinstance(data.get("impact_levels"), list):
            data = dict(data)
            data["impact_levels"] = tuple(str(x).lower() for x in data["impact_levels"])
        cfg = _build(cls, "news", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _bool("news.sentinel_enabled", self.sentinel_enabled)
        _optional_text("news.calendar_file", self.calendar_file)
        if not isinstance(self.impact_levels, tuple) or not self.impact_levels:
            raise ValueError("news.impact_levels must be a non-empty list such as [\"high\"]")
        for level in self.impact_levels:
            _choice("news.impact_levels entry", level, ("high", "medium", "low"))
        _int("news.blackout_minutes_before", self.blackout_minutes_before, 0, 1440)
        _int("news.blackout_minutes_after", self.blackout_minutes_after, 0, 1440)
        _optional_text("news.headlines_file", self.headlines_file)
        _bool("news.advisor_headlines", self.advisor_headlines)


# ---------------------------------------------------------------------------- panel


@dataclass(frozen=True)
class PanelConfig:
    """CYBERBOX Trading Office web panel (separate process, read-only by default)."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "MT5BOT_PANEL_TOKEN"
    language: str = "pt-PT"
    theme: str = "dark"
    control_actions_enabled: bool = False
    control_rate_per_minute: int = 5
    max_streams: int = 8
    stream_poll_ms: int = 500

    @classmethod
    def from_dict(cls, data: Any) -> "PanelConfig":
        cfg = _build(cls, "panel", data)
        cfg.validate()
        return cfg

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    def validate(self) -> None:
        _bool("panel.enabled", self.enabled)
        if not isinstance(self.host, str) or not re.match(r"^[A-Za-z0-9.:\-]{1,253}$", self.host):
            raise ValueError(f"panel.host must be a host name or IP address, got {self.host!r}")
        _int("panel.port", self.port, 1024, 65535)
        if not isinstance(self.token_env, str) or not _ENV_NAME.match(self.token_env):
            raise ValueError(f"panel.token_env must be an environment variable NAME, got {self.token_env!r}")
        _choice("panel.language", self.language, PANEL_LANGUAGES)
        _choice("panel.theme", self.theme, ("dark", "light"))
        _bool("panel.control_actions_enabled", self.control_actions_enabled)
        _int("panel.control_rate_per_minute", self.control_rate_per_minute, 1, 60)
        _int("panel.max_streams", self.max_streams, 1, 64)
        _int("panel.stream_poll_ms", self.stream_poll_ms, 100, 10000)


# -------------------------------------------------------------------- notifications


@dataclass(frozen=True)
class NotificationsConfig:
    """Which v3 events are sent through the (optional) Telegram notifier."""

    advisor_state: bool = True
    guardian: bool = True
    team_mode_changes: bool = True
    daily_html_report: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "NotificationsConfig":
        cfg = _build(cls, "notifications", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for f in dataclasses.fields(self):
            _bool(f"notifications.{f.name}", getattr(self, f.name))


BLOCKS: dict[str, type] = {
    "claude": ClaudeConfig,
    "team": TeamConfig,
    "news": NewsConfig,
    "panel": PanelConfig,
    "notifications": NotificationsConfig,
}


def parse_block(name: str, data: Any) -> Any:
    return BLOCKS[name].from_dict(data)  # type: ignore[attr-defined]


def to_jsonable(value: Any) -> Any:
    """Inverse of the parsers above, for showing the effective config (no secrets live here)."""
    if isinstance(value, ClaudeConfig):
        out = {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
        out["prices"] = {m: dataclasses.asdict(p) for m, p in value.prices}
        return out
    if isinstance(value, TeamConfig):
        out = {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
        out["agents"] = dict(value.agents)
        return out
    if dataclasses.is_dataclass(value):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    return value
