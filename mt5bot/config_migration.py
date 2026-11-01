"""Migration of older config.json files to the current schema.

Rules: map every deprecated key to its new home when one exists; when the mechanism it
controlled no longer exists (the champion/challenger promotion gates of v3.0), keep the
user's value under ``_legacy_v30`` so nothing is silently discarded; emit one notice per
key. ``Config`` ignores keys that start with "_". Loading never touches the file:
``write_migrated`` writes ``config.migrated.json`` next to it, and ``apply_migrated`` (used
when the bot starts) replaces config.json only after saving a dated backup of it.

v4.2 maps ``account_mode``. The old default ``any`` with ``real_money_confirmed: true`` becomes
``real``. With confirmation false it becomes ``auto`` and stays unconfirmed: a migration never
turns on real money that the owner did not already confirm.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from .persistence import atomic_write_text

CURRENT_VERSION = "4.2"
MIGRATED_NAME = "config.migrated.json"
ACCOUNT_MODE_MAP = {"demo_only": "demo", "real_only": "real"}

# v3.0 champion/challenger and optimiser acceptance gates. v3.1 replaces binary promotion
# with evidence tiers: the walk-forward still computes these statistics (shown as quality
# flags in the panel), but they no longer block trading on their own.
SUPERSEDED_BY_TIERS = (
    "min_oos_trades", "min_profit_factor", "max_oos_drawdown_r", "min_window_win_ratio", "bootstrap_confidence",
    "bootstrap_samples", "min_deflated_sharpe", "min_neighbor_pass_ratio", "max_challengers", "min_shadow_trades",
    "min_shadow_days", "min_shadow_trades_floor", "promotion_margin", "switch_cooldown_days", "bootstrap_days",
    "degrade_window_trades", "degrade_min_trades", "degrade_ucb_z", "degrade_dd_multiple", "degrade_max_dd_r",
    "degrade_streak_alpha",
)


def migrate_config_dict(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (migrated mapping, notices). Pure: ``data`` is not modified."""
    out: dict[str, Any] = json.loads(json.dumps(data))  # deep copy of plain JSON
    notices: list[str] = []
    legacy: dict[str, Any] = dict(out.get("_legacy_v30") or {})
    for key in SUPERSEDED_BY_TIERS:
        if key in out:
            legacy[key] = out.pop(key)
            notices.append(f"'{key}' ({legacy[key]!r}) belonged to the v3.0 champion/challenger gates, which evidence "
                           "tiers replace; the value is kept under '_legacy_v30' and no longer blocks trading")
    if "max_spread_points" in out:
        value = out.pop("max_spread_points")
        legacy["max_spread_points"] = value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            universe = dict(out.get("universe") or {})
            per_symbol = dict(universe.get("max_spread_points_by_symbol") or {})
            for symbol in out.get("symbols") or ["EURUSD"]:
                per_symbol.setdefault(str(symbol), value)
            universe["max_spread_points_by_symbol"] = per_symbol
            out["universe"] = universe
            notices.append(f"'max_spread_points' ({value}) is in POINTS, which differ per symbol (FX vs gold vs "
                           f"indices); it now applies only to {', '.join(map(str, per_symbol))} through "
                           "'universe.max_spread_points_by_symbol'. Every symbol also has a relative spread filter")
    team = out.get("team")
    if isinstance(team, Mapping) and team.get("team_mode") == "baseline":
        notices.append("team.team_mode 'baseline' now means 'simple rules: the team records opinions but does not "
                       "resize'; it is no longer a separate engine")
    _migrate_account_mode(out, notices)
    _migrate_universe(out, notices)
    tail = out.get("tail")
    if isinstance(tail, dict) and "small_account_equity" in tail:
        legacy["small_account_equity"] = tail.pop("small_account_equity")
        notices.append("'tail.small_account_equity' was a fixed money band and no longer exists: the weekend rule now "
                       "applies at every balance")
    if legacy:
        out["_legacy_v30"] = legacy
    if out.get("config_version") != CURRENT_VERSION:
        if "config_version" in out or notices:
            notices.append(f"config_version {out.get('config_version', '3.0')!r} -> {CURRENT_VERSION!r}")
        out["config_version"] = CURRENT_VERSION
    return out, notices


def _migrate_account_mode(out: dict[str, Any], notices: list[str]) -> None:
    mode = out.get("account_mode")
    if mode in ACCOUNT_MODE_MAP:
        out["account_mode"] = ACCOUNT_MODE_MAP[mode]
        notices.append(f"account_mode {mode!r} -> {out['account_mode']!r}")
    elif mode == "any":
        confirmed = out.get("real_money_confirmed") is True
        out["account_mode"] = "real" if confirmed else "auto"
        notices.append(f"account_mode 'any' -> {out['account_mode']!r}"
                       + ("" if confirmed else ": real money stays unconfirmed, as it was"))


def _migrate_universe(out: dict[str, Any], notices: list[str]) -> None:
    """``symbols`` and ``timeframe`` are deprecated: their content moves into ``universe``."""
    symbols = out.get("symbols")
    timeframe = out.get("timeframe")
    if symbols is None and timeframe is None:
        return
    universe = dict(out.get("universe") or {})
    if isinstance(symbols, list):
        known = list(universe.get("candidates") or [])
        universe["candidates"] = [str(s) for s in symbols if s not in known] + known
        out.pop("symbols")
        notices.append(f"'symbols' is deprecated: {', '.join(map(str, symbols))} moved into 'universe.candidates'; "
                       "the balance now decides which of them are traded")
    if isinstance(timeframe, str):
        frames = list(universe.get("timeframes") or [])
        if frames and timeframe not in frames:
            universe["timeframes"] = [timeframe] + frames
        out.pop("timeframe")
        notices.append(f"'timeframe' is deprecated: {timeframe} is now one of 'universe.timeframes'")
    if universe:
        out["universe"] = universe


def write_migrated(config_path: Path, migrated: Mapping[str, Any]) -> Path:
    """Write the migrated config next to the original (never over it)."""
    target = Path(config_path).with_name(MIGRATED_NAME)
    text = json.dumps(migrated, indent=2, ensure_ascii=False) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") == text:
        return target
    atomic_write_text(target, text)
    return target


def apply_migrated(config_path: Path, migrated: Mapping[str, Any], stamp: Optional[str] = None) -> Path:
    """Replace config.json with its migrated form after a dated backup. Returns the backup path."""
    path = Path(config_path)
    label = stamp or time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{label}")
    shutil.copy2(path, backup)
    atomic_write_text(path, json.dumps(migrated, indent=2, ensure_ascii=False) + "\n")
    return backup
