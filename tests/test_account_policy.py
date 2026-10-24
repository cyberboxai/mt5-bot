"""Account policy: any / demo_only / real_only, the one-time real-money confirmation,
real-account risk defaults and per-account state separation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeMT5, FixedSource
from mt5bot import bot as botmod
from mt5bot import cli
from mt5bot.bot import AccountPolicyError, account_policy_problem
from mt5bot.persistence import account_key


def make_real(fake: FakeMT5) -> None:
    fake.account.trade_mode = fake.ACCOUNT_TRADE_MODE_REAL
    fake.account.server = "STARTRADER-Real"
    fake.account.login = 12345678


@pytest.mark.parametrize("mode,is_real,confirmed,allowed", [
    ("any", False, False, True),
    ("any", True, False, False),
    ("any", True, True, True),
    ("demo_only", False, False, True),
    ("demo_only", True, True, False),
    ("real_only", True, True, True),
    ("real_only", True, False, False),
    ("real_only", False, True, False),
])
def test_policy_matrix(mode: str, is_real: bool, confirmed: bool, allowed: bool) -> None:
    assert (account_policy_problem(mode, is_real, confirmed) is None) is allowed


def test_demo_account_runs_without_confirmation(make_bot) -> None:
    bot = make_bot()
    bot.startup()
    assert bot.is_real is False


def test_real_without_confirmation_is_refused_with_instructions(make_bot, fake_mt5: FakeMT5) -> None:
    make_real(fake_mt5)
    with pytest.raises(AccountPolicyError, match="--confirm-real-money"):
        make_bot().startup()
    assert fake_mt5.sends == [] and fake_mt5.checks == []


def test_real_with_cli_confirmation_runs(make_bot, fake_mt5: FakeMT5) -> None:
    make_real(fake_mt5)
    bot = make_bot(confirm_real_money=True)
    bot.startup()
    assert bot.is_real is True


def test_real_with_config_confirmation_runs(make_bot, fake_mt5: FakeMT5) -> None:
    make_real(fake_mt5)
    bot = make_bot(real_money_confirmed=True)
    bot.startup()
    assert bot.is_real


@pytest.mark.parametrize("mode,real", [("demo_only", True), ("real_only", False)])
def test_wrong_account_type_is_refused(make_bot, fake_mt5: FakeMT5, mode: str, real: bool) -> None:
    if real:
        make_real(fake_mt5)
    with pytest.raises(AccountPolicyError):
        make_bot(account_mode=mode, confirm_real_money=True).startup()


def test_real_account_uses_stricter_risk(make_bot, fake_mt5: FakeMT5) -> None:
    make_real(fake_mt5)
    bot = make_bot(confirm_real_money=True)
    bot.startup()
    assert bot.risk.risk_pct == 0.25 and bot.risk.max_daily_loss_pct == 1.5
    demo = FakeMT5()
    assert demo.account.trade_mode == demo.ACCOUNT_TRADE_MODE_DEMO


def test_main_exits_3_for_unconfirmed_real_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMT5()
    make_real(fake)
    monkeypatch.setattr(botmod.Bot, "__init__", _init_with(fake, botmod.Bot.__init__))
    cfg_path = _write_cfg(tmp_path)
    try:
        assert cli.main(["--config", str(cfg_path)]) == botmod.EXIT_SAFETY_STOP
    finally:
        _close_logs()
    assert "REAL MONEY" in (tmp_path / "bot.log").read_text(encoding="utf-8")


def test_main_with_confirmation_runs_until_kill_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMT5()
    make_real(fake)
    monkeypatch.setattr(botmod.Bot, "__init__", _init_with(fake, botmod.Bot.__init__))
    cfg_path = _write_cfg(tmp_path)
    (tmp_path / "KILL").touch()
    try:
        code = cli.main(["--config", str(cfg_path), "--confirm-real-money", "--no-countdown"])
    finally:
        _close_logs()
    assert code == botmod.EXIT_OK


def test_countdown_can_be_aborted(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot()
    ticks: list[float] = []
    bot._wait = lambda s: (ticks.append(s), bot.request_stop(2) if len(ticks) == 3 else None)  # type: ignore
    assert cli.countdown(bot) is False
    assert len(ticks) == 3


def test_state_is_separated_per_account(make_cfg, tmp_path: Path, waits) -> None:
    cfg = make_cfg()
    demo, real = FakeMT5(), FakeMT5()
    make_real(real)
    bots = []
    for fake, confirm in ((demo, False), (real, True)):
        b = botmod.Bot(cfg, api=fake, wait=waits.append, env={}, signal_source=FixedSource(),
                       confirm_real_money=confirm, globber=lambda p: [])
        b.startup()
        bots.append(b)
    demo_dir, real_dir = bots[0].state_dir, bots[1].state_dir
    assert demo_dir != real_dir
    assert demo_dir.name == account_key("Broker-Demo", 87654321)
    assert real_dir.name == account_key("STARTRADER-Real", 12345678)
    bots[0].registry.set_kv("marker", "demo-only")
    assert bots[1].registry.get_kv("marker") is None
    bots[0].guard.update("2025-10-09", 5_000.0, 2.0, 12.0)
    assert bots[1].guard.state.start_equity != 5_000.0
    for b in bots:
        b.close()


def test_account_key_is_filesystem_safe() -> None:
    assert account_key("Broker: Live/01", 42) == "Broker-Live-01_42"


# ---------------------------------------------------------------- helpers


def _init_with(fake: FakeMT5, original):
    def patched(self, cfg, api=None, **kwargs):
        kwargs.setdefault("signal_source", FixedSource(None))
        kwargs["globber"] = lambda pattern: []
        kwargs["env"] = {}
        original(self, cfg, api=fake, **kwargs)

    return patched


def _write_cfg(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "log_file": str(tmp_path / "bot.log"), "state_root": str(tmp_path / "state"),
        "kill_switch_file": str(tmp_path / "KILL"), "health_file": str(tmp_path / "health.json"),
        "reports_dir": str(tmp_path / "reports"),
    }))
    return path


def _close_logs() -> None:
    for handler in list(cli.log.handlers):
        handler.close()
        cli.log.removeHandler(handler)
    cli.log.propagate = True
