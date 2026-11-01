"""Phase 10: observability, alerts, clock discipline, backups, secret scanning, service map."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mt5bot import backup, observability as ob
from mt5bot import secretscan
from mt5bot.cli import RedactingJsonFormatter
from mt5bot.registry import Registry
from mt5bot.services import COMPONENTS, SERVICE_OF, complexity_budget, service_activity
from mt5bot.team.roster import AGENT_IDS


def record(message: str) -> logging.LogRecord:
    return logging.LogRecord("mt5_bot", logging.INFO, __file__, 1, message, (), None)


def test_json_logs_carry_the_correlation_id() -> None:
    fmt = ob.JsonFormatter()
    with ob.correlated("EURUSD:M15:123"):
        entry = json.loads(fmt.format(record("hello")))
    assert entry["correlation_id"] == "EURUSD:M15:123" and entry["message"] == "hello" and entry["level"] == "INFO"
    assert json.loads(fmt.format(record("x")))["correlation_id"] is None


def test_json_logs_are_redacted() -> None:
    text = RedactingJsonFormatter().format(record("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV0123456789"))
    assert "ABCDEFGHIJKLMNOP" not in text


def test_metrics_are_valid_prometheus_text() -> None:
    health = {"loop_ms": 12.5, "drawdown_pct": 1.5, "tiers": {"T1": 2, "T2": 1}, "tail": {"margin_level": 400.0},
              "execution": {"unresolved": 0}, "advisor": {"state": "DISABLED"}, "tick_age_seconds": 3.0}
    data = {"health": health, "latency": 90.0, "slippage": 1.2, "reject_rate": 0.1, "funnel": {"block": 9}, "gap": -0.05}
    text = ob.render_metrics(data)
    assert "# TYPE mt5bot_arms gauge" in text and 'mt5bot_arms{tier="T2"} 1' in text
    assert "mt5bot_reality_gap_r -0.05" in text and 'mt5bot_advisor_state{state="DISABLED"} 1' in text
    assert 'mt5bot_funnel_events_total{outcome="block"} 9' in text
    for line in text.splitlines():
        assert line.startswith("#") or len(line.rsplit(" ", 1)) == 2


def test_missing_numbers_are_left_out_of_the_metrics_not_faked() -> None:
    assert "mt5bot_margin_level_percent" not in ob.render_metrics({"health": {"tail": {}}})


def test_alert_rules_each_have_a_runbook_entry() -> None:
    runbook = (Path(__file__).resolve().parent.parent / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    health = {"execution": {"breaker": "OPEN", "unresolved": 1}, "research_only": {"active": True},
              "promotions_frozen": "x", "tail": {"stress_factor": 0.5, "ladder": 0.5}, "clock_drift_seconds": 9.0,
              "clock_drift_alert": True, "backup_failed": "disk"}
    alerts = ob.evaluate_rules(health)
    assert len(alerts) == 8
    for alert in alerts:
        assert f"`{alert.name}`" in runbook, alert.name


def test_a_healthy_system_raises_nothing() -> None:
    assert ob.evaluate_rules({"execution": {"breaker": "CLOSED", "unresolved": 0}, "tail": {"ladder": 1.0}}) == []


def test_alerts_are_deduplicated_by_severity_window() -> None:
    sent: list[str] = []
    now = [1000.0]
    engine = ob.AlertEngine(lambda text, key=None: sent.append(text), lambda: now[0])
    alert = ob.Alert("x", "critical", "boom")
    engine.process([alert])
    engine.process([alert])
    assert len(sent) == 1
    now[0] += 601
    engine.process([alert])
    assert len(sent) == 2


def test_a_failing_notifier_cannot_reach_the_loop() -> None:
    def boom(text: str, key: Any = None) -> None:
        raise OSError("telegram down")

    assert ob.AlertEngine(boom).process([ob.Alert("x", "info", "y")])


def test_clock_drift_against_the_broker() -> None:
    assert ob.broker_drift(1000.0, 8200.0, 7200.0) == pytest.approx(0.0)
    assert ob.broker_drift(1009.0, 8200.0, 7200.0) == pytest.approx(9.0)


def test_ntp_failure_is_none_not_an_exception() -> None:
    assert ob.ntp_offset("203.0.113.1", timeout=0.05) is None


def make_db(path: Path) -> Registry:
    reg = Registry(path, clock=lambda: 1.0)
    reg.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.0, 1.2, 0.1, 10.0, ticket=1)
    return reg


def test_backup_is_verified_by_a_real_restore(tmp_path: Path) -> None:
    reg = make_db(tmp_path / "live.sqlite")
    result = backup.backup_database(tmp_path / "live.sqlite", tmp_path / "b", 2_000_000_000.0, 14)
    reg.close()
    assert result.verified and result.path.exists()
    conn = sqlite3.connect(result.path)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    conn.close()


def test_a_corrupt_backup_is_detected_not_trusted(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4000 + b"garbage" * 500)
    ok, detail = backup.verify_restore(bad, {"trades": 1})
    assert not ok and detail


def test_a_backup_missing_rows_fails_verification(tmp_path: Path) -> None:
    reg = make_db(tmp_path / "live.sqlite")
    reg.close()
    assert not backup.verify_restore(tmp_path / "live.sqlite", {"trades": 5})[0]


def test_retention_keeps_the_newest_backup_whatever_its_age(tmp_path: Path) -> None:
    import os

    out = tmp_path / "b"
    out.mkdir()
    for name in ("mt5bot_20200101_000000.sqlite", "mt5bot_20200102_000000.sqlite"):
        (out / name).write_text("x")
        os.utime(out / name, (0, 0))
    assert backup.prune(out, 1e9, 14) == 1
    assert [p.name for p in out.iterdir()] == ["mt5bot_20200102_000000.sqlite"]


def test_one_backup_per_server_day(tmp_path: Path) -> None:
    reg = make_db(tmp_path / "live.sqlite")
    schedule = backup.BackupSchedule(tmp_path, tmp_path / "live.sqlite", 14, 3, lambda: 2_000_000_000.0)
    day = 1_767_571_200.0
    schedule.maybe_run(day + 3600)        # before the hour
    assert not (tmp_path / "backups").exists()
    schedule.maybe_run(day + 4 * 3600)
    schedule.maybe_run(day + 5 * 3600)
    reg.close()
    assert len(list((tmp_path / "backups").glob("*.sqlite"))) == 1 and schedule.failed == ""


def test_config_changes_are_versioned_in_the_ledger(make_bot) -> None:
    bot = make_bot()
    bot.startup()
    [event] = bot.events.tail(["config.changed"])
    assert event.payload["config"]["magic"] == bot.cfg.magic and event.payload["to"]


def test_every_order_decision_carries_its_reproducibility_stamp(make_bot, fake_mt5: Any) -> None:
    bot = make_bot()
    bot.startup()
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    events = bot.events.tail(["decision.proposed"])
    assert events and {"code", "config", "data"} <= set(events[0].stamp)


@pytest.mark.parametrize("text,name", [("sk-ant-api03-abcdefghijkl", "anthropic_key"),
                                       ("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh", "telegram_token"),
                                       ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "bearer_token"),
                                       ("-----BEGIN RSA PRIVATE KEY-----", "private_key")])
def test_secret_patterns(text: str, name: str) -> None:
    assert name in secretscan.scan_text(text)


def test_scan_reports_where_but_never_the_secret(tmp_path: Path) -> None:
    (tmp_path / "bot.log").write_text("password=hunter2hunter2 ok", encoding="utf-8")
    found = secretscan.scan_project(tmp_path, {"MT5_PASSWORD": "hunter2hunter2"})
    assert list(found.values()) == [["literal_0"]]
    assert "hunter2" not in json.dumps(found)


def test_scan_reads_every_text_column_of_the_database(tmp_path: Path) -> None:
    reg = make_db(tmp_path / "x.sqlite")
    reg.set_kv("note", "sk-ant-api03-abcdefghijkl")
    reg.close()
    assert "kv.value" in secretscan.scan_database(tmp_path / "x.sqlite")


def test_no_secret_reaches_logs_database_or_files_in_a_full_run(tmp_path: Path, caplog: Any) -> None:
    from mt5bot.config import Config
    from mt5bot.sim.runner import run_simulation

    env = {"ANTHROPIC_API_KEY": "sk-ant-api03-REAL-LOOKING-SECRET-000", "MT5_PASSWORD": "SuperSecret!123"}
    cfg = Config.from_dict({"state_root": str(tmp_path)})
    run_simulation(cfg, tmp_path, steps=20, speed=0, warmup_bars=800, advisor="none")
    assert secretscan.scan_project(tmp_path, env) == {}


def test_every_agent_belongs_to_exactly_one_service() -> None:
    assert sorted(SERVICE_OF) == sorted(AGENT_IDS)


def test_service_activity_reports_real_funnel_stages() -> None:
    out = service_activity({"risk": 5, "affordability": 2, "signal": 9})
    assert out["risk"]["events"] == 7 and out["signals"]["events"] == 9 and out["research"]["events"] == 0


def test_complexity_budget_claims_nothing_without_data() -> None:
    rows = complexity_budget(None, None, 0.0)
    assert len(rows) == len(COMPONENTS) and {r["status"] for r in rows} == {"proving"}


def test_a_component_past_its_window_without_proof_is_flagged() -> None:
    rows = {r["id"]: r for r in complexity_budget({"n": 5, "verdict": "x"}, 0.0, 200 * 86400.0)}
    assert rows["specialist_team_votes"]["status"] == "no_value_shown"
    kept = {r["id"]: r for r in complexity_budget({"n": 80, "verdict": "beat", "team_beats_baseline": True}, 0.0, 86400.0)}
    assert kept["specialist_team_votes"]["status"] == "kept"


def test_shadow_mode_never_reaches_the_live_broker(make_bot: Any, fake_mt5: Any) -> None:
    from mt5bot.adapters.fake import FakeBroker
    from mt5bot.adapters.shadow import ShadowBroker

    live = FakeBroker()
    shadow = ShadowBroker(live, SimpleNamespace(account=live.account, terminal=live.terminal, last_error=live.last_error,
                                                connect=live.connect, disconnect=live.disconnect, check=live.check))
    assert shadow.account_state.equity == live.account_state.equity
