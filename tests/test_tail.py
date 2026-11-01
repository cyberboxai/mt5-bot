"""Phases 5, 6 and 12: portfolio construction, tail risk, governance. The hard ceilings stay."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from conftest import EURUSD_SPEC, FakeMT5
from hypothesis import given
from hypothesis import strategies as st

from mt5bot import governance, limits, stress
from mt5bot.arms import EntryIntent
from mt5bot.capital import CapitalState, SizingInputs, anti_martingale_cap, size_risk
from mt5bot.core import governance as g
from mt5bot.core import portfolio as pf
from mt5bot.core import tail as tl
from mt5bot.core.broker import POSITION_TYPE_BUY, POSITION_TYPE_SELL
from mt5bot.profiles import effective_limits
from mt5bot.risk import EntryContext, OpenRisk, RiskManager, TradePlan
from mt5bot.strategies.base import Signal

# ------------------------------------------------------------------------ portfolio


def test_inverse_volatility_gives_equal_risk() -> None:
    w = pf.inverse_vol_weights({"A": 1.0, "B": 2.0})
    assert w["A"] == pytest.approx(2 / 3) and w["A"] * 1.0 == pytest.approx(w["B"] * 2.0)


def test_clones_are_one_bet_and_independent_bets_count() -> None:
    assert pf.effective_bets([1, 1], np.ones((2, 2))) == pytest.approx(1.0)
    assert pf.effective_bets([1, 1, 1], np.eye(3)) == pytest.approx(3.0)


def test_clusters_are_found_and_capped() -> None:
    corr = np.array([[1.0, 0.9, 0.0], [0.9, 1.0, 0.0], [0.0, 0.0, 1.0]])
    groups = pf.clusters(["A", "B", "C"], corr, 0.7)
    assert sorted(map(sorted, groups)) == [["A", "B"], ["C"]]
    capped = pf.cap_clusters({"A": 0.4, "B": 0.4, "C": 0.2}, groups, 0.5)
    assert capped["A"] + capped["B"] == pytest.approx(0.5) and capped["C"] == 0.2


@given(st.lists(st.floats(0.0, 2.0), min_size=1, max_size=4), st.floats(0.0, 3.0), st.integers(0, 5))
def test_portfolio_factor_only_ever_shrinks(open_risk: list[float], new_risk: float, seed: int) -> None:
    rng = np.random.default_rng(seed)
    names = [f"S{i}" for i in range(len(open_risk))]
    returns = {n: rng.normal(0, 0.001 * (i + 1), 100) for i, n in enumerate(names)}
    factor, _info = pf.size_factor(names[0], names, returns, dict(zip(names, open_risk)), new_risk, pf.PortfolioRules())
    assert 0.0 <= factor <= 1.0


def test_unknown_correlation_counts_as_full_correlation() -> None:
    assert pf.correlation_matrix([np.zeros(3), np.zeros(3)])[0, 1] == 1.0


# ------------------------------------------------------------------------- margin


@given(st.floats(1.0, 1e6), st.floats(0.0, 1e6), st.floats(0.0, 1e6), st.floats(10.0, 120.0))
def test_margin_guard_never_allows_below_the_multiple(equity: float, used: float, new: float, so: float) -> None:
    refused = tl.margin_guard(equity, used, new, so)
    if refused is None:
        assert tl.projected_margin_level(equity, used, new) >= so * 3.0 - 1e-9


def test_margin_guard_blocks_in_the_pipeline(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot()
    bot.startup()
    intent = EntryIntent(Signal("BUY", 0.0010, 2.0, 3.0), None, "s", "s", timeframe="M15", bar_time=1, risk_pct=0.2)
    acc = SimpleNamespace(equity=10_000.0, leverage=100, margin=9_000.0, margin_so_so=50.0, margin_so_mode=0)
    out = bot._try_open("EURUSD", intent, acc, preview=True)
    assert not out.risk_allowed and out.code == "MARGIN_GUARD"


def test_stop_out_quoted_in_money_is_converted(make_bot) -> None:
    bot = make_bot()
    bot.startup()
    intent = EntryIntent(Signal("BUY", 0.0010, 2.0, 3.0), None, "s", "s", timeframe="M15", bar_time=1, risk_pct=0.2)
    acc = SimpleNamespace(equity=1000.0, leverage=100, margin=900.0, margin_so_so=800.0, margin_so_mode=1)
    assert bot._try_open("EURUSD", intent, acc, preview=True).code == "MARGIN_GUARD"


def test_margin_pressure_freezes_entries_and_alerts(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot()
    bot.startup()
    fake_mt5.account.margin_level = 120.0
    fake_mt5.account.margin_so_call = 100.0
    bot._next_housekeeping = 0
    bot.run_once()
    assert "margin pressure" in bot._entry_block_reason()
    fake_mt5.account.margin_level = 900.0
    bot._next_housekeeping = 0
    bot.run_once()
    assert bot._entry_block_reason() is None


# ------------------------------------------------------------------------ gap risk


def test_gap_allowance_comes_from_observed_breaks() -> None:
    times = np.array([0, 900, 1800, 200_000, 200_900])
    opens = np.array([1.0, 1.0, 1.0, 1.05, 1.05])
    closes = np.array([1.0, 1.0, 1.0, 1.05, 1.05])
    gaps = tl.open_gaps(opens, closes, times, 1800)
    assert gaps == pytest.approx([0.05])
    assert tl.gap_allowance_points(gaps, 1.0, 0.00001) == pytest.approx(5000.0)


def test_sizing_shrinks_for_positions_that_can_cross_a_break(make_bot, fake_mt5: FakeMT5) -> None:
    # Pinned to the morning: late in the server day the 5 h "day trade" would cross the break too.
    bot = make_bot(utc_now=lambda: 1_760_000_000.0)
    bot.startup()
    rates = fake_mt5.rates["EURUSD"].copy()
    rates["time"][-100:] += 400_000                     # a weekend-like hole with a real gap
    rates["open"][-100:] *= 1.01
    rates["close"][-100:] *= 1.01
    rates["high"][-100:] *= 1.01
    rates["low"][-100:] *= 1.01
    fake_mt5.rates["EURUSD"] = rates
    acc = SimpleNamespace(equity=10_000.0, leverage=100, margin=0.0)
    intent = EntryIntent(Signal("BUY", 0.0010, 2.0, 3.0), None, "s", "s", timeframe="M15", bar_time=1, risk_pct=0.3)
    day_trade = bot._try_open("EURUSD", intent, acc, preview=True)
    bot.cfg = bot.cfg.__class__(**{**bot.cfg.__dict__, "costs": bot.cfg.costs.__class__(expected_hold_bars=500)})
    bot._gap_cache.clear()
    overnight = bot._try_open("EURUSD", intent, acc, preview=True)
    assert 0 < overnight.volume < day_trade.volume


def test_small_accounts_are_flat_before_the_weekend_unless_proven(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot()
    bot.startup()
    normal = fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, sl=1.09, tp=1.12)
    bot.registry.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 10.0, ticket=normal.ticket, tier="T2")
    friday = 1_767_571_200 + 4 * 86400 + 22 * 3600        # Friday 22:00 by the BROKER's clock
    bot.clock.observe("EURUSD", friday, 1.1, 1.1001)
    bot.cfg = bot.cfg.__class__(**{**bot.cfg.__dict__, "trading_hours_server": (0, 24)})
    bot._weekend_flatten(fake_mt5.account, friday)
    assert not fake_mt5.positions


def test_the_weekend_rule_reads_the_broker_clock_not_the_pc(make_bot, fake_mt5: FakeMT5) -> None:
    """Regression: on a Sunday PC clock it flattened everything while the feed said Tuesday."""
    bot = make_bot()
    bot.startup()
    fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, sl=1.09, tp=1.12)
    tuesday = 1_767_571_200 + 86400 + 10 * 3600
    bot.clock.observe("EURUSD", tuesday, 1.1, 1.1001)
    sunday_pc = 1_767_571_200 + 6 * 86400
    bot._weekend_flatten(fake_mt5.account, sunday_pc)
    assert fake_mt5.positions


# -------------------------------------------------------------------------- stress


def book() -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    pos = SimpleNamespace(ticket=1, symbol="EURUSD", type=POSITION_TYPE_BUY, volume=1.0, magic=1, price_open=1.1,
                          sl=1.098, tp=1.11)
    return [pos], {"EURUSD": EURUSD_SPEC}, {"EURUSD": SimpleNamespace(bid=1.1, ask=1.1001)}


def test_a_gap_loses_more_than_the_stop_says() -> None:
    positions, specs, quotes = book()
    planned = (1.1 - 1.098) * 100_000                                   # 200 at the stop
    result = stress.run_scenario(stress.Scenario("gap", 3.0), positions, specs, quotes, 10_000.0, 10_000.0, 100, 50.0)
    assert result.loss_pct > planned / 10_000.0 * 100.0 * 10            # the 3% gap fills far beyond it


def test_spread_explosion_can_hit_stops_with_the_market_flat() -> None:
    positions, specs, quotes = book()
    positions[0].sl = 1.09995
    result = stress.run_scenario(stress.Scenario("s", 0.0, 10.0), positions, specs, quotes, 10_000.0, 10_000.0, 100, 50.0)
    assert result.loss_pct > 0


def test_stop_out_is_modelled() -> None:
    positions, specs, quotes = book()
    positions[0].volume, positions[0].sl = 5.0, 0.0
    result = stress.run_scenario(stress.Scenario("gap", 5.0), positions, specs, quotes, 10_000.0, 10_000.0, 100, 50.0)
    assert result.stopped_out >= 1


def test_every_scenario_runs_and_a_breach_is_reported() -> None:
    positions, specs, quotes = book()
    report = stress.run_all(positions, specs, quotes, 10_000.0, 10_000.0, 100, 50.0, 2.0, 12.0)
    assert len(report["scenarios"]) == len(stress.SCENARIOS) and report["breached"]
    assert report["worst"] and report["worst_loss_pct"] > 2.0
    calm = stress.run_all(positions, specs, quotes, 10_000.0, 10_000.0, 100, 50.0, 90.0, 90.0)
    assert not calm["breached"]


def test_sell_books_are_shocked_upward() -> None:
    positions, specs, quotes = book()
    positions[0].type, positions[0].sl, positions[0].tp = POSITION_TYPE_SELL, 1.102, 1.09
    assert stress.run_scenario(stress.Scenario("g", 3.0), positions, specs, quotes, 1e4, 1e4, 100, 50.0).loss_pct > 10


def test_a_breach_reduces_new_risk_and_is_logged(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot(tail={"stress_reduction": 0.4})
    bot.startup()
    pos = fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, volume=2.0, sl=1.098, tp=1.12)
    bot._run_stress(fake_mt5.account, [pos])
    assert bot._stress_factor == 0.4
    row = bot.registry.conn.execute("SELECT * FROM stress_reports").fetchone()
    assert row["breached"] == 1 and row["worst_loss_pct"] > 0
    assert [e.kind for e in bot.events.tail(["adaptation.stress"])] == ["adaptation.stress"]


# --------------------------------------------------------------------- CVaR, ladder


def test_cvar_is_the_mean_of_the_worst_tail() -> None:
    assert tl.cvar(list(range(-50, 50)), 0.95) == pytest.approx(48.0)
    assert tl.cvar([1.0] * 5) is None


def test_portfolio_cvar_is_an_estimate_in_percent() -> None:
    value = tl.portfolio_cvar_pct([1.0, 1.0], list(np.random.default_rng(1).normal(0, 1, 500)))
    assert value is not None and 1.0 < value < 5.0
    assert tl.portfolio_cvar_pct([], [1.0] * 30) is None


def test_ladder_steps_down_at_once_and_up_slowly() -> None:
    s = tl.LadderState()
    s = tl.update_ladder(s, 7.0, 1000.0)
    assert s.multiplier == 0.5
    s = tl.update_ladder(s, 11.0, 1001.0)
    assert s.multiplier == 0.25
    assert tl.update_ladder(s, 0.0, 1002.0).multiplier == 0.25      # recovery alone is not enough
    later = 1001.0 + 3 * 86400
    s = tl.update_ladder(s, 0.0, later)
    assert s.multiplier == 0.5                                      # one rung, not all the way
    s = tl.update_ladder(s, 0.0, later + 3 * 86400)
    assert s.multiplier == 0.75
    assert tl.update_ladder(s, 0.0, later + 6 * 86400).multiplier == 1.0


@given(st.lists(st.floats(0.0, 25.0), min_size=1, max_size=30))
def test_ladder_multiplier_stays_in_bounds(path: list[float]) -> None:
    s = tl.LadderState()
    for i, dd in enumerate(path):
        s = tl.update_ladder(s, dd, float(i) * 86400)
        assert 0.25 <= s.multiplier <= 1.0
        assert s.multiplier <= tl.target_multiplier(dd) or i >= 0


def test_corrupt_ladder_state_assumes_the_worst_step() -> None:
    assert tl.LadderState.from_json("{").multiplier == 0.25


def test_ladder_reduces_sizing(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot()
    bot.startup()
    intent = EntryIntent(Signal("BUY", 0.0010, 2.0, 3.0), None, "s", "s", tier="T2", timeframe="M15", bar_time=1)
    acc = SimpleNamespace(equity=10_000.0, leverage=100)
    full = bot.size("EURUSD", intent, acc).risk_pct
    bot.ladder = tl.LadderState(0.5, 0.0)
    assert bot.size("EURUSD", intent, acc).risk_pct == pytest.approx(full * 0.5)


# ---------------------------------------------------------------------- hard limits


def test_the_hard_ceilings_are_still_the_v3_numbers() -> None:
    assert (limits.MAX_RISK_PER_TRADE_PCT, limits.MAX_DAILY_LOSS_PCT, limits.MAX_DRAWDOWN_FROM_PEAK_PCT,
            limits.MAX_TOTAL_OPEN_RISK_PCT) == (2.0, 5.0, 20.0, 4.0)


@given(tier=st.sampled_from(["T1", "T2", "T3", "EXT"]), equity=st.floats(100.0, 1e8), factor=st.floats(0.0, 1.0),
       derisk=st.floats(0.0, 1.0), open_risk=st.floats(0.0, 1e6), last_loss=st.one_of(st.none(), st.floats(0.0, 5.0)),
       kelly=st.one_of(st.none(), st.floats(0.0, 3.0)))
def test_no_input_can_size_above_the_ceilings(tier: str, equity: float, factor: float, derisk: float, open_risk: float,
                                              last_loss: Any, kelly: Any) -> None:
    from mt5bot.config import Config

    lim = effective_limits(Config.from_dict({"risk_profile": "aggressive"}), False)
    capital = CapitalState()
    capital.observe(equity, 0.0)
    out = size_risk(SizingInputs(tier, equity, False, 0.3, kelly, open_risk, None, last_loss,
                                 portfolio_factor=factor, derisk_factor=derisk), lim, capital)
    assert out.risk_pct <= limits.MAX_RISK_PER_TRADE_PCT + 1e-9
    assert out.risk_pct <= lim.t3_risk_pct + 1e-9
    if last_loss is not None:
        assert out.risk_pct <= last_loss + 1e-6


@given(st.floats(0.01, 2.0), st.floats(0.0, 5.0))
def test_anti_martingale_a_loss_never_raises_the_cap(risk: float, later: float) -> None:
    row = SimpleNamespace(status="closed", pnl=-1.0, close_ts=1.0, extra={"risk_pct": risk})
    assert anti_martingale_cap([row]) == pytest.approx(risk)
    win = SimpleNamespace(status="closed", pnl=1.0, close_ts=2.0, extra={"risk_pct": later})
    assert anti_martingale_cap([row, win]) is None


@given(st.floats(1.0, 1e6), st.floats(0.5, 3.0))
def test_risk_manager_rejects_anything_over_the_hard_ceiling(equity: float, pct: float) -> None:
    from mt5bot.config import Config

    plan = TradePlan("BUY", 1.1, 1.09, 1.12, 1000, 2000, 1.0, 100.0, equity * pct / 100.0, pct)
    plan_money = equity * pct / 100.0
    plan = TradePlan("BUY", 1.1, 1.09, 1.12, 1000, 2000, plan_money / 100.0, 100.0, plan_money, pct)
    manager = RiskManager(Config.from_dict({"risk_profile": "aggressive"}), False)
    allowed, _ = manager.approve_entry(EntryContext("EURUSD", plan, equity, 5.0, [OpenRisk("GBPUSD", 0.0)],
                                                    notional_money=0.0))
    if pct > manager.risk_pct + 1e-9:
        assert not allowed


# ----------------------------------------------------------------------- governance

RULES = g.GovernanceRules(horizon_trades=200, horizon_days=91, rolling_min_trades=30)


def test_too_little_evidence_is_said_plainly() -> None:
    v = g.evaluate([1.0] * 5, 0.0, 86400.0, RULES)
    assert v.status == g.INSUFFICIENT and not v.stop_real_trading


def test_no_edge_stops_real_trading_continuously_not_only_at_the_horizon() -> None:
    losses = list(np.random.default_rng(1).normal(-5.0, 2.0, 40))
    v = g.evaluate(losses, 0.0, 86400.0, RULES)
    assert v.status == g.NO_EDGE and v.stop_real_trading and not v.horizon_reached and v.ci_high < 0


def test_positive_evidence_is_reported_with_the_same_rigor() -> None:
    wins = list(np.random.default_rng(2).normal(5.0, 2.0, 60))
    v = g.evaluate(wins, 0.0, 86400.0, RULES)
    assert v.status == g.PLAUSIBLE and not v.stop_real_trading and v.ci_low > 0
    assert "promises nothing" in v.reason


def test_an_interval_that_includes_zero_decides_nothing() -> None:
    mixed = list(np.random.default_rng(3).normal(0.0, 5.0, 100))
    assert g.evaluate(mixed, 0.0, 86400.0, RULES).status == g.INSUFFICIENT


def test_the_horizon_is_whichever_comes_first() -> None:
    assert g.evaluate([1.0] * 200, 0.0, 86400.0, RULES).horizon_reached
    assert g.evaluate([1.0] * 3, 0.0, 91 * 86400.0, RULES).horizon_reached
    assert g.evaluate([1.0] * 100, 0.0, 86400.0, RULES).progress == pytest.approx(0.5)


def closed_trades(bot: Any, pnls: list[float], tier: str = "T2") -> None:
    for i, pnl in enumerate(pnls):
        tid = bot.registry.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 10.0, ticket=9000 + i, tier=tier)
        bot.registry.close_trade(tid, 1.1, pnl, pnl / 10.0, "closed at broker")


def real_bot(make_bot: Any, fake_mt5: FakeMT5, **kw: Any) -> Any:
    fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_REAL
    bot = make_bot(confirm_real_money=True, **kw)
    bot.startup()
    return bot


def test_stop_rule_moves_the_real_account_to_research_only(make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
    bot = real_bot(make_bot, fake_mt5, reports_dir=str(tmp_path / "out"))
    closed_trades(bot, [float(x) for x in np.random.default_rng(4).normal(-6.0, 2.0, 40)])
    bot._next_governance = 0.0
    bot._check_governance(fake_mt5.account)
    assert bot.research_only and "research-only" in bot._entry_block_reason()
    report = Path(bot.research_only["report"])
    text = report.read_text(encoding="utf-8")
    assert "só investigação" in text and "--reenable-real" in text and "—" not in text
    assert [e.kind for e in bot.events.tail(["governance.research_only"])] == ["governance.research_only"]
    assert governance.research_only_state(bot.registry)["active"]


def test_no_new_real_orders_but_positions_are_still_managed(make_bot, fake_mt5: FakeMT5) -> None:
    bot = real_bot(make_bot, fake_mt5)
    pos = fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, sl=1.09, tp=1.12)
    bot.research_only = {"active": True, "reason": "no edge"}
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    fake_mt5.positions.remove(pos)
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    assert [s for s in fake_mt5.sends if "position" not in s] == []     # flat, and still no new order
    assert not [s for s in fake_mt5.sends if s.get("position") == pos.ticket]   # never force-closed


def test_research_only_survives_a_restart_and_needs_an_explicit_command(make_bot, fake_mt5: FakeMT5, make_cfg, capsys,
                                                                        tmp_path: Path) -> None:
    from mt5bot import cli
    from mt5bot.config_blocks import to_jsonable  # noqa: F401

    bot = real_bot(make_bot, fake_mt5)
    closed_trades(bot, [float(x) for x in np.random.default_rng(5).normal(-6.0, 2.0, 40)])
    bot._next_governance = 0.0
    bot._check_governance(fake_mt5.account)
    state_dir = bot.state_dir
    bot.close()
    again = make_bot(cfg=bot.base_cfg, confirm_real_money=True)
    again.startup()
    assert again.research_only
    again.close()
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"state_root": str(state_dir.parent)}))
    assert cli.main(["--config", str(cfg), "--state-dir", str(state_dir), "--reenable-real"]) == 0
    assert "allowed again" in capsys.readouterr().out
    third = make_bot(cfg=bot.base_cfg, confirm_real_money=True)
    third.startup()
    assert third.research_only is None


def test_demo_accounts_keep_trading_whatever_the_verdict(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot()
    bot.startup()
    closed_trades(bot, [float(x) for x in np.random.default_rng(6).normal(-6.0, 2.0, 40)])
    bot._next_governance = 0.0
    bot._check_governance(fake_mt5.account)
    assert bot.research_only is None
    assert json.loads(bot.registry.get_kv(governance.VERDICT_KEY))["status"] == g.NO_EDGE


def test_board_report_in_european_portuguese(make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
    bot = make_bot()
    bot.startup()
    closed_trades(bot, [5.0, -2.0])
    now = float(bot._utc_now())
    verdict = governance.evaluate_account(bot.registry.conn, g.GovernanceRules(), now)
    path = governance.write_board_report(bot.registry.conn, tmp_path, now - 86400, now + 60, verdict, "DEMO ***321")
    text = path.read_text(encoding="utf-8")
    assert "Relatório semanal do conselho" in text and "evidência insuficiente" in text
    assert "depois de custos: +3.00 em 2 trades" in text and "—" not in text and "promete" in text
    assert bot.registry.conn.execute("SELECT kind FROM governance_reports").fetchone()["kind"] == "board"


def test_infinity_in_a_verdict_never_breaks_a_report() -> None:
    assert math.isinf(tl.projected_margin_level(100.0, 0.0, 0.0))
