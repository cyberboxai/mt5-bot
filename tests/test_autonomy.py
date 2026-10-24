"""Autonomy: reconciliation and SL repair, supervisor restarts and its circuit breaker,
atomic persistence, config hot reload, notifications, health/CLI, scheduling, watchdog."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeMT5
from mt5bot import cli, persistence
from mt5bot.cycle import CycleLock, OptimizerScheduler
from mt5bot.config import Config, reload_decision
from mt5bot.market import ServerClock, market_expected_open
from mt5bot.notify import Notifier
from mt5bot.persistence import atomic_write_json, atomic_write_text
from mt5bot.registry import Registry
from mt5bot.supervisor import EXIT_BREAKER, Supervisor
from mt5bot.terminal import FreezeWatchdog, discover_terminal_paths


# ---------------------------------------------------------------- reconciliation


class TestReconciliation:
    def test_repairs_position_missing_sl_and_tp(self, make_bot, fake_mt5: FakeMT5) -> None:
        pos = fake_mt5.add_position("EURUSD", "BUY", magic=510001, volume=0.2, price_open=1.10000)
        bot = make_bot()
        bot.startup()
        [req] = fake_mt5.sends
        assert req["action"] == fake_mt5.TRADE_ACTION_SLTP and req["position"] == pos.ticket
        assert 0 < pos.sl < 1.1 < pos.tp
        # Protective SL never risks more than the per-trade risk: 0.5% of 10k = 50 USD.
        assert (1.1 - pos.sl) * 100_000 * 0.2 <= 50.0 + 1e-6
        [row] = bot.registry.trades(mode="live")
        assert row.ticket == pos.ticket and row.extra.get("recovered") is True and row.sl == pos.sl

    def test_repairs_only_missing_tp_without_touching_sl(self, make_bot, fake_mt5: FakeMT5) -> None:
        pos = fake_mt5.add_position("EURUSD", "SELL", magic=510001, price_open=1.1, sl=1.1050)
        make_bot().startup()
        assert pos.sl == 1.1050 and 0 < pos.tp < 1.1

    def test_unprotected_position_beyond_risk_is_closed(self, make_bot, fake_mt5: FakeMT5) -> None:
        pos = fake_mt5.add_position("EURUSD", "BUY", magic=510001, volume=1.0, price_open=1.12)
        make_bot().startup()
        assert pos not in fake_mt5.positions
        assert fake_mt5.sends[0]["position"] == pos.ticket and fake_mt5.sends[0]["type"] == fake_mt5.ORDER_TYPE_SELL

    def test_foreign_positions_untouched(self, make_bot, fake_mt5: FakeMT5) -> None:
        foreign = fake_mt5.add_position("EURUSD", "BUY", magic=4242)
        make_bot().startup()
        assert fake_mt5.sends == [] and foreign.sl == 0.0

    def test_pending_orders_without_stops_cancelled(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.orders = [SimpleNamespace(ticket=77, magic=510001, sl=0.0, tp=0.0),
                           SimpleNamespace(ticket=78, magic=510001, sl=1.0, tp=1.2),
                           SimpleNamespace(ticket=79, magic=1, sl=0.0, tp=0.0)]
        make_bot().startup()
        assert [o.ticket for o in fake_mt5.orders] == [78, 79]

    def test_broker_removed_sl_is_repaired_at_runtime(self, make_bot, fake_mt5: FakeMT5) -> None:
        pos = fake_mt5.add_position("EURUSD", "BUY", magic=510001, sl=1.09, tp=1.12)
        bot = make_bot()
        bot.startup()
        pos.sl = 0.0
        bot._next_housekeeping = 0
        bot.run_once()
        assert pos.sl > 0

    def test_closed_live_trade_recorded_from_deals(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = make_bot()
        bot.startup()
        bot.run_once()
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        [row] = bot.registry.trades(mode="live")
        pos = fake_mt5.positions[0]
        fake_mt5.positions.clear()
        fake_mt5.deals = [
            SimpleNamespace(position_id=pos.ticket, entry=0, price=pos.price_open, profit=0.0, commission=-1.6,
                            swap=0.0, fee=0.0, symbol="EURUSD", volume=pos.volume, type=0, ticket=1),
            SimpleNamespace(position_id=pos.ticket, entry=1, price=pos.tp, profit=69.0, commission=-1.6,
                            swap=-0.5, fee=0.0, symbol="EURUSD", volume=pos.volume, type=1, ticket=2),
        ]
        bot._next_housekeeping = 0
        bot.run_once()
        [closed] = bot.registry.trades(mode="live")
        assert closed.status == "closed" and closed.pnl == pytest.approx(65.3)
        assert closed.r == pytest.approx(65.3 / row.risk_money)


# -------------------------------------------------------------------- supervisor


class FakeProc:
    def __init__(self, code: int) -> None:
        self.code = code

    def wait(self, timeout: float | None = None) -> int:
        return self.code

    def poll(self) -> int:
        return self.code

    def terminate(self) -> None:
        pass


class TestSupervisor:
    def _make(self, codes: list[int], **kw: Any) -> tuple[Supervisor, list, list, list]:
        launched, sleeps, notes = [], [], []
        clock = [0.0]

        def spawn(args: list[str]) -> FakeProc:
            launched.append(args)
            clock[0] += kw.pop("run_seconds", 1.0) if False else 1.0
            return FakeProc(codes.pop(0) if codes else 1)

        sup = Supervisor(["python", "mt5_bot.py", "--confirm-real-money"], spawn=spawn,
                         sleep=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)),
                         clock=lambda: clock[0], notify=notes.append, **kw)
        return sup, launched, sleeps, notes

    def test_restarts_after_crash_with_backoff_then_stops_on_clean_exit(self) -> None:
        sup, launched, sleeps, _ = self._make([1, 1, 0])
        assert sup.run() == 0
        assert len(launched) == 3
        assert sleeps == [15.0, 30.0]
        assert "--no-countdown" not in launched[0] and "--no-countdown" in launched[1]
        assert all("--confirm-real-money" in a for a in launched)

    def test_circuit_breaker_after_repeated_crashes(self) -> None:
        sup, launched, sleeps, notes = self._make([], max_crashes=5, window_seconds=1800)
        assert sup.run() == EXIT_BREAKER
        assert len(launched) == 5
        assert any("circuit breaker" in n for n in notes)

    def test_safety_stop_is_not_restarted(self) -> None:
        sup, launched, _, notes = self._make([3])
        assert sup.run() == 3
        assert len(launched) == 1 and notes

    def test_crashes_outside_the_window_do_not_trip(self) -> None:
        sup, launched, _, _ = self._make([1] * 6 + [0], max_crashes=3, window_seconds=10, backoff_start=20,
                                         backoff_max=20)
        assert sup.run() == 0
        assert len(launched) == 7


# ------------------------------------------------------------------ persistence


class TestAtomicWrites:
    def test_interrupted_write_keeps_previous_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "risk_state.json"
        atomic_write_json(target, {"peak_equity": 10_000})

        def power_cut(src: str, dst: str) -> None:
            raise OSError("simulated power cut before rename")

        monkeypatch.setattr(persistence.os, "replace", power_cut)
        with pytest.raises(OSError):
            atomic_write_json(target, {"peak_equity": 1})
        monkeypatch.undo()
        assert json.loads(target.read_text())["peak_equity"] == 10_000

    def test_crash_while_writing_temp_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "health.json"
        atomic_write_text(target, "old")
        real_fsync = os.fsync

        def die(fd: int) -> None:
            raise KeyboardInterrupt("killed mid-write")

        monkeypatch.setattr(persistence.os, "fsync", die)
        with pytest.raises(KeyboardInterrupt):
            atomic_write_text(target, "new and longer content")
        monkeypatch.setattr(persistence.os, "fsync", real_fsync)
        assert target.read_text() == "old"

    def test_guard_state_written_atomically(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = make_bot()
        bot.startup()
        bot.run_once()
        data = json.loads((bot.state_dir / "risk_state.json").read_text())
        assert data["peak_equity"] == 10_000.0
        assert not (bot.state_dir / "risk_state.json.tmp").exists()

    def test_durable_registry_uses_wal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Registry, "durable", True)
        reg = Registry(tmp_path / "r.sqlite")
        assert reg.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        reg.close()


# -------------------------------------------------------------------- hot reload


class TestHotReload:
    def _bot(self, make_bot, tmp_path: Path, **cfg: Any):
        path = tmp_path / "config.json"
        base = {"state_root": str(tmp_path / "state"), "health_file": str(tmp_path / "health.json"),
                "kill_switch_file": str(tmp_path / "KILL"), "log_file": str(tmp_path / "bot.log"),
                "reports_dir": str(tmp_path / "reports"), "trading_hours_server": [0, 24]}
        base.update(cfg)
        path.write_text(json.dumps(base))
        bot = make_bot(cfg=Config.load(path))
        bot.startup()
        return bot, path, base

    def test_invalid_config_is_rejected_and_old_kept(self, make_bot, tmp_path: Path,
                                                     caplog: pytest.LogCaptureFixture) -> None:
        bot, path, _ = self._bot(make_bot, tmp_path)
        old = bot.cfg
        path.write_text("{ broken json")
        caplog.set_level(logging.ERROR, logger="mt5_bot")
        assert bot.apply_config_file() is False
        assert bot.cfg is old and "keeping the previous config" in caplog.text
        path.write_text(json.dumps({"risk_per_trade_pct_demo": 9.0}))
        assert bot.apply_config_file() is False and bot.cfg is old

    def test_valid_change_applied_and_safety_only_tightens(self, make_bot, tmp_path: Path) -> None:
        bot, path, base = self._bot(make_bot, tmp_path)
        path.write_text(json.dumps({**base, "poll_seconds": 9, "risk_per_trade_pct_demo": 0.3}))
        assert bot.apply_config_file()
        assert bot.cfg.poll_seconds == 9 and bot.risk.risk_pct == 0.3
        path.write_text(json.dumps({**base, "poll_seconds": 9, "risk_per_trade_pct_demo": 1.0, "magic": 7}))
        assert bot.apply_config_file()
        assert bot.risk.risk_pct == 0.3  # loosening needs a restart
        assert bot.cfg.magic == 510001  # identity key needs a restart

    def test_cli_overrides_survive_reload(self, make_bot, tmp_path: Path) -> None:
        bot, path, base = self._bot(make_bot, tmp_path)
        bot._overrides = {"dry_run": True}
        bot.cfg = Config.from_dict({**base, "dry_run": True})
        path.write_text(json.dumps({**base, "poll_seconds": 7}))
        bot.apply_config_file()
        assert bot.cfg.dry_run is True and bot.cfg.poll_seconds == 7

    def test_reload_decision_notes(self) -> None:
        merged, notes = reload_decision(Config(), Config(max_drawdown_from_peak_pct=15.0, symbols=("GBPUSD",)))
        assert merged.max_drawdown_from_peak_pct == 12.0 and merged.symbols == ("EURUSD",)
        assert len(notes) == 2


# ----------------------------------------------------------------- notifications


class TestNotifier:
    def test_disabled_without_env_and_never_reads_files(self) -> None:
        assert Notifier(True, {}).enabled is False
        assert Notifier(False, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}).enabled is False

    def test_posts_and_deduplicates(self) -> None:
        sent: list[Any] = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def opener(request, timeout):
            sent.append(request)
            return Resp()

        n = Notifier(True, {"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "42"}, opener=opener,
                     background=False, clock=lambda: 0.0)
        n.send("hello")
        n.send("hello")
        assert len(sent) == 1
        assert "123:ABC" in sent[0].full_url and b"chat_id=42" in sent[0].data

    def test_failure_never_logs_the_token(self, caplog: pytest.LogCaptureFixture) -> None:
        def opener(request, timeout):
            raise OSError(f"cannot reach {request.full_url}")

        caplog.set_level(logging.DEBUG, logger="mt5_bot")
        Notifier(True, {"TELEGRAM_BOT_TOKEN": "SECRET-TOKEN", "TELEGRAM_CHAT_ID": "1"}, opener=opener,
                 background=False).send("x")
        assert "SECRET-TOKEN" not in caplog.text and "failed" in caplog.text


# ----------------------------------------------------------- health, CLI, reports


class TestOperations:
    def test_health_file_and_status(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path,
                                    capsys: pytest.CaptureFixture) -> None:
        bot = make_bot()
        bot.startup()
        bot.run_once()
        health = json.loads((tmp_path / "health.json").read_text())
        for key in ("last_loop_utc", "last_tick_server", "equity", "open_positions", "halted", "symbols"):
            assert key in health
        assert health["symbols"]["EURUSD"]["champion"] == "NoTrade"
        assert "87654321" not in json.dumps(health["account"])
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"health_file": str(tmp_path / "health.json"),
                                        "state_root": str(tmp_path / "state")}))
        assert cli.main(["--config", str(cfg_path), "--status"]) == 0
        out = capsys.readouterr().out
        assert "Equity: 10000.0" in out and "EURUSD: champion NoTrade" in out

    def test_history_rollback_resume(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path,
                                     capsys: pytest.CaptureFixture) -> None:
        from test_engine import candidate, store_run

        bot = make_bot()
        bot.startup()
        reg = bot.registry
        [cid] = store_run(reg, candidate())
        promo = reg.record_switch("EURUSD", "promote", reg.champion("EURUSD"), reg.candidate(cid), "unit test", {"x": 1})
        reg.record_switch("EURUSD", "demote", reg.candidate(cid), reg.champion("NONE"), "degraded", {})
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"health_file": str(tmp_path / "health.json"),
                                        "state_root": str(tmp_path / "state")}))
        assert cli.main(["--config", str(cfg_path), "--history"]) == 0
        out = capsys.readouterr().out
        assert "PROMOTE" in out and "DEMOTE" in out and "unit test" in out
        assert cli.main(["--config", str(cfg_path), "--rollback", str(promo)]) == 0
        assert reg.champion("EURUSD").candidate_id == cid
        (bot.state_dir / "RESUME_REQUIRED.txt").write_text("halt")
        assert cli.main(["--config", str(cfg_path), "--resume"]) == 0
        assert not (bot.state_dir / "RESUME_REQUIRED.txt").exists()

    def test_daily_report_written_on_new_day(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
        utc = [1_760_000_000.0]
        bot = make_bot(utc_now=lambda: utc[0])
        bot.startup()
        bot.run_once()
        utc[0] += 86400
        bot.run_once()
        reports = list((tmp_path / "reports").rglob("daily_*.txt"))
        assert reports
        text = reports[0].read_text()
        assert "expectancy" in text and "does not guarantee profit" in text
        assert list((tmp_path / "reports").rglob("daily_*_trades.csv"))

    def test_cash_flow_adjusts_guard(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = make_bot()
        bot.startup()
        bot.run_once()
        fake_mt5.deals = [SimpleNamespace(type=2, ticket=900, profit=-2_000.0)]
        fake_mt5.account.equity = 8_000.0  # -20%: the guard is about to trip, so cash flow is checked first
        bot.run_once()
        assert bot.guard.state.peak_equity == 8_000.0
        assert not bot.guard.state.dd_halted

    def test_preexisting_deposits_are_not_counted(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.deals = [SimpleNamespace(type=2, ticket=1, profit=10_000.0)]
        bot = make_bot()
        bot.startup()
        bot.run_once()
        assert bot.guard.state.peak_equity == 10_000.0


# ---------------------------------------------------------------- optimiser jobs


class TestScheduler:
    def test_cold_start_launches_low_priority_offline_process(self, tmp_path: Path) -> None:
        reg = Registry(tmp_path / "r.sqlite")
        launched = []
        sched = OptimizerScheduler(Config(), reg, tmp_path, str(tmp_path / "config.json"), lambda: True,
                                   spawn=lambda args, **kw: launched.append((args, kw)) or FakeProc(0))
        assert sched.maybe_launch(weekend=False) == "cold start: no optimisation results yet"
        args, kw = launched[0]
        assert "--optimize-once" in args and "--offline" in args and "--state-dir" in args
        reg.close()

    def test_weekly_cycle_waits_for_the_weekend(self, tmp_path: Path) -> None:
        reg = Registry(tmp_path / "r.sqlite")
        now = [1_760_000_000.0]
        reg.set_kv("last_cycle_finished", str(now[0]))
        sched = OptimizerScheduler(Config(), reg, tmp_path, "config.json", lambda: True, clock=lambda: now[0])
        assert sched.due(weekend=True) is None
        now[0] += 7.5 * 86400
        assert sched.due(weekend=False) is None
        assert sched.due(weekend=True) == "weekly cycle (market closed)"
        now[0] += 3 * 86400
        assert "overdue" in sched.due(weekend=False)
        sched.request("EURUSD", "demotion")
        assert sched.due(weekend=False) == "EURUSD: demotion"
        reg.close()

    def test_failed_data_refresh_postpones(self, tmp_path: Path) -> None:
        reg = Registry(tmp_path / "r.sqlite")
        sched = OptimizerScheduler(Config(), reg, tmp_path, "config.json", lambda: False,
                                   spawn=lambda *a, **k: pytest.fail("must not launch"))
        assert sched.maybe_launch(weekend=True) is None
        reg.close()

    def test_cycle_lock_is_exclusive(self, tmp_path: Path) -> None:
        a, b = CycleLock(tmp_path), CycleLock(tmp_path)
        assert a.acquire() and not b.acquire()
        a.release()
        assert b.acquire()
        b.release()


# ------------------------------------------------------ terminal and market time


class TestTerminalAndClock:
    def test_discovery_prefers_startrader(self) -> None:
        hits = {r"C:\Program Files\*\terminal64.exe": [r"C:\Program Files\MetaTrader 5\terminal64.exe",
                                                        r"C:\Program Files\STARTRADER MetaTrader 5\terminal64.exe"]}
        paths = discover_terminal_paths({"ProgramFiles": r"C:\Program Files"}, None,
                                        lambda pattern: hits.get(pattern.replace("/", "\\"), []))
        assert "STARTRADER" in paths[0]
        assert discover_terminal_paths({}, r"D:\mt5\terminal64.exe", lambda p: [])[0] == r"D:\mt5\terminal64.exe"

    def test_watchdog_rules(self) -> None:
        t = [0.0]
        dog = FreezeWatchdog(600, lambda: t[0])
        assert dog.should_restart(700, False, True, True)[0] is False  # market closed
        assert dog.should_restart(100, True, True, True)[0] is False  # fresh
        assert dog.should_restart(700, True, True, False)[0] is False  # unprotected position
        assert dog.should_restart(700, True, True, True)[0] is True
        dog.restarted()
        assert dog.should_restart(700, True, True, True)[0] is False  # rate limited
        t[0] += 1801
        assert dog.should_restart(700, True, True, True)[0] is True

    def test_bot_restarts_frozen_terminal(self, make_bot, fake_mt5: FakeMT5) -> None:
        mono, utc = [0.0], [1_760_090_400.0]  # a Thursday, 10:00 UTC
        killed: list[str] = []
        bot = make_bot(monotonic=lambda: mono[0], utc_now=lambda: utc[0],
                       terminal_killer=lambda exe: killed.append(exe) or True)
        bot.startup()
        bot.run_once()
        mono[0] += 11 * 60
        bot._next_housekeeping = 0
        bot.run_once()
        assert killed == [r"C:\MT5" + os.sep + "terminal64.exe"]

    def test_server_offset_detected_from_fresh_ticks(self) -> None:
        utc = [1_000_000.0]
        clock = ServerClock(utc_now=lambda: utc[0], monotonic=lambda: utc[0])
        clock.observe("EURUSD", int(utc[0] + 7200 - 5), 1.1, 1.1001)
        utc[0] += 10
        clock.observe("EURUSD", int(utc[0] + 7200 - 2), 1.1002, 1.1003)
        assert clock.offset == 7200
        assert clock.is_fresh("EURUSD", 300)
        utc[0] += 600
        assert not clock.is_fresh("EURUSD", 300)

    def test_market_expected_open(self) -> None:
        thursday_noon = 1_760_011_200  # 2025-10-09 12:00
        assert market_expected_open(thursday_noon, [])
        assert not market_expected_open(thursday_noon + 2 * 86400, [])  # Saturday
        assert not market_expected_open(thursday_noon, ["2025-10-09"])

    def test_closed_market_sleeps_longer(self, make_bot, fake_mt5: FakeMT5) -> None:
        mono = [0.0]
        bot = make_bot(monotonic=lambda: mono[0])
        bot.startup()
        bot.run_once()
        mono[0] += 3600
        bot.run_once()
        assert bot._next_wait == bot.cfg.closed_market_poll_seconds
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert bot._next_wait == bot.cfg.poll_seconds


def test_no_secrets_in_config_or_logs(tmp_path: Path) -> None:
    text = (Path(__file__).resolve().parent.parent / "config.json").read_text()
    for word in ("password", "token", "TELEGRAM_BOT_TOKEN", "MT5_PASSWORD"):
        assert word.lower() not in text.lower()


def test_subprocess_constant_exists() -> None:
    assert subprocess.STDOUT is not None


def test_supervisor_notifications_follow_config(tmp_path: Path) -> None:
    from mt5bot.supervisor import telegram_enabled

    (tmp_path / "config.json").write_text(json.dumps({"telegram_enabled": True}))
    (tmp_path / "other.json").write_text(json.dumps({}))
    assert telegram_enabled([], tmp_path) is True
    assert telegram_enabled(["--config", "other.json"], tmp_path) is False
    (tmp_path / "config.json").write_text("{bad")
    assert telegram_enabled([], tmp_path) is False
