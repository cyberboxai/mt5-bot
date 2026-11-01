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
    max_monthly_cost_usd: float = 5.0
    cache_ttl_seconds: float = 300.0
    # Stored as sorted (model, price) pairs so the frozen config stays hashable.
    prices: tuple[tuple[str, ModelPrice], ...] = tuple(
        (model, ModelPrice(**p)) for model, p in sorted(DEFAULT_PRICES.items()))
    probe_degraded_base_seconds: float = 30.0
    probe_degraded_max_seconds: float = 900.0
    probe_offline_seconds: float = 1800.0
    probe_offline_max_seconds: float = 21600.0
    key_recheck_seconds: float = 300.0
    # Per-trade gating is off: language models earn their keep on reports and explanations. When it
    # is switched on, reviews stay shadow-only until ablation shows value after the advisor's cost.
    review_trades: bool = False
    review_requires_ablation: bool = True
    batch_reports: bool = True
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
        _num("claude.max_monthly_cost_usd", self.max_monthly_cost_usd, 0.0, limits.MAX_CLAUDE_MONTHLY_COST_USD)
        _bool("claude.review_requires_ablation", self.review_requires_ablation)
        _bool("claude.batch_reports", self.batch_reports)
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


# ----------------------------------------------------------------------- tiers


def _opt_num(name: str, value: Any, low: float, high: float) -> None:
    if value is not None:
        _num(name, value, low, high)


@dataclass(frozen=True)
class TiersConfig:
    """Evidence tiers and exploration (see ``mt5bot.tiers``). Risk numbers live in the profile."""

    t1_enter_p: float = 0.55
    t1_exit_p: float = 0.50
    t1_min_evidence: int = 30
    t2_enter_p: float = 0.80
    t2_exit_p: float = 0.70
    t2_min_trades: int = 50
    t2_max_dd_r: float = 6.0
    t3_enter_p: float = 0.93
    t3_exit_p: float = 0.85
    t3_min_trades: int = 100
    t3_max_dd_r: float = 12.0
    promote_cooldown_hours: float = 4.0
    exploration_enabled: bool = True
    exploration_slots: int = 2
    exploration_threshold: float = -0.02   # an arm whose posterior mean is below this never explores
    exploration_day_pct: Optional[float] = None
    exploration_week_pct: Optional[float] = None
    t1_risk_pct: Optional[float] = None
    prior_discount: float = 0.3
    tau_min: float = 0.05
    global_mean: float = -0.03
    evidence_refresh_seconds: float = 120.0
    seed: int = 20261026

    @classmethod
    def from_dict(cls, data: Any) -> "TiersConfig":
        cfg = _build(cls, "tiers", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for name in ("t1_enter_p", "t1_exit_p", "t2_enter_p", "t2_exit_p", "t3_enter_p", "t3_exit_p"):
            _num(f"tiers.{name}", getattr(self, name), 0.5, 0.999)
        if not (self.t1_exit_p < self.t1_enter_p and self.t2_exit_p < self.t2_enter_p
                and self.t3_exit_p < self.t3_enter_p):
            raise ValueError("tiers: every *_exit_p must be below its *_enter_p (hysteresis)")
        if not (self.t1_enter_p < self.t2_enter_p < self.t3_enter_p):
            raise ValueError("tiers: t1_enter_p < t2_enter_p < t3_enter_p is required")
        _int("tiers.t1_min_evidence", self.t1_min_evidence, 0, 10000)
        _int("tiers.t2_min_trades", self.t2_min_trades, 10, 10000)
        _int("tiers.t3_min_trades", self.t3_min_trades, self.t2_min_trades, 100000)
        _num("tiers.t2_max_dd_r", self.t2_max_dd_r, 1.0, 100.0)
        _num("tiers.t3_max_dd_r", self.t3_max_dd_r, 1.0, 100.0)
        _num("tiers.promote_cooldown_hours", self.promote_cooldown_hours, 0.0, 24.0 * 14)
        _bool("tiers.exploration_enabled", self.exploration_enabled)
        _int("tiers.exploration_slots", self.exploration_slots, 0, limits.MAX_OPEN_POSITIONS)
        _num("tiers.exploration_threshold", self.exploration_threshold, -0.05, 1.0)
        _opt_num("tiers.exploration_day_pct", self.exploration_day_pct, 0.0, limits.MAX_EXPLORATION_DAY_PCT)
        _opt_num("tiers.exploration_week_pct", self.exploration_week_pct, 0.0, limits.MAX_EXPLORATION_WEEK_PCT)
        _opt_num("tiers.t1_risk_pct", self.t1_risk_pct, 0.001, limits.MAX_EXPLORATION_TRADE_PCT)
        _num("tiers.prior_discount", self.prior_discount, 0.0, 1.0)
        _num("tiers.tau_min", self.tau_min, 0.01, 0.5)
        _num("tiers.global_mean", self.global_mean, -0.5, 0.0)
        _num("tiers.evidence_refresh_seconds", self.evidence_refresh_seconds, 10.0, 3600.0)
        _int("tiers.seed", self.seed, 0, 2 ** 32 - 1)


@dataclass(frozen=True)
class CapitalConfig:
    """Overrides of the profile's capital rules (null = use the profile; see ``mt5bot.capital``)."""

    floor_drawdown_pct: Optional[float] = None
    lock_fraction: Optional[float] = None
    house_money_max: Optional[float] = None
    kelly_fraction: Optional[float] = None
    affordability_cap_pct: Optional[float] = None
    floor_soft_stop: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "CapitalConfig":
        cfg = _build(cls, "capital", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _opt_num("capital.floor_drawdown_pct", self.floor_drawdown_pct, 0.5, limits.MAX_DRAWDOWN_FROM_PEAK_PCT * 0.9)
        _opt_num("capital.lock_fraction", self.lock_fraction, 0.0, limits.MAX_LOCK_FRACTION)
        _opt_num("capital.house_money_max", self.house_money_max, 1.0, limits.MAX_HOUSE_MONEY)
        _opt_num("capital.kelly_fraction", self.kelly_fraction, 0.01, limits.MAX_KELLY_FRACTION)
        _opt_num("capital.affordability_cap_pct", self.affordability_cap_pct, 0.0, limits.MAX_RISK_PER_TRADE_PCT)
        _bool("capital.floor_soft_stop", self.floor_soft_stop)


DEFAULT_CANDIDATES = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY", "BTCUSD",
                      "XAUUSD")
UNIVERSE_TIMEFRAMES = ("M5", "M15", "M30", "H1", "H4", "D1")


@dataclass(frozen=True)
class UniverseConfig:
    """Opportunity engine: which symbols and timeframes the arms may use."""

    candidates: tuple[str, ...] = DEFAULT_CANDIDATES
    timeframes: tuple[str, ...] = ("M5", "M15", "H1", "H4")
    always_include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_symbols: int = 6
    max_arms: int = 1400
    default_arms: bool = True
    max_spread_points_by_symbol: tuple[tuple[str, float], ...] = ()
    spread_spike_mult: float = 2.5
    max_spread_to_stop: float = 0.25
    max_cost_r: float = 0.5                  # all-in cost in R a stop may carry before any edge is measured
    scan_every_minutes: float = 15.0

    @classmethod
    def from_dict(cls, data: Any) -> "UniverseConfig":
        if isinstance(data, Mapping):
            data = dict(data)
            for key in ("candidates", "timeframes", "always_include", "exclude"):
                if isinstance(data.get(key), list):
                    data[key] = tuple(data[key])
            spread = data.get("max_spread_points_by_symbol")
            if isinstance(spread, Mapping):
                data["max_spread_points_by_symbol"] = tuple(sorted((str(k), v) for k, v in spread.items()))
        cfg = _build(cls, "universe", data)
        cfg.validate()
        return cfg

    def spread_cap(self, symbol: str) -> Optional[float]:
        for name, value in self.max_spread_points_by_symbol:
            if name == symbol or symbol.upper().startswith(name.upper()):
                return float(value)
        return None

    def validate(self) -> None:
        if not isinstance(self.candidates, tuple) or not all(isinstance(s, str) and s.strip() for s in self.candidates):
            raise ValueError("universe.candidates must be a list of symbol names")
        for name in ("always_include", "exclude"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ValueError(f"universe.{name} must be a list of symbol names")
        if set(self.always_include) & set(self.exclude):
            raise ValueError("universe.always_include and universe.exclude must not share a symbol")
        if not isinstance(self.timeframes, tuple) or not self.timeframes:
            raise ValueError("universe.timeframes must be a non-empty list such as [\"M5\", \"M15\", \"H1\"]")
        for tf in self.timeframes:
            _choice("universe.timeframes entry", tf, UNIVERSE_TIMEFRAMES)
        if len(set(self.timeframes)) != len(self.timeframes):
            raise ValueError("universe.timeframes must not contain duplicates")
        _int("universe.max_symbols", self.max_symbols, 1, 20)
        _int("universe.max_arms", self.max_arms, 10, 2000)
        _bool("universe.default_arms", self.default_arms)
        if not isinstance(self.max_spread_points_by_symbol, tuple):
            raise ValueError("universe.max_spread_points_by_symbol must be an object such as {\"EURUSD\": 20}")
        for name, value in self.max_spread_points_by_symbol:
            _num(f"universe.max_spread_points_by_symbol.{name}", value, 0.1, 1e6)
        _num("universe.spread_spike_mult", self.spread_spike_mult, 1.2, 10.0)
        _num("universe.max_spread_to_stop", self.max_spread_to_stop, 0.02, 1.0)
        _num("universe.max_cost_r", self.max_cost_r, 0.02, 2.0)
        _num("universe.scan_every_minutes", self.scan_every_minutes, 1.0, 1440.0)


@dataclass(frozen=True)
class FlowDoctorConfig:
    """Adaptive activity governor (see ``mt5bot.flow``). It only moves bounded knobs."""

    enabled: bool = True
    max_idle_hours_demo: float = 6.0
    max_idle_hours_real: float = 12.0
    review_after_trades: int = 10
    review_after_hours: float = 48.0
    revert_mean_r: float = -0.25
    tighten_daily_loss_used: float = 0.6
    min_hours_between_adjustments: float = 2.0
    max_relaxations_per_week: int = 3
    min_counterfactual_signals: int = 10
    min_counterfactual_r: float = 0.0

    @classmethod
    def from_dict(cls, data: Any) -> "FlowDoctorConfig":
        cfg = _build(cls, "flow_doctor", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _int("flow_doctor.max_relaxations_per_week", self.max_relaxations_per_week, 0, 14)
        _int("flow_doctor.min_counterfactual_signals", self.min_counterfactual_signals, 3, 1000)
        _num("flow_doctor.min_counterfactual_r", self.min_counterfactual_r, 0.0, 2.0)
        _bool("flow_doctor.enabled", self.enabled)
        _num("flow_doctor.max_idle_hours_demo", self.max_idle_hours_demo, 1.0, 24.0 * 14)
        _num("flow_doctor.max_idle_hours_real", self.max_idle_hours_real, 1.0, 24.0 * 14)
        _int("flow_doctor.review_after_trades", self.review_after_trades, 3, 1000)
        _num("flow_doctor.review_after_hours", self.review_after_hours, 1.0, 24.0 * 30)
        _num("flow_doctor.revert_mean_r", self.revert_mean_r, -2.0, 0.0)
        _num("flow_doctor.tighten_daily_loss_used", self.tighten_daily_loss_used, 0.1, 1.0)
        _num("flow_doctor.min_hours_between_adjustments", self.min_hours_between_adjustments, 0.25, 72.0)


@dataclass(frozen=True)
class ManagementConfig:
    """Trade management policies, evaluated as evidence like arms (see ``mt5bot.management``)."""

    policies_enabled: bool = True
    policy_min_trades: int = 30
    policy_min_p_better: float = 0.6
    spread_spike_protect: bool = True
    news_derisk_minutes: Optional[int] = 15

    @classmethod
    def from_dict(cls, data: Any) -> "ManagementConfig":
        cfg = _build(cls, "management", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _bool("management.policies_enabled", self.policies_enabled)
        _int("management.policy_min_trades", self.policy_min_trades, 10, 100000)
        _num("management.policy_min_p_better", self.policy_min_p_better, 0.5, 0.999)
        _bool("management.spread_spike_protect", self.spread_spike_protect)
        if self.news_derisk_minutes is not None:
            _int("management.news_derisk_minutes", self.news_derisk_minutes, 1, 240)


@dataclass(frozen=True)
class ResearchConfig:
    """Quant Research department (walk-forward priors for the arms)."""

    startup_trials: int = 8
    startup_history_months: int = 6
    retry_minutes: float = 60.0
    reference_equity: float = 10_000.0
    reference_risk_pct: float = 1.0
    cpcv_groups: int = 6
    cpcv_test_groups: int = 2
    embargo_bars: int = 20
    min_dsr: float = 0.95
    max_pbo: float = 0.5
    min_trades: int = 30
    require_verdict: bool = True      # an arm needs an "edge plausible" verdict to reach T2
    retire_failed: bool = True
    capture_ticks: bool = True
    tick_retention_days: int = 30

    @classmethod
    def from_dict(cls, data: Any) -> "ResearchConfig":
        cfg = _build(cls, "research", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _int("research.startup_trials", self.startup_trials, 1, 200)
        _int("research.startup_history_months", self.startup_history_months, 4, 60)
        _num("research.retry_minutes", self.retry_minutes, 5.0, 24 * 60.0)
        _num("research.reference_equity", self.reference_equity, 1000.0, 1e9)
        _num("research.reference_risk_pct", self.reference_risk_pct, 0.1, limits.MAX_RISK_PER_TRADE_PCT)
        _int("research.cpcv_groups", self.cpcv_groups, 4, 12)
        _int("research.cpcv_test_groups", self.cpcv_test_groups, 1, 4)
        _int("research.embargo_bars", self.embargo_bars, 0, 500)
        _num("research.min_dsr", self.min_dsr, 0.5, 0.999)
        _num("research.max_pbo", self.max_pbo, 0.05, 1.0)
        _int("research.min_trades", self.min_trades, 10, 10_000)
        _bool("research.require_verdict", self.require_verdict)
        _bool("research.retire_failed", self.retire_failed)
        _bool("research.capture_ticks", self.capture_ticks)
        _int("research.tick_retention_days", self.tick_retention_days, 1, 3650)

    def rigor(self, k_cost: float) -> Any:
        from .rigor import RigorSettings

        return RigorSettings(self.cpcv_groups, self.cpcv_test_groups, self.embargo_bars, min_trades=self.min_trades,
                             min_dsr=self.min_dsr, max_pbo=self.max_pbo, k_cost=k_cost)


@dataclass(frozen=True)
class ExecutionConfig:
    """Order execution. ``retryable_retcodes`` may only name definite non-executions: the rule
    that an uncertain order is never re-sent is not configurable."""

    max_attempts: int = 3
    backoff_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    confirm_polls: int = 6
    confirm_poll_seconds: float = 0.25
    uncertain_grace_seconds: float = 120.0
    protect_attempts: int = 3
    protect_retry_seconds: float = 0.5
    stop_tolerance_points: float = 2.0
    retryable_retcodes: tuple[int, ...] = (10004, 10020, 10021, 10024)
    breaker_failures: int = 5
    breaker_open_seconds: float = 300.0
    breaker_half_open_probes: int = 1

    @classmethod
    def from_dict(cls, data: Any) -> "ExecutionConfig":
        cfg = _build(cls, "execution", data)
        if isinstance(cfg.retryable_retcodes, list):
            cfg = dataclasses.replace(cfg, retryable_retcodes=tuple(cfg.retryable_retcodes))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        from .core.orders import validate_retryable

        _int("execution.max_attempts", self.max_attempts, 1, 10)
        _num("execution.backoff_seconds", self.backoff_seconds, 0.0, 30.0)
        _num("execution.backoff_max_seconds", self.backoff_max_seconds, 0.0, 120.0)
        _int("execution.confirm_polls", self.confirm_polls, 1, 40)
        _num("execution.confirm_poll_seconds", self.confirm_poll_seconds, 0.0, 5.0)
        _num("execution.uncertain_grace_seconds", self.uncertain_grace_seconds, 10.0, 3600.0)
        _int("execution.protect_attempts", self.protect_attempts, 1, 10)
        _num("execution.protect_retry_seconds", self.protect_retry_seconds, 0.0, 5.0)
        _num("execution.stop_tolerance_points", self.stop_tolerance_points, 0.0, 100.0)
        _int("execution.breaker_failures", self.breaker_failures, 1, 100)
        _num("execution.breaker_open_seconds", self.breaker_open_seconds, 1.0, 86400.0)
        _int("execution.breaker_half_open_probes", self.breaker_half_open_probes, 1, 10)
        validate_retryable(self.retryable_retcodes)

    def settings(self) -> Any:
        from .core.breaker import BreakerSettings
        from .execution import ExecutionSettings

        return ExecutionSettings(self.max_attempts, self.backoff_seconds, self.backoff_max_seconds, self.confirm_polls,
                                 self.confirm_poll_seconds, self.uncertain_grace_seconds, self.protect_attempts,
                                 self.protect_retry_seconds, self.stop_tolerance_points,
                                 frozenset(self.retryable_retcodes),
                                 BreakerSettings(self.breaker_failures, self.breaker_open_seconds,
                                                 self.breaker_half_open_probes))


@dataclass(frozen=True)
class CostsConfig:
    """Cost-to-edge gate and schedule hygiene. ``k`` has bounds: it can be tuned, not disabled."""

    k: float = 2.0
    edge_z: float = 1.645                    # which posterior lower quantile counts as proven edge: 1.645 is 5%
    gate_enabled: bool = True
    exploration_exempt_demo: bool = True     # demo micro-trades are how own fills get measured
    exploration_exempt_real: bool = True     # v4.2: bounded by the prior threshold and the trade budget instead
    expected_hold_bars: int = 20
    calibrate_every_hours: float = 24.0
    rollover_start: str = "23:50"            # server time; learned windows can only widen this
    rollover_end: str = "00:20"
    friday_cutoff_hour: float = 21.0
    flat_before_weekend: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "CostsConfig":
        cfg = _build(cls, "costs", data)
        cfg.validate()
        return cfg

    @staticmethod
    def minutes(text: str) -> int:
        hour, minute = text.split(":")
        return int(hour) * 60 + int(minute)

    def validate(self) -> None:
        _num("costs.k", self.k, 1.0, 10.0)
        _num("costs.edge_z", self.edge_z, 0.67, 2.33)
        _bool("costs.gate_enabled", self.gate_enabled)
        _bool("costs.exploration_exempt_demo", self.exploration_exempt_demo)
        _bool("costs.exploration_exempt_real", self.exploration_exempt_real)
        _int("costs.expected_hold_bars", self.expected_hold_bars, 1, 1000)
        _num("costs.calibrate_every_hours", self.calibrate_every_hours, 1.0, 24.0 * 30)
        _num("costs.friday_cutoff_hour", self.friday_cutoff_hour, 12.0, 24.0)
        _bool("costs.flat_before_weekend", self.flat_before_weekend)
        for name, value in (("rollover_start", self.rollover_start), ("rollover_end", self.rollover_end)):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value)):
                raise ValueError(f"costs.{name} must be HH:MM, got {value!r}")

    def rules(self) -> Any:
        from .core.costs import ScheduleRules

        return ScheduleRules(self.minutes(self.rollover_start), self.minutes(self.rollover_end),
                             self.friday_cutoff_hour, self.flat_before_weekend)


@dataclass(frozen=True)
class PortfolioConfig:
    """Portfolio construction. Every number here can only shrink a position."""

    max_concurrent_arms: int = 4
    correlation_cap: float = 0.7
    max_cluster_weight: float = 0.5
    max_currency_risk_pct: float = 2.0
    target_correlated_risk_pct: float = 2.5

    @classmethod
    def from_dict(cls, data: Any) -> "PortfolioConfig":
        cfg = _build(cls, "portfolio", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _int("portfolio.max_concurrent_arms", self.max_concurrent_arms, 1, limits.MAX_OPEN_POSITIONS)
        _num("portfolio.correlation_cap", self.correlation_cap, 0.3, 0.95)
        _num("portfolio.max_cluster_weight", self.max_cluster_weight, 0.1, 1.0)
        _num("portfolio.max_currency_risk_pct", self.max_currency_risk_pct, 0.1, limits.MAX_TOTAL_OPEN_RISK_PCT)
        _num("portfolio.target_correlated_risk_pct", self.target_correlated_risk_pct, 0.1,
             limits.MAX_TOTAL_OPEN_RISK_PCT)

    def rules(self) -> Any:
        from .core.portfolio import PortfolioRules

        return PortfolioRules(self.max_concurrent_arms, self.correlation_cap, self.max_cluster_weight,
                              self.max_currency_risk_pct, self.target_correlated_risk_pct)


@dataclass(frozen=True)
class TailConfig:
    """Tail risk. The margin multiple has a floor: 1x stop-out would be no guard at all."""

    margin_stop_out_multiple: float = 3.0
    margin_call_multiple: float = 1.5
    gap_quantile: float = 0.99
    overnight_hours: float = 6.0
    ladder_recovery_days: float = 3.0
    stress_reduction: float = 0.5

    @classmethod
    def from_dict(cls, data: Any) -> "TailConfig":
        cfg = _build(cls, "tail", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _num("tail.margin_stop_out_multiple", self.margin_stop_out_multiple, 2.0, 20.0)
        _num("tail.margin_call_multiple", self.margin_call_multiple, 1.0, 10.0)
        _num("tail.gap_quantile", self.gap_quantile, 0.5, 1.0)
        _num("tail.overnight_hours", self.overnight_hours, 0.0, 48.0)
        _num("tail.ladder_recovery_days", self.ladder_recovery_days, 0.0, 60.0)
        _num("tail.stress_reduction", self.stress_reduction, 0.05, 1.0)

    def rules(self) -> Any:
        from .core.tail import TailRules

        return TailRules(self.margin_stop_out_multiple, self.margin_call_multiple, self.gap_quantile,
                         self.overnight_hours, ladder_recovery_days=self.ladder_recovery_days)


@dataclass(frozen=True)
class GovernanceConfig:
    """The evaluation horizon and the stop rule of the real account."""

    enabled: bool = True
    horizon_trades: int = 200
    horizon_days: float = 91.0
    rolling_min_trades: int = 30
    confidence: float = 0.95

    @classmethod
    def from_dict(cls, data: Any) -> "GovernanceConfig":
        cfg = _build(cls, "governance", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _bool("governance.enabled", self.enabled)
        _int("governance.horizon_trades", self.horizon_trades, 30, 100_000)
        _num("governance.horizon_days", self.horizon_days, 7.0, 3650.0)
        _int("governance.rolling_min_trades", self.rolling_min_trades, 20, 100_000)
        _num("governance.confidence", self.confidence, 0.5, 0.999)

    def rules(self) -> Any:
        from .core.governance import GovernanceRules

        return GovernanceRules(self.horizon_trades, self.horizon_days, self.rolling_min_trades, self.confidence)


@dataclass(frozen=True)
class OpsConfig:
    """Operations: time budget, logs, metrics, clock discipline and backups."""

    cycle_budget_ms: float = 2000.0
    json_logs: bool = False
    metrics_enabled: bool = False
    clock_drift_warn_seconds: float = 5.0
    ntp_server: Optional[str] = None
    backup_enabled: bool = True
    backup_hour_server: int = 3
    backup_keep_days: int = 14
    symbol_info_ttl_seconds: float = 30.0

    @classmethod
    def from_dict(cls, data: Any) -> "OpsConfig":
        cfg = _build(cls, "ops", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _num("ops.cycle_budget_ms", self.cycle_budget_ms, 50.0, 60_000.0)
        _bool("ops.json_logs", self.json_logs)
        _bool("ops.metrics_enabled", self.metrics_enabled)
        _num("ops.clock_drift_warn_seconds", self.clock_drift_warn_seconds, 0.5, 600.0)
        _optional_text("ops.ntp_server", self.ntp_server)
        _bool("ops.backup_enabled", self.backup_enabled)
        _int("ops.backup_hour_server", self.backup_hour_server, 0, 23)
        _int("ops.backup_keep_days", self.backup_keep_days, 1, 3650)
        _num("ops.symbol_info_ttl_seconds", self.symbol_info_ttl_seconds, 0.0, 3600.0)


@dataclass(frozen=True)
class SizingConfig:
    """Broker-granularity sizing (see ``mt5bot.feasibility``). Null values use the profile."""

    min_viable_enabled: bool = True
    min_viable_risk_cap_pct: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Any) -> "SizingConfig":
        cfg = _build(cls, "sizing", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _bool("sizing.min_viable_enabled", self.min_viable_enabled)
        _opt_num("sizing.min_viable_risk_cap_pct", self.min_viable_risk_cap_pct, 0.01, limits.MAX_RISK_PER_TRADE_PCT)


@dataclass(frozen=True)
class BudgetConfig:
    """Exploration budget measured in trades as well as money. Null values use the profile."""

    min_trades_per_day: Optional[int] = None
    min_trades_per_week: Optional[int] = None
    weekly_soft_limit_pct: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Any) -> "BudgetConfig":
        cfg = _build(cls, "budget", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.min_trades_per_day is not None:
            _int("budget.min_trades_per_day", self.min_trades_per_day, 0, limits.MAX_EXPLORATION_TRADES_PER_DAY)
        if self.min_trades_per_week is not None:
            _int("budget.min_trades_per_week", self.min_trades_per_week, 0, limits.MAX_EXPLORATION_TRADES_PER_WEEK)
        _opt_num("budget.weekly_soft_limit_pct", self.weekly_soft_limit_pct, 0.1, limits.MAX_WEEKLY_SOFT_LOSS_PCT)


@dataclass(frozen=True)
class StartupConfig:
    """Warm start: the research sprint and the real-account acknowledgment."""

    research_sprint_minutes: float = 20.0
    sprint_trials: int = 6
    ack_max_age_days: float = 30.0

    @classmethod
    def from_dict(cls, data: Any) -> "StartupConfig":
        cfg = _build(cls, "startup", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _num("startup.research_sprint_minutes", self.research_sprint_minutes, 1.0, 240.0)
        _int("startup.sprint_trials", self.sprint_trials, 1, 100)
        _num("startup.ack_max_age_days", self.ack_max_age_days, 0.0, 3650.0)


@dataclass(frozen=True)
class EntryGatesConfig:
    """What an arm needs for its first, minimum-viable trades (see ``mt5bot.gates``)."""

    min_mean_r: float = 0.0
    min_p: float = 0.55
    min_cost_to_edge: float = 1.2
    min_oos_trades: int = 30

    @classmethod
    def from_dict(cls, data: Any) -> "EntryGatesConfig":
        cfg = _build(cls, "entry_gates", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _num("entry_gates.min_mean_r", self.min_mean_r, 0.0, 1.0)
        _num("entry_gates.min_p", self.min_p, limits.ENTRY_MIN_P_FLOOR, 0.99)
        _num("entry_gates.min_cost_to_edge", self.min_cost_to_edge, limits.ENTRY_MIN_COST_TO_EDGE_FLOOR, 20.0)
        _int("entry_gates.min_oos_trades", self.min_oos_trades, limits.ENTRY_MIN_OOS_TRADES_FLOOR, 100000)


@dataclass(frozen=True)
class ScalingGatesConfig:
    """What an arm needs to size above the minimum (see ``mt5bot.gates``)."""

    min_cost_to_edge: float = 2.0
    require_verdict: bool = True
    max_reality_gap_r: float = 0.15

    @classmethod
    def from_dict(cls, data: Any) -> "ScalingGatesConfig":
        cfg = _build(cls, "scaling_gates", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _num("scaling_gates.min_cost_to_edge", self.min_cost_to_edge, 1.0, 20.0)
        _bool("scaling_gates.require_verdict", self.require_verdict)
        _num("scaling_gates.max_reality_gap_r", self.max_reality_gap_r, 0.01, 5.0)


DRIFT_DETECTORS = ("page_hinkley", "cusum", "off")


@dataclass(frozen=True)
class LearningConfig:
    """Continuous learning (see ``mt5bot.learning``)."""

    half_life_days: float = 60.0
    drift_detector: str = "page_hinkley"
    drift_delta_r: float = 0.15
    drift_threshold_r: float = 15.0
    drift_min_trades: int = 30
    nightly_research: bool = True
    nightly_hour_server: int = 23
    weekend_deep_research: bool = True
    stop_variants: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "LearningConfig":
        cfg = _build(cls, "learning", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _num("learning.half_life_days", self.half_life_days, 1.0, 3650.0)
        _choice("learning.drift_detector", self.drift_detector, DRIFT_DETECTORS)
        _num("learning.drift_delta_r", self.drift_delta_r, 0.0, 2.0)
        _num("learning.drift_threshold_r", self.drift_threshold_r, 0.5, 200.0)
        _int("learning.drift_min_trades", self.drift_min_trades, 5, 100000)
        _bool("learning.nightly_research", self.nightly_research)
        _int("learning.nightly_hour_server", self.nightly_hour_server, 0, 23)
        _bool("learning.weekend_deep_research", self.weekend_deep_research)
        _bool("learning.stop_variants", self.stop_variants)


@dataclass(frozen=True)
class SloConfig:
    """Time-to-first-trade objective, in hours of market activity."""

    first_trade_hours: float = 2.0

    @classmethod
    def from_dict(cls, data: Any) -> "SloConfig":
        cfg = _build(cls, "slo", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _num("slo.first_trade_hours", self.first_trade_hours, 0.25, 168.0)


@dataclass(frozen=True)
class CapacityConfig:
    """How much size the system tries to put through (see ``mt5bot.feasibility``).

    ``lots_per_tick`` is a heuristic: MT5 reports tick counts, not traded volume, so the
    liquidity cap is an estimate until real slippage has been measured."""

    max_child_orders: int = limits.MAX_SPLIT_ORDERS
    child_interval_seconds: float = 30.0
    lots_per_tick: float = 0.25
    hysteresis: float = 0.1

    @classmethod
    def from_dict(cls, data: Any) -> "CapacityConfig":
        cfg = _build(cls, "capacity", data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        _int("capacity.max_child_orders", self.max_child_orders, 1, limits.MAX_SPLIT_ORDERS)
        _num("capacity.child_interval_seconds", self.child_interval_seconds, 0.0, 3600.0)
        _num("capacity.lots_per_tick", self.lots_per_tick, 0.001, 100.0)
        _num("capacity.hysteresis", self.hysteresis, 0.0, 0.5)


BLOCKS: dict[str, type] = {
    "claude": ClaudeConfig,
    "team": TeamConfig,
    "news": NewsConfig,
    "panel": PanelConfig,
    "notifications": NotificationsConfig,
    "tiers": TiersConfig,
    "capital": CapitalConfig,
    "universe": UniverseConfig,
    "flow_doctor": FlowDoctorConfig,
    "management": ManagementConfig,
    "research": ResearchConfig,
    "execution": ExecutionConfig,
    "costs": CostsConfig,
    "portfolio": PortfolioConfig,
    "tail": TailConfig,
    "governance": GovernanceConfig,
    "sizing": SizingConfig,
    "budget": BudgetConfig,
    "startup": StartupConfig,
    "entry_gates": EntryGatesConfig,
    "scaling_gates": ScalingGatesConfig,
    "learning": LearningConfig,
    "slo": SloConfig,
    "capacity": CapacityConfig,
    "ops": OpsConfig,
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
    if isinstance(value, UniverseConfig):
        out = {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
        out["max_spread_points_by_symbol"] = dict(value.max_spread_points_by_symbol)
        return out
    if dataclasses.is_dataclass(value):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    return value
