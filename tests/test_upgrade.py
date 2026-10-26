"""Unification and upgrade: one engine and one decision path, config migration with notices,
legacy (v3.0) database migration with backup and history preserved, rollback."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import sqlite3
import time
from pathlib import Path

import pytest

from conftest import FakeMT5
from mt5bot import bot as botmod
from mt5bot import cli, upgrade
from mt5bot.config import Config
from mt5bot.config_migration import MIGRATED_NAME, SUPERSEDED_BY_TIERS, migrate_config_dict, write_migrated
from mt5bot.db import DB_NAME, SCHEMA_1, SCHEMA_VERSION, db_path, user_version

PROJECT = Path(__file__).resolve().parent.parent
upgrade.DURABLE = False  # like Registry.durable in conftest: no fsync per statement in tests
V30_CONFIG = {
    "symbols": ["EURUSD"], "timeframe": "M15", "bars": 1000, "history_months": 12, "magic": 510001,
    "account_mode": "any", "real_money_confirmed": False, "risk_per_trade_pct_demo": 0.5,
    "risk_per_trade_pct_real": 0.25, "max_daily_loss_pct_demo": 2.0, "max_daily_loss_pct_real": 1.5,
    "max_open_positions_total": 3, "max_total_open_risk_pct": 2.0, "max_symbol_exposure_x": 5.0,
    "max_drawdown_from_peak_pct": 12.0, "max_spread_points": 20, "deviation_points": 10,
    "max_slippage_points": 20, "trading_hours_server": [7, 20], "close_on_opposite": True,
    "commission_per_lot": None, "commission_fallback_per_lot": 7.0, "backtest_slippage_points": 2.0,
    "news_filter_enabled": False, "enabled_strategies": None, "trials_per_strategy": 30,
    "max_trials_per_cycle": 300, "optimizer_workers": 1, "min_oos_trades": 100, "min_profit_factor": 1.3,
    "max_oos_drawdown_r": 12.0, "min_window_win_ratio": 0.6, "min_deflated_sharpe": 0.9, "min_shadow_trades": 30,
    "min_shadow_days": 14, "promotion_margin": 0.15, "switch_cooldown_days": 7, "dry_run": False,
    "poll_seconds": 5, "heartbeat_minutes": 30, "telegram_enabled": False, "kill_switch_file": "KILL",
    "state_root": "state", "reports_dir": "reports", "health_file": "health.json", "log_file": "bot.log",
    "mt5_path": None, "team": {"team_mode": "auto"},
    "claude": {"enabled": False, "max_calls_per_hour": 20, "max_daily_cost_usd": 0.5, "latency_budget_ms": 1500},
    "panel": {"enabled": True, "host": "127.0.0.1", "port": 8765, "control_actions_enabled": False},
}


# ---------------------------------------------------------------- one system


class TestSingleSystem:
    def test_no_engine_or_mode_switch_exists(self) -> None:
        names = {f.name for f in dataclasses.fields(Config)}
        for forbidden in ("engine", "engine_mode", "v2", "v3", "mode", "use_v2", "adaptive_engine"):
            assert forbidden not in names
        flags = {a for a in inspect.getsource(cli.parse_args).split('"') if a.startswith("--")}
        assert not [f for f in flags if "v2" in f or "engine" in f]
        for module in ("engine", "cycle"):
            with pytest.raises(ImportError):
                __import__(f"mt5bot.{module}", fromlist=["x"])
        source = "\n".join(p.read_text(encoding="utf-8") for p in (PROJECT / "mt5bot").rglob("*.py"))
        assert "AdaptiveEngine" not in source and "v2 path" not in source and "v2 core" not in source

    def test_one_decision_path_to_the_broker(self) -> None:
        """Every order goes data -> arms -> allocation -> sizing -> team -> risk -> execution:
        ``order_send`` for an opening deal happens only in Bot._send_entry, reached only from
        Bot._try_open, itself called only by Bot._enter and the team's gateway."""
        tree = ast.parse(inspect.getsource(botmod))
        callers: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                        callers.setdefault(call.func.attr, set()).add(node.name)
        assert callers["_send_entry"] == {"_try_open"}
        assert callers["_try_open"] == {"_enter"}
        assert callers["_enter"] == {"_process_bar"}
        bridge = inspect.getsource(__import__("mt5bot.teambridge", fromlist=["x"]))
        assert bridge.count("_try_open(") == 2  # BotGateway.preview and BotGateway.submit only

    def test_single_entry_point_and_database(self) -> None:
        launcher = (PROJECT / "mt5_bot.py").read_text(encoding="utf-8")
        assert "from mt5bot.cli import main" in launcher and len(launcher.splitlines()) < 40
        assert DB_NAME == "mt5bot.sqlite" and SCHEMA_VERSION >= 2


# --------------------------------------------------------------- config migration


class TestConfigMigration:
    def test_v30_config_maps_with_notices_and_keeps_every_value(self, tmp_path: Path) -> None:
        migrated, notices = migrate_config_dict(V30_CONFIG)
        cfg = Config.from_dict(migrated)
        assert cfg.risk_per_trade_pct_real == 0.25 and cfg.trading_hours_server == (7, 20)
        assert cfg.universe.spread_cap("EURUSD") == 20 and cfg.universe.spread_cap("XAUUSD") is None
        legacy = migrated["_legacy_v30"]
        for key in SUPERSEDED_BY_TIERS:
            if key in V30_CONFIG:
                assert legacy[key] == V30_CONFIG[key]  # nothing silently discarded
        assert legacy["max_spread_points"] == 20 and migrated["config_version"] == "3.1"
        assert len(notices) >= 10 and any("POINTS" in n for n in notices)
        original = tmp_path / "config.json"
        original.write_text(json.dumps(V30_CONFIG), encoding="utf-8")
        before = original.read_bytes()
        target = write_migrated(original, migrated)
        assert target.name == MIGRATED_NAME and original.read_bytes() == before
        assert Config.load(target).risk_per_trade_pct_real == 0.25
        assert migrate_config_dict(json.loads(target.read_text()))[1] == []  # already current: no notices

    def test_shipped_config_needs_no_migration(self) -> None:
        _cfg, notices, _m = Config.load_with_notices(PROJECT / "config.json")
        assert notices == []


# ---------------------------------------------------------- legacy state migration


def build_v30_state(state: Path) -> None:
    """A v3.0 account folder: registry.sqlite (journal, champion, challenger, runs, kv) and
    office.sqlite (events, dossiers, agent weights), plus the risk state file."""
    state.mkdir(parents=True)
    reg = sqlite3.connect(state / "registry.sqlite")
    office = sqlite3.connect(state / "office.sqlite")
    for conn in (reg, office):
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript("BEGIN;" + SCHEMA_1 + "COMMIT;")
    reg.execute("INSERT INTO param_sets (id, strategy, params_json) VALUES (1, 'donchian', ?)",
                (json.dumps({"channel": 40, "sl_mult": 2.0, "tp_mult": 3.0}),))
    reg.execute("INSERT INTO param_sets (id, strategy, params_json) VALUES (2, 'ema_cross', ?)",
                (json.dumps({"fast": 15, "slow": 80, "sl_mult": 2.0, "tp_mult": 3.0}),))
    reg.execute("INSERT INTO optimization_runs (id, symbol, trigger, started_at, status) VALUES (1, 'EURUSD', 'x', 1, 'done')")
    reg.execute("INSERT INTO candidate_metrics (id, run_id, symbol, param_set_id, passed, reasons_json, metrics_json,"
                " score, rank) VALUES (1, 1, 'EURUSD', 2, 1, '[]', '{}', 1.0, 0)")
    reg.execute("INSERT INTO champions (symbol, param_set_id, candidate_id, since) VALUES ('EURUSD', 1, NULL, 5)")
    reg.execute("INSERT INTO challengers (symbol, param_set_id, candidate_id, added) VALUES ('EURUSD', 2, 1, 6)")
    for i in range(12):
        reg.execute("INSERT INTO trades (mode, symbol, param_set_id, side, status, open_ts, close_ts, entry, sl, tp,"
                    " volume, risk_money, pnl, r) VALUES ('shadow', 'EURUSD', ?, 'BUY', 'closed', ?, ?, 1.1, 1.09,"
                    " 1.12, 0.1, 50, 25, 0.5)", (1 + i % 2, 100 + i, 200 + i))
    reg.execute("INSERT INTO trades (mode, symbol, param_set_id, side, status, open_ts, entry, sl, tp, volume,"
                " risk_money, ticket) VALUES ('live', 'EURUSD', 1, 'SELL', 'open', 300, 1.1, 1.11, 1.08, 0.1, 50, 777)")
    reg.execute("INSERT INTO promotions (ts, symbol, action, old_desc, new_desc, reason, metrics_json) "
                "VALUES (5, 'EURUSD', 'promote', 'NoTrade', 'donchian', 'gates passed', '{}')")
    reg.execute("INSERT INTO kv (key, value) VALUES ('server_offset', '7200')")
    office.execute("INSERT INTO events (ts, topic, agent, payload_json) VALUES (1, 'agent.state', 'cio', '{}')")
    office.execute("INSERT INTO dossiers (id, ts, symbol, mode, status) VALUES ('d1', 2, 'EURUSD', 'baseline', 'skipped')")
    office.execute("INSERT INTO agent_weights (agent, regime, log_weight, n, updated_ts) VALUES ('trend_analyst',"
                   " 'all', 0.1, 40, 3)")
    office.execute("INSERT INTO kv (key, value) VALUES ('perf_benched', '[]')")
    for conn in (reg, office):
        conn.commit()
        conn.close()
    (state / "risk_state.json").write_text(json.dumps({"day": "2026-10-25", "start_equity": 10000.0,
                                                       "daily_halted": False, "peak_equity": 10100.0,
                                                       "dd_halted": False, "dd_tripped_at": None}))


class TestStateMigration:
    def test_migrates_with_backup_and_keeps_all_history(self, tmp_path: Path) -> None:
        state = tmp_path / "state" / "Broker-Demo_87654321"
        build_v30_state(state)
        config = tmp_path / "config.json"
        config.write_text(json.dumps(V30_CONFIG), encoding="utf-8")
        report = upgrade.migrate_installation(tmp_path, config, tmp_path / "state", None, "M15")
        backup = Path(report["backup"])
        manifest = json.loads((backup / "manifest.json").read_text())
        names = {Path(f["backup"]).name for f in manifest["files"]}
        assert {"registry.sqlite", "office.sqlite", "risk_state.json", "config.json"} <= names
        assert all(len(f["sha256"]) == 64 for f in manifest["files"])
        assert (state / DB_NAME).exists() and not (state / "registry.sqlite").exists()
        assert (state / "registry.sqlite.v30").exists() and (state / "office.sqlite.v30").exists()
        conn = sqlite3.connect(state / DB_NAME)
        conn.row_factory = sqlite3.Row
        assert user_version(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 13
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM dossiers").fetchone()[0] == 1
        assert conn.execute("SELECT log_weight FROM agent_weights").fetchone()[0] == 0.1
        assert conn.execute("SELECT value FROM kv WHERE key = 'server_offset'").fetchone()[0] == "7200"
        assert conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0] == 1
        arms = list(conn.execute("SELECT * FROM arms ORDER BY param_set_id"))
        assert [(a["param_set_id"], a["source"], a["tier"], a["timeframe"]) for a in arms] == \
            [(1, "legacy", "T0", "M15"), (2, "legacy", "T0", "M15")]
        unlinked = conn.execute("SELECT COUNT(*) FROM trades WHERE param_set_id IS NOT NULL AND arm_id IS NULL")
        assert unlinked.fetchone()[0] == 0  # every past trade is now evidence for its arm
        conn.close()
        # Idempotent: a second run neither backs up again nor changes anything.
        again = upgrade.migrate_installation(tmp_path, config, tmp_path / "state", None, "M15")
        assert again["backup"] is None and again["accounts"][0]["action"] == "current"

    def test_the_migrated_history_feeds_the_evidence(self, tmp_path: Path) -> None:
        from mt5bot.arms import ArmEngine
        from mt5bot.registry import Registry

        state = tmp_path / "state" / "B_1"
        build_v30_state(state)
        upgrade.migrate_state_dir(state, "M15")
        reg = Registry(db_path(state))
        try:
            engine = ArmEngine(reg, Config().tiers, Config().research)
            posts = engine.posteriors()
            assert sorted(p.n_obs for p in posts.values()) == [6, 6]
        finally:
            reg.close()

    def test_rollback_restores_and_keeps_the_replaced_state(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        build_v30_state(state_root / "B_1")
        config = tmp_path / "config.json"
        config.write_text(json.dumps(V30_CONFIG), encoding="utf-8")
        upgrade.migrate_installation(tmp_path, config, state_root, None, "M15")
        config.write_text(json.dumps({"risk_profile": "balanced"}), encoding="utf-8")
        health = tmp_path / "health.json"
        health.write_text("{}")
        with pytest.raises(ValueError, match="running"):
            upgrade.rollback_upgrade(tmp_path, config, state_root, health_file=health)
        report = upgrade.rollback_upgrade(tmp_path, config, state_root, health_file=health, force=True)
        assert (state_root / "B_1" / "registry.sqlite").exists() and not (state_root / "B_1" / DB_NAME).exists()
        assert json.loads(config.read_text())["min_shadow_days"] == 14  # the v3.0 config is back
        aside = Path(report["moved_aside_to"])
        assert (aside / "state" / "B_1" / DB_NAME).exists() and (aside / "config.json").exists()
        with pytest.raises(ValueError, match="no upgrade backup"):
            upgrade.rollback_upgrade(tmp_path / "elsewhere", config, state_root)

    def test_bot_start_migrates_automatically(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
        from mt5bot.persistence import account_key

        state = tmp_path / "state" / account_key("Broker-Demo", 87654321)
        build_v30_state(state)
        bot = make_bot()
        bot.startup()
        assert (state / DB_NAME).exists() and list((tmp_path / "backups").glob("upgrade_*"))
        assert bot.registry.trade_by_ticket(777) is not None  # the open v3.0 position is still journaled
        bot.close()

    def test_cli_migrate_and_rollback(self, tmp_path: Path, capsys) -> None:
        state_root = tmp_path / "state"
        build_v30_state(state_root / "B_9")
        config = tmp_path / "config.json"
        config.write_text(json.dumps(dict(V30_CONFIG, state_root=str(state_root),
                                          health_file=str(tmp_path / "health.json"))), encoding="utf-8")
        assert cli.main(["--config", str(config), "--migrate"]) == 0
        out = capsys.readouterr().out
        assert "Backup:" in out and "merged" in out and MIGRATED_NAME in out
        assert (tmp_path / MIGRATED_NAME).exists() and json.loads(config.read_text())["min_shadow_days"] == 14
        time.sleep(1.1)  # a distinct backup folder name for the forced second backup
        assert cli.main(["--config", str(config), "--rollback-upgrade"]) == 0
        assert "Restored" in capsys.readouterr().out
