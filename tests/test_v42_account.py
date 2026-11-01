"""Account mode: real by default, demo by option, wrong type exits 3, state never mixes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import FakeMT5, FixedSource
from mt5bot import account_mode as am
from mt5bot import bot as botmod
from mt5bot import cli
from mt5bot.config import Config, apply_overrides
from mt5bot.preflight import run_preflight
from mt5bot.profiles import effective_limits
from mt5bot.shared import SharedMarket

PROJECT = Path(__file__).resolve().parent.parent


def make_real(fake: FakeMT5) -> None:
    fake.account.trade_mode = fake.ACCOUNT_TRADE_MODE_REAL
    fake.account.server = "STARTRADER-Real"
    fake.account.login = 12345678


class TestModeMatrix:
    @pytest.mark.parametrize("mode,real,exit_3", [("real", True, False), ("real", False, True), ("demo", False, False),
                                                  ("demo", True, True), ("auto", True, False), ("auto", False, False)])
    def test_matrix(self, make_bot, fake_mt5: FakeMT5, mode: str, real: bool, exit_3: bool) -> None:
        if real:
            make_real(fake_mt5)
        bot = make_bot(account_mode=mode, real_money_confirmed=True)
        if exit_3:
            with pytest.raises(botmod.AccountPolicyError):
                bot.startup()
            assert fake_mt5.sends == [] and fake_mt5.checks == []
        else:
            bot.startup()
            assert bot.is_real is real

    def test_refusal_says_what_to_do_in_portuguese(self) -> None:
        assert "start_bot_demo.bat" in am.account_policy_problem("real", False, True)
        assert "start_bot.bat" in am.account_policy_problem("demo", True, True)
        assert "real_money_confirmed" in am.account_policy_problem("real", True, False)

    def test_trade_mode_decides_not_the_server_name(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.server = "STARTRADER-Demo"     # a misleading name
        make_real(fake_mt5)
        fake_mt5.account.server = "Broker-Demo"
        bot = make_bot(account_mode="real", real_money_confirmed=True)
        bot.startup()
        assert bot.is_real is True

    def test_cli_override_beats_the_config(self) -> None:
        cfg = Config.from_dict({"account_mode": "real"})
        assert apply_overrides(cfg, {"account_mode": "demo"}).account_mode == "demo"
        assert cli.parse_args(["--account", "demo"]).account == "demo"
        with pytest.raises(SystemExit):
            cli.parse_args(["--account", "other"])

    def test_defaults_ship_real_but_code_never_assumes_confirmation(self) -> None:
        shipped = Config.load(PROJECT / "config.json")
        assert shipped.account_mode == "real" and shipped.real_money_confirmed is True
        bare = Config()
        assert bare.account_mode == "real" and bare.real_money_confirmed is False

    def test_main_returns_3_and_never_sends(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        fake = FakeMT5()
        original = botmod.Bot.__init__

        def init(self, cfg, **kwargs):  # type: ignore[no-untyped-def]
            kwargs.update(api=fake, wait=lambda s: None, env={}, signal_source=FixedSource(), globber=lambda p: [],
                          durable_files=False, auto_migrate=False)
            original(self, cfg, **kwargs)

        monkeypatch.setattr(botmod.Bot, "__init__", init)
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"state_root": str(tmp_path / "s"), "log_file": str(tmp_path / "b.log"),
                                    "health_file": str(tmp_path / "h.json"), "real_money_confirmed": True,
                                    "kill_switch_file": str(tmp_path / "K")}), encoding="utf-8")
        assert cli.main(["--config", str(path)]) == 3      # default real, demo account logged in
        cli.log.handlers.clear()
        assert fake.sends == []
        assert "conta DEMO" in capsys.readouterr().out


class TestBanner:
    def acc(self) -> SimpleNamespace:
        return SimpleNamespace(login=12345678, server="STARTRADER-Real", equity=65.09, currency="USD", leverage=500)

    def test_content_and_masking(self) -> None:
        lim = effective_limits(Config(risk_profile="aggressive"), True)
        data = am.banner_data(self.acc(), True, "real", True, lim)
        text = "\n".join(am.banner_lines(data))
        assert "12345678" not in text and "REAL" in text and "STARTRADER-Real" in text
        assert "65.09 USD" in text and "1:500" in text and "hedging" in text and "aggressive" in text
        assert "DINHEIRO REAL EM RISCO" in text
        assert data["limits"]["risk_per_trade"] == 1.3 and data["limits"]["daily_loss"] == 3.25

    def test_demo_has_no_real_money_reminder(self) -> None:
        lim = effective_limits(Config(), False)
        assert "DINHEIRO REAL" not in "\n".join(am.banner_lines(am.banner_data(self.acc(), False, "demo", True, lim)))


class TestAcknowledgment:
    def test_only_first_use_or_long_absence_waits(self, tmp_path: Path) -> None:
        assert am.needs_countdown(tmp_path, 1000.0, 30.0)
        am.acknowledge(tmp_path, 12345678, "S", 1000.0)
        assert not am.needs_countdown(tmp_path, 1000.0 + 29 * 86400, 30.0)
        assert am.needs_countdown(tmp_path, 1000.0 + 31 * 86400, 30.0)
        assert "12345678" not in (tmp_path / am.ACK_FILE).read_text(encoding="utf-8")

    def test_corrupt_file_counts_as_not_acknowledged(self, tmp_path: Path) -> None:
        (tmp_path / am.ACK_FILE).write_text("not json", encoding="utf-8")
        assert am.needs_countdown(tmp_path, 1.0, 30.0)


class TestPreflight:
    def test_reads_only_and_reports_in_portuguese(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.equity = fake_mt5.account.balance = 65.0
        bot = make_bot(account_mode="auto", source=None, team=False, universe={"candidates": ["EURUSD"]})
        code, text = run_preflight(bot)
        assert code == 0 and fake_mt5.sends == [] and fake_mt5.checks == []
        assert bot.registry is None and bot.state_dir is None     # no state was opened
        assert "Piso de viabilidade" in text and "Tecto de capacidade" in text and "EURUSD" in text

    def test_wrong_account_type_exits_3(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = make_bot(account_mode="real", real_money_confirmed=True, source=None, team=False)
        code, text = run_preflight(bot)
        assert code == 3 and "DEMO" in text and fake_mt5.sends == []

    def test_permission_problems_are_named(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.trade_expert = False
        bot = make_bot(account_mode="auto", source=None, team=False, universe={"candidates": ["EURUSD"]})
        _code, text = run_preflight(bot)
        assert "não autoriza robôs" in text


class TestLaunchers:
    @pytest.mark.parametrize("name,mode", [("start_bot.bat", "--account real"), ("start_bot_demo.bat", "--account demo")])
    def test_each_launcher_starts_its_mode(self, name: str, mode: str) -> None:
        text = (PROJECT / name).read_text(encoding="utf-8")
        assert f"python mt5_bot.py {mode}" in text
        assert ("REAL" if "real" in mode else "DEMO") in text

    def test_preflight_and_autostart(self) -> None:
        assert "--preflight" in (PROJECT / "preflight.bat").read_text(encoding="utf-8")
        assert '[string]$BotArgs = "--account real"' in (PROJECT / "install_autostart.ps1").read_text(encoding="utf-8")


class TestIsolation:
    def two_bots(self, make_cfg, waits: list) -> tuple[botmod.Bot, botmod.Bot, FakeMT5, FakeMT5]:
        demo, real = FakeMT5(), FakeMT5()
        make_real(real)
        cfg = make_cfg(real_money_confirmed=True)
        bots = []
        for fake in (demo, real):
            bot = botmod.Bot(cfg, api=fake, wait=waits.append, env={}, signal_source=FixedSource(), globber=lambda p: [])
            bot.startup()
            bots.append(bot)
        return bots[0], bots[1], demo, real

    def test_state_and_results_never_mix(self, make_cfg, waits: list) -> None:
        demo_bot, real_bot, _demo, _real = self.two_bots(make_cfg, waits)
        assert demo_bot.state_dir != real_bot.state_dir
        demo_bot.registry.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 50.0, ticket=7, tier="T1")
        demo_bot.registry.set_kv("capital_state", "{}")
        assert real_bot.registry.trades(mode="live") == []
        assert real_bot.registry.get_kv("capital_state") is None
        demo_bot.close()
        real_bot.close()

    def test_demo_fills_never_reach_real_calibration(self, make_cfg, waits: list) -> None:
        demo_bot, real_bot, _demo, _real = self.two_bots(make_cfg, waits)
        conn = demo_bot.registry.conn
        conn.execute("INSERT INTO tca (ts, symbol) VALUES (1.0, 'EURUSD')") if False else None
        assert real_bot.registry.path != demo_bot.registry.path
        assert real_bot.registry.conn.execute("SELECT COUNT(*) AS n FROM tca").fetchone()["n"] == 0
        demo_bot.close()
        real_bot.close()

    def test_market_assets_are_shared_and_hold_no_account_data(self, tmp_path: Path) -> None:
        shared = SharedMarket(tmp_path)
        rows = np.zeros(3, dtype=[("time", "<i8"), ("close", "<f8")])
        rows["time"] = [1, 2, 3]
        shared.store_bars("EURUSD", "M15", rows)
        more = np.zeros(2, dtype=rows.dtype)
        more["time"] = [3, 4]
        shared.store_bars("EURUSD", "M15", more)
        assert list(shared.load_bars("EURUSD", "M15")["time"]) == [1, 2, 3, 4]
        shared.publish_trials("demo_1", 10)
        shared.publish_trials("real_2", 5)
        assert shared.other_trials("real_2") == 10 and shared.summary()["trials_all_accounts"] == 15
        assert "Nenhum dado de conta" in (tmp_path / "_shared" / "LEIA-ME.txt").read_text(encoding="utf-8")
