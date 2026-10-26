"""Decision funnel (reason codes, report, counterfactuals), --diagnose (v3.1 and legacy v3.0
data) and the Flow Doctor (bounded relax / review / revert / tighten)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mt5bot import flow, funnel
from mt5bot.config import Config, widen_hours
from mt5bot.config_blocks import FlowDoctorConfig
from mt5bot.db import SCHEMA_1, connect, db_path, ensure_schema
from mt5bot.diagnose import diagnose
from mt5bot.registry import Registry

NOW = 1_767_800_000.0


# ---------------------------------------------------------------------- funnel


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    reg = Registry(db_path(tmp_path), clock=lambda: NOW)
    yield reg
    reg.close()


def record_scenario(reg: Registry) -> funnel.Funnel:
    f = funnel.Funnel(reg.tx, clock=lambda: NOW - 600)
    for _ in range(40):
        f.record("scan", "info", "BAR_EVALUATED", "EURUSD", "M15")
    for i in range(10):
        f.record("signal", "info", "SIGNAL", "EURUSD", "M15", arm_id=1, bar_time=1000 + i,
                 values={"strategy": "donchian"})
    for i in range(6):
        f.record("data", "block", "OUTSIDE_HOURS", "EURUSD", "M15", arm_id=1, bar_time=1000 + i)
    f.record("risk", "block", "SPREAD_LIMIT", "XAUUSD", "H1", arm_id=2, bar_time=5000)
    f.record("execution", "pass", "ORDER_OK", "EURUSD", "M15", arm_id=1, bar_time=1009)
    f.record("risk", "block", "something odd")
    return f


class TestFunnel:
    def test_flush_never_interleaves_with_the_office_writer(self, registry: Registry) -> None:
        """Regression: the funnel ran BEGIN/INSERT/COMMIT as separate statements on the shared
        connection, so the office event writer could start its own transaction in between
        ("cannot start a transaction within a transaction") and events were lost."""
        import threading

        from mt5bot.officedb import OfficeStore

        office = OfficeStore(registry.path, clock=lambda: NOW, shared=registry)
        f = funnel.Funnel(registry.tx, clock=lambda: NOW)
        errors: list[BaseException] = []

        def office_writer() -> None:
            try:
                for i in range(300):
                    office.insert_events([{"ts": NOW, "topic": "agent.state", "agent": "cio", "payload": {"i": i}}])
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=office_writer)
        thread.start()
        for i in range(300):
            f.record("scan", "info", "BAR_EVALUATED", "EURUSD", "M15", bar_time=i)
            f.flush()
        thread.join()
        assert errors == [] and f.write_errors == 0
        assert registry.conn.execute("SELECT COUNT(*) FROM funnel_events").fetchone()[0] == 300
        assert registry.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 300
        assert not registry.raw.in_transaction

    def test_failed_commit_never_leaves_the_connection_in_a_transaction(self, registry: Registry) -> None:
        with pytest.raises(RuntimeError):
            with registry.tx() as conn:
                conn.execute("INSERT INTO kv (key, value) VALUES ('a', '1')")
                raise RuntimeError("boom")
        assert not registry.raw.in_transaction and registry.get_kv("a") is None
        with registry.tx() as conn:  # the next transaction starts normally
            conn.execute("INSERT INTO kv (key, value) VALUES ('b', '2')")
        assert registry.get_kv("b") == "2"

    def test_report_counts_and_ranks_blockers(self, registry: Registry) -> None:
        f = record_scenario(registry)
        assert f.counts[("block", "OTHER")] == 1 and f.last_order_ts == NOW - 600
        assert f.flush() == 59
        rep = funnel.report(registry.conn, NOW - 3600, NOW)
        assert rep["available"] and rep["bars_evaluated"] == 40 and rep["signals"] == 10 and rep["orders"] == 1
        top = rep["top_blockers"][0]
        assert top["reason"] == "OUTSIDE_HOURS" and top["count"] == 6 and top["knob"] == "hours_extend"
        assert top["share"] == pytest.approx(6 / 8) and "horário" in top["description"]
        assert rep["signals_by"]["strategy"] == {"donchian": 10}
        assert funnel.dominant_blocker(rep, relaxable_only=True)["reason"] == "OUTSIDE_HOURS"

    def test_counterfactual_value_of_a_filter(self, registry: Registry) -> None:
        f = record_scenario(registry)
        f.flush()
        # The arm's shadow book traded the blocked signals: three lost, so the hours filter protected value.
        for i, r in enumerate((-1.0, -1.0, 1.5)):
            tid = registry.open_trade("shadow", "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 50.0,
                                      open_bar_time=1000 + i + 1, arm_id=1, timeframe="M15")
            registry.close_trade(tid, 1.11, 50 * r, r, "tp" if r > 0 else "sl")
        cf = funnel.counterfactuals(registry.conn, NOW - 3600, NOW)["OUTSIDE_HOURS"]
        assert cf["n"] == 3 and cf["virtual_total_r"] == pytest.approx(-0.5) and cf["filter_value_r"] > 0

    def test_reason_mapping_from_messages(self) -> None:
        assert funnel.risk_reason_code("trading halted: daily loss limit (resets...)") == "HALTED_DAILY"
        assert funnel.risk_reason_code("bot already has a position on this symbol") == "ONE_PER_SYMBOL"
        assert funnel.risk_reason_code("total open risk 210 would exceed") == "RISK_OPEN_RISK"
        assert funnel.team_reason_code("aggregate confidence 0.51 < 0.52") == "TEAM_LOW_CONFIDENCE"
        assert funnel.team_reason_code("1 agreeing voter(s) < min_agreement 2") == "TEAM_NO_AGREEMENT"
        for code, (stage, text, knob) in funnel.REASONS.items():
            assert stage in funnel.STAGES and text and (knob is None or knob in flow.KNOBS), code

    def test_format_pt_without_and_with_data(self, registry: Registry) -> None:
        assert "Ainda não há registos" in funnel.format_report_pt({"available": False}, 24)
        record_scenario(registry).flush()
        text = funnel.format_report_pt(funnel.report(registry.conn, NOW - 3600, NOW), 1)
        assert "Barras avaliadas" in text and "OUTSIDE_HOURS" in text and "Doutor de Fluxo" in text

    def test_prune_keeps_recent_rows(self, registry: Registry) -> None:
        record_scenario(registry).flush()
        assert funnel.prune(registry.conn, NOW - 300) == 59
        assert funnel.report(registry.conn, 0, NOW)["events"] == 0


# --------------------------------------------------------------------- diagnose


def legacy_v30_state(state: Path) -> None:
    """A v3.0 registry: one failed cold-start optimisation, one finished run whose candidates
    all failed the gates, NoTrade champion, no shadow and no live trades."""
    state.mkdir(parents=True)
    conn = sqlite3.connect(state / "registry.sqlite")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript("BEGIN;" + SCHEMA_1 + "COMMIT;")
    conn.execute("INSERT INTO optimization_runs (symbol, trigger, started_at, finished_at, status, trials, notes) "
                 "VALUES ('EURUSD', 'cold start: no optimisation results yet', 1, 2, 'done', 210, '0 survivor(s)')")
    conn.execute("INSERT INTO param_sets (strategy, params_json) VALUES ('donchian', '{}')")
    for reasons in (["deflated Sharpe 0.85 < 0.9 for 210 trials"],
                    ["only 74 out-of-sample trades (< 100)", "profit factor 1.10 < 1.3"]):
        conn.execute("INSERT INTO candidate_metrics (run_id, symbol, param_set_id, passed, reasons_json, metrics_json,"
                     " score, rank) VALUES (1, 'EURUSD', 1, 0, ?, '{}', -1, 0)", (json.dumps(reasons),))
    conn.execute("INSERT INTO optimization_runs (symbol, trigger, started_at, finished_at, status, trials, notes) "
                 "VALUES ('EURUSD', 'weekly', 3, 4, 'failed', 0, 'RuntimeError: only 1501 cached bars')")
    conn.commit()
    conn.close()


class TestDiagnose:
    def test_legacy_v30_reconstruction(self, tmp_path: Path) -> None:
        state = tmp_path / "state" / "Broker_1"
        legacy_v30_state(state)
        log = tmp_path / "bot.log"
        log.write_text("2026-10-26 10:00:00 INFO     EURUSD: OBSERVATION: collecting data; first optimisation pending.\n"
                       "2026-10-26 10:00:05 INFO     EURUSD: OBSERVATION: collecting data; first optimisation pending.\n",
                       encoding="utf-8")
        text = diagnose(state, 24 * 365 * 20, [log], now=1_800_000_000.0)
        assert "versão 3.0" in text and "Candidatos avaliados: 2; aprovados em todos os portões: 0" in text
        assert "Sharpe deflacionado" in text and "poucos trades fora da amostra" in text
        assert "FALHOU" in text and "OBSERVATION" in text and "Actualize para a versão 3.1" in text

    def test_v31_funnel_and_advice(self, tmp_path: Path) -> None:
        reg = Registry(db_path(tmp_path), clock=lambda: NOW)
        try:
            record_scenario(reg).flush()
        finally:
            reg.close()
        text = diagnose(tmp_path, 1.0, [], now=NOW)
        assert "Sinais gerados .............. 10" in text and "Ordens executadas ........... 1" in text
        assert "Bloqueio principal: OUTSIDE_HOURS" in text

    def test_simulation_is_read_on_its_own_clock_and_quiet_windows_say_so(self, tmp_path: Path) -> None:
        """Regression: a simulation's funnel rows carry virtual time; judged against the PC clock the
        window looked empty and --diagnose fell back to the v3.0 advice ("upgrade to 3.1")."""
        reg = Registry(db_path(tmp_path), clock=lambda: NOW)
        try:
            record_scenario(reg).flush()  # rows at NOW - 600, months away from the PC clock
            reg.set_kv("simulation", "1")
        finally:
            reg.close()
        text = diagnose(tmp_path, 1.0, [])
        assert "Sinais gerados .............. 10" in text and "Actualize" not in text
        quiet = diagnose(tmp_path, 1.0, [], now=NOW + 30 * 86400)
        assert "Nenhuma decisão registada nesta janela" in quiet and "Actualize" not in quiet

    def test_cli_diagnose_matches_the_scenario(self, tmp_path: Path, capsys) -> None:
        from mt5bot import cli

        state = tmp_path / "state" / "Broker_7"
        reg = Registry(db_path(state), clock=lambda: NOW)
        try:
            f = record_scenario(reg)
            f.clock = lambda: __import__("time").time() - 60
            f.record("budget", "block", "EXPLORATION_BUDGET_SPENT", "EURUSD", "M15")
            f.flush()
        finally:
            reg.close()
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"state_root": str(tmp_path / "state"), "health_file": str(tmp_path / "h.json"),
                                   "log_file": str(tmp_path / "bot.log")}))
        assert cli.main(["--config", str(cfg), "--diagnose", "--hours", "1"]) == 0
        out = capsys.readouterr().out
        assert "EXPLORATION_BUDGET_SPENT" in out and "Funil de decisão" in out


# ------------------------------------------------------------------ flow doctor


FD = FlowDoctorConfig()


def inputs(**kw) -> flow.FlowInputs:
    values = dict(now=NOW, is_real=False, markets_open=True, last_order_ts=NOW - 7 * 3600, started_ts=NOW - 86400,
                  signals_in_window=12, top_blockers=[{"reason": "OUTSIDE_HOURS", "count": 9, "share": 0.75, "knob": "hours_extend"}],
                  counterfactuals={}, trades_since={}, daily_loss_used_frac=0.0, drawdown_frac=0.0,
                  tier_counts={"T0": 10}, min_agreement_cfg=1)
    values.update(kw)
    return flow.FlowInputs(**values)


class TestFlowDoctor:
    def test_relaxes_the_dominant_blocker_one_bounded_step(self) -> None:
        state = flow.FlowState()
        [adj] = flow.evaluate(inputs(), state, [], FD)
        assert adj.knob == "hours_extend" and adj.direction == "relax" and (adj.old, adj.new) == (0.0, 1.0)
        assert "OUTSIDE_HOURS" in adj.reason and adj.review_after == NOW + FD.review_after_hours * 3600
        flow.apply(state, [adj])
        assert flow.evaluate(inputs(), state, [], FD) == []  # one adjustment per min_hours_between_adjustments

    def test_not_idle_markets_closed_or_no_signals_do_nothing(self) -> None:
        for kw in ({"last_order_ts": NOW - 3600}, {"markets_open": False}, {"signals_in_window": 0}):
            assert flow.evaluate(inputs(**kw), flow.FlowState(), [], FD) == []

    def test_real_accounts_wait_longer(self) -> None:
        assert flow.evaluate(inputs(is_real=True), flow.FlowState(), [], FD) == []
        assert flow.evaluate(inputs(is_real=True, last_order_ts=NOW - 13 * 3600), flow.FlowState(), [], FD)

    def test_never_touches_non_knob_blockers_and_respects_ceilings(self) -> None:
        blockers = [{"reason": "HALTED_DAILY", "count": 50, "share": 0.5, "knob": None},
                    {"reason": "RISK_OPEN_RISK", "count": 30, "share": 0.3, "knob": None},
                    {"reason": "SPREAD_LIMIT", "count": 15, "share": 0.15, "knob": "spread_mult"}]
        [adj] = flow.evaluate(inputs(top_blockers=blockers), flow.FlowState(), [], FD)
        assert adj.knob == "spread_mult"
        capped = flow.FlowState()
        capped.knobs["spread_mult"] = flow.KNOBS["spread_mult"][3]
        assert flow.evaluate(inputs(top_blockers=blockers), capped, [], FD) == []

    def test_rare_blockers_never_loosen_a_filter(self) -> None:
        """Regression (seen in the demo): one SPREAD_LIMIT block in 284 relaxed the spread filter."""
        rare = [{"reason": "ONE_PER_SYMBOL", "count": 133, "share": 0.47, "knob": None},
                {"reason": "SPREAD_LIMIT", "count": 1, "share": 0.004, "knob": "spread_mult"}]
        assert flow.evaluate(inputs(top_blockers=rare), flow.FlowState(), [], FD) == []
        few = [{"reason": "SPREAD_LIMIT", "count": 4, "share": 0.8, "knob": "spread_mult"}]
        assert flow.evaluate(inputs(top_blockers=few), flow.FlowState(), [], FD) == []

    def test_protective_filters_are_never_relaxed(self) -> None:
        cf = {"OUTSIDE_HOURS": {"n": 25, "virtual_mean_r": -0.4}}
        assert flow.evaluate(inputs(counterfactuals=cf), flow.FlowState(), [], FD) == []

    def test_review_reverts_poor_relaxations_and_keeps_good_ones(self) -> None:
        bad = flow.Adjustment(1, NOW - 10, "hours_extend", 0.0, 1.0, "relax", "x", NOW - 1)
        good = flow.Adjustment(2, NOW - 10, "spread_mult", 0.0, 0.25, "relax", "y", NOW - 1)
        out = flow.evaluate(inputs(last_order_ts=NOW, trades_since={1: [-1.0] * 10, 2: [1.0, -1.0, 0.5] * 4}),
                            flow.FlowState(), [bad, good], FD)
        assert [a.direction for a in out] == ["revert"] and out[0].knob == "hours_extend" and out[0].new == 0.0
        assert bad.status == "reverted" and good.status == "kept"

    def test_tightens_when_the_account_degrades(self) -> None:
        active = flow.Adjustment(5, NOW - 100, "t1_entry_p", 0.0, -0.01, "relax", "z", NOW + 1e6)
        out = flow.evaluate(inputs(daily_loss_used_frac=0.7), flow.FlowState(), [active], FD)
        assert [a.direction for a in out] == ["tighten"] and out[0].new == 0.0 and active.status == "reverted"

    def test_overrides_are_bounded_and_validated(self) -> None:
        state = flow.FlowState()
        for knob, (_d, _s, lo, hi, _l) in flow.KNOBS.items():
            state.knobs[knob] = hi if hi > 0 else lo
        cfg = Config(trading_hours_server=(7, 20))
        out = flow.overrides(cfg, state)
        assert out["trading_hours_server"] == (3, 23)
        assert out["tiers.t1_enter_p"] >= 0.52 and 0.50 <= out["tiers.t1_exit_p"] < out["tiers.t1_enter_p"]
        assert out["team.min_agreement"] >= 1
        assert out["team.min_aggregate_confidence"] >= 0.50
        from mt5bot.config import apply_overrides

        merged = apply_overrides(cfg, out)  # still a valid config
        # No knob reaches a risk number: per-trade, daily, drawdown, exposure and budgets are untouched.
        for field in ("risk_profile", "risk_per_trade_pct_demo", "max_daily_loss_pct_demo",
                      "max_drawdown_from_peak_pct", "max_total_open_risk_pct", "capital"):
            assert getattr(merged, field) == getattr(cfg, field)
        assert merged.tiers.exploration_day_pct == cfg.tiers.exploration_day_pct

    def test_state_roundtrip_clamps_tampered_values(self) -> None:
        text = json.dumps({"knobs": {"hours_extend": 99, "spread_mult": -5, "unknown": 1}})
        state = flow.FlowState.from_json(text)
        assert state.knobs["hours_extend"] == flow.KNOBS["hours_extend"][3]
        assert state.knobs["spread_mult"] == flow.KNOBS["spread_mult"][2] and "unknown" not in state.knobs

    @pytest.mark.parametrize("window,extra,expected", [((7, 20), 2, (5, 22)), ((7, 20), 9, (1, 23)),
                                                        ((0, 24), 3, (0, 24)), ((22, 6), 2, (22, 6))])
    def test_hours_widen_within_the_widest_window(self, window, extra, expected) -> None:
        assert widen_hours(window, extra) == expected

    def test_status_text_is_plain_portuguese(self) -> None:
        text = flow.status_text({"T0": 5, "T1": 2, "T2": 1}, {"reason": "SPREAD_LIMIT",
                                                              "example": {"symbol": "XAUUSD"}}, 6, True, None)
        assert text.startswith("A negociar") and "2 braços T1" in text and "spread" in text and "XAUUSD" in text
        assert flow.status_text({}, None, 6, False, "daily loss limit").startswith("Pausado")


def test_flow_knobs_change_the_bot_window_but_never_the_limits(make_bot) -> None:
    bot = make_bot(trading_hours_server=[7, 20])
    bot.startup()
    limits_before = bot.risk.limits
    bot.flow_state.knobs["hours_extend"] = 2.0
    bot.flow_state.knobs["spread_mult"] = 0.5
    bot._apply_flow_knobs()
    assert bot.trading_window() == (5, 22) and bot.cfg.universe.spread_spike_mult == 3.0
    assert bot.base_cfg.trading_hours_server == (7, 20)
    assert bot.risk.limits == limits_before


def test_schema_is_versioned(tmp_path: Path) -> None:
    from mt5bot.db import SCHEMA_VERSION, user_version

    conn = connect(tmp_path / "x.sqlite")
    assert ensure_schema(conn) == list(range(1, SCHEMA_VERSION + 1)) and user_version(conn) == SCHEMA_VERSION
    assert ensure_schema(conn) == []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    assert {"arm_id", "timeframe", "tier", "risk_pct"} <= cols
    conn.close()


def test_period_report_has_a_self_review(registry: Registry, tmp_path: Path) -> None:
    from mt5bot.reports import write_report

    record_scenario(registry).flush()
    for k, (r, regime) in enumerate([(1.5, "trend"), (-1.0, "range"), (2.0, "trend")]):
        trade_id = registry.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 10.0, ticket=50 + k,
                                       open_ts=NOW - 3000, extra={"regime": regime, "slippage_points": 1.0})
        registry.close_trade(trade_id, 1.1, r * 10.0, r, "tp" if r > 0 else "sl", close_ts=NOW - 100)
    registry.conn.execute("INSERT INTO cash_flows (ts, ticket, amount, kind) VALUES (?, 1, 500.0, 'deposit')",
                          (NOW - 50,))
    path = write_report(registry, tmp_path / "reports", "daily_test", NOW - 3600, NOW, "demo", ["EURUSD"])
    text = path.read_text()
    assert "SELF-REVIEW" in text and "decision funnel: 40 bars evaluated, 10 signals, 1 orders" in text
    assert "OUTSIDE_HOURS: 6" in text
    assert "regime trend: 2 trades" in text and "regime range: 1 trades" in text
    assert "mean slippage +1.0 points over 3 fills" in text
    assert "deposits 500.00" in text and "excluded from performance" in text
    assert text.rstrip().endswith("does not guarantee profit.")
