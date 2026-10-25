"""The team inside the real trading loop (fake MT5): dossiers and journal links, size only
shrinks, identical orders across advisor failures, fail-open fallback without double
orders, Guardian freeze / kill, panel pause, optional rules, and the simulation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import fake_anthropic as fa
from conftest import FakeMT5, FixedSource
from team_helpers import fixed
from mt5bot.advisor import NullAdvisor
from mt5bot.advisor.claude import ClaudeAdvisor
from mt5bot.config import Config

KEY = "sk-ant-api03-INTEGRATION-000000000000"


def opening_sends(fake: FakeMT5) -> list[dict]:
    return [r for r in fake.sends if "position" not in r and r.get("action") == fake.TRADE_ACTION_DEAL and r.get("sl")]


def start(make_bot: Any, **kwargs: Any) -> Any:
    bot = make_bot(**kwargs)
    bot.startup()
    assert bot.team is not None
    assert bot.run_once() is None  # first loop only records the bar
    return bot


def next_bar(bot: Any, fake: FakeMT5) -> None:
    fake.advance_bar("EURUSD")
    assert bot.run_once() is None


def pin(bot: Any, **stances: tuple) -> None:
    for agent_id, (stance, conf) in stances.items():
        bot.team.agents[agent_id].analyze = fixed(agent_id, stance, conf)


def test_full_mode_trade_has_dossier_and_journal_link(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot, team_mode="full")
    pin(bot, trend_analyst=("long", 0.7), breakout_analyst=("long", 0.7), mean_reversion_analyst=("flat", 0.5),
        mtf_analyst=("long", 0.6))
    next_bar(bot, fake_mt5)
    sends = opening_sends(fake_mt5)
    assert len(sends) == 1
    dossier = bot.team.store.dossiers(5)[0]
    assert dossier["final_action"] == "enter" and dossier["execution"]["ok"]
    row = bot.registry.trade_by_ticket(dossier["execution"]["ticket"])
    assert row.extra["dossier_id"] == dossier["id"] and row.extra["size_multiplier"] == pytest.approx(
        dossier["final_multiplier"])


def test_team_disagreement_shrinks_the_real_order(make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
    volumes = {}
    for mode in ("baseline", "full"):
        fake = FakeMT5()
        bot = make_bot(team_mode=mode, state_root=str(tmp_path / mode))
        bot.mt5 = fake
        bot.startup()
        bot.run_once()
        pin(bot, trend_analyst=("long", 0.7), mean_reversion_analyst=("short", 0.4), breakout_analyst=("flat", 0.5),
            mtf_analyst=("flat", 0.5))
        fake.advance_bar("EURUSD")
        bot.run_once()
        volumes[mode] = [s["volume"] for s in opening_sends(fake)]
    assert len(volumes["baseline"]) == len(volumes["full"]) == 1
    assert 0 < volumes["full"][0] < volumes["baseline"][0]


def claude(behaviour: Any) -> ClaudeAdvisor:
    cfg = Config.from_dict({"claude": {"enabled": True, "latency_budget_ms": 300}}).claude
    return ClaudeAdvisor(cfg, env={"ANTHROPIC_API_KEY": KEY}, client_factory=fa.ClientBox(fa.FakeClient(behaviour)),
                         keyring_module=None)


@pytest.mark.parametrize("failure", ["invalid_key_401", "credit_exhausted_400", "rate_limit_429", "overloaded_529",
                                     "timeout", "connection_error"])
@pytest.mark.parametrize("mode", ["baseline", "full"])
def test_orders_identical_to_null_advisor_in_failure_modes(make_bot, tmp_path: Path, failure: str, mode: str) -> None:
    def run(advisor: Any, name: str) -> list:
        fake = FakeMT5()
        bot = make_bot(team_mode=mode, state_root=str(tmp_path / name), advisor=advisor)
        bot.mt5 = fake
        bot.startup()
        bot.run_once()
        pin(bot, trend_analyst=("long", 0.7), mean_reversion_analyst=("short", 0.3), breakout_analyst=("long", 0.6),
            mtf_analyst=("flat", 0.5))
        for close in (1.1002, 1.1004, 1.0999):
            fake.advance_bar("EURUSD", close=close)
            bot.run_once()
        return [(s["type"], s["volume"], s["sl"], s["tp"]) for s in opening_sends(fake)]

    reference = run(NullAdvisor(), "null")
    advisor = claude(fa.FAILURES[failure]())
    try:
        assert run(advisor, failure) == reference and reference
    finally:
        advisor.close()


def test_team_crash_before_submit_falls_back_once(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot, team_mode="full")

    def broken(ctx, gateway):
        raise RuntimeError("team bug")

    bot.team.decide = broken
    next_bar(bot, fake_mt5)
    assert len(opening_sends(fake_mt5)) == 1  # deterministic v2 path took over


def test_team_crash_after_submit_never_sends_twice(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot, team_mode="baseline")
    original = bot.team.decide

    def submit_then_crash(ctx, gateway):
        original(ctx, gateway)
        raise RuntimeError("crash while writing the dossier")

    bot.team.decide = submit_then_crash
    next_bar(bot, fake_mt5)
    assert len(opening_sends(fake_mt5)) == 1


def test_team_start_failure_keeps_the_v2_core_trading(make_bot, fake_mt5: FakeMT5, monkeypatch) -> None:
    import mt5bot.team.runtime as runtime_mod

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cannot build the office")

    monkeypatch.setattr(runtime_mod, "TeamRuntime", explode)
    bot = make_bot()
    bot.startup()
    assert bot.team is None
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    assert len(opening_sends(fake_mt5)) == 1


def test_panel_pause_blocks_new_entries_without_closing_positions(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot)
    (bot.state_dir / "PAUSE_ENTRIES").write_text("paused from the panel")
    bot._next_housekeeping = 0
    next_bar(bot, fake_mt5)
    assert opening_sends(fake_mt5) == []
    (bot.state_dir / "PAUSE_ENTRIES").unlink()
    next_bar(bot, fake_mt5)
    assert len(opening_sends(fake_mt5)) == 1
    pos_count = len(fake_mt5.positions)
    (bot.state_dir / "PAUSE_ENTRIES").write_text("again")
    next_bar(bot, fake_mt5)
    assert len(fake_mt5.positions) == pos_count  # pausing never closes anything


def test_guardian_freezes_on_an_unjournaled_position(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot)
    fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, sl=1.0, tp=2.0)  # appeared behind the bot's back
    bot._next_housekeeping = 0
    next_bar(bot, fake_mt5)
    assert bot._guardian_freeze and "journal" in bot._guardian_freeze
    assert opening_sends(fake_mt5) == []
    assert "Guardian freeze" in bot._entry_block_reason()


def test_guardian_kill_switch_on_ceiling_breach(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot)
    for _ in range(2):
        fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, sl=1.0, tp=2.0)
    bot._next_housekeeping = 0
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    assert bot._kill_path.exists() and "Guardian" in bot._kill_path.read_text()
    assert bot.run_once() is not None  # the next loop runs the kill switch and exits


@pytest.mark.parametrize("enabled", [True, False])
def test_advisor_outage_notification_follows_the_config(make_bot, enabled: bool) -> None:
    bot = make_bot(notifications={"advisor_state": enabled})
    sent: list[str] = []
    bot.notifier.send = sent.append  # type: ignore[method-assign]
    bot._notify_advisor("Claude advisor ONLINE -> OFFLINE_NO_CREDIT")
    assert bool(sent) is enabled


def test_guardian_crash_fails_closed(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot)

    def broken(snapshot):
        raise RuntimeError("guardian bug")

    bot.team.guardian_check = broken
    bot._next_housekeeping = 0
    next_bar(bot, fake_mt5)
    assert bot._guardian_freeze and opening_sends(fake_mt5) == []


def test_position_rules_are_off_by_default_and_tighten_only_when_on(make_bot, fake_mt5: FakeMT5) -> None:
    bot = start(make_bot, source=FixedSource(None))
    pos = fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, volume=0.1, price_open=1.09900, sl=1.09800,
                                tp=1.10500)
    bot.reconcile()
    next_bar(bot, fake_mt5)
    assert pos.sl == 1.09800  # default: no management at all
    on = make_bot(source=FixedSource(None), team={"position_manager": {"breakeven_after_r": 0.5}},
                  state_root=str(Path(bot.cfg.state_root).parent / "on"))
    bot.close()
    on.startup()
    on.reconcile()
    on.run_once()
    fake_mt5.advance_bar("EURUSD")
    on.run_once()
    assert pos.sl == pytest.approx(1.09901)  # moved to break-even (+1 point), never wider
    on.close()


def test_health_file_reports_team_and_advisor_without_secrets(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot(advisor=claude(fa.response(fa.review_payload())))
    bot.startup()
    bot._next_housekeeping = 0
    bot.run_once()
    payload = json.loads(Path(bot.cfg.health_file).read_text())
    assert payload["team"]["effective_mode"] == "baseline" and payload["advisor"]["state"] == "ONLINE"
    assert KEY not in json.dumps(payload)


@pytest.fixture(scope="module")
def sim_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from mt5bot.sim.runner import run_simulation

    import threading

    root = tmp_path_factory.mktemp("sim")
    stop = threading.Event()

    def enough(step: int, bot: Any) -> None:
        if step >= 20 and bot.team.store.dossiers(1) and bot.team.store.advisor_calls(1):
            stop.set()  # stop as soon as the office has a decision meeting and an advisor review

    assert run_simulation(Config(), root, steps=200, speed=0, warmup_bars=700, stop=stop, on_step=enough) == 0
    return root


def test_simulation_produces_a_populated_office(sim_dir: Path) -> None:
    from mt5bot.officedb import OfficeStore

    office = next(sim_dir.glob("*/office.sqlite"))
    store = OfficeStore(office)
    try:
        states = store.agent_states()
        assert len(states) == 21
        assert store.dossiers(500), "the seeded champions must produce decision meetings"
        topics = {e["topic"] for e in store.latest_events(5000)}
        assert {"agent.state", "decision.started", "decision.finished", "market.snapshot"} <= topics
        assert store.advisor_calls(), "the simulated advisor audits its calls"
    finally:
        store.close()
    health = json.loads((sim_dir / "health.json").read_text())
    assert health["account_type"] == "DEMO" and health["team"]["effective_mode"] == "full"


def test_cli_ablation_report_and_reset(sim_dir: Path, tmp_path: Path, capsys) -> None:
    from mt5bot import cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state_root": str(sim_dir), "health_file": str(sim_dir / "health.json")}))
    assert cli.main(["--config", str(config), "--ablation-report"]) == 0
    out = capsys.readouterr().out
    assert "Team ablation report" in out and "Verdict" in out
    assert cli.main(["--config", str(config), "--reset-team-weights"]) == 0
