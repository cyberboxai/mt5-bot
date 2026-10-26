"""Trade-management policies: paired replays, evidence-based choice and live application."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from conftest import EURUSD_SPEC, RATES_DTYPE, TEST_COSTS, FakeMT5, FixedSource, synthetic_bars
from mt5bot import management
from mt5bot.backtest import VirtualPosition, exit_for_bar
from mt5bot.config import Config
from mt5bot.registry import Registry
from mt5bot.team.agents.execution import PositionManager
from mt5bot.team.context import PositionView

T0 = 1_700_000_000
RISK_MONEY = 200.0  # 1 lot EURUSD, 20 pips to the stop


def bars_from(rows: list[tuple[float, float, float, float]], spread: int = 0) -> np.ndarray:
    """(open, high, low, close) rows -> an MT5-like rates array of H1 bars."""
    out = np.zeros(len(rows), dtype=RATES_DTYPE)
    for i, (o, h, lo, c) in enumerate(rows):
        out[i] = (T0 + i * 3600, o, h, lo, c, 100, spread, 0)
    return out


def long_position() -> VirtualPosition:
    return VirtualPosition(1, 0, T0, 1.1000, 1.0980, 1.1060, 1.0, RISK_MONEY)


# ------------------------------------------------------------------ replay


def test_replay_policies_on_a_known_path() -> None:
    # Runs to +1.25R, falls back through the entry, then hits the original stop.
    bars = bars_from([(1.1000, 1.1005, 1.0995, 1.1003), (1.1003, 1.1028, 1.1000, 1.1025),
                      (1.1024, 1.1024, 1.0990, 1.0992), (1.0992, 1.0993, 1.0975, 1.0978)])
    atr = np.full(len(bars), 0.0010)
    pos = long_position()
    fixed_exit = 1.0980 - 2 * EURUSD_SPEC.point  # SL minus slippage, as the shadow book records it
    out = management.replay_all(pos, bars, 0, 3, fixed_exit, "sl", atr, EURUSD_SPEC, TEST_COSTS)
    assert out["fixed"] == pytest.approx(-1.045, abs=0.01)
    assert out["breakeven"] == pytest.approx(-0.04, abs=0.01)    # stopped at entry + 1 point
    assert out["trail_atr"] == pytest.approx(0.205, abs=0.01)    # 2 ATR under the 1.1025 close
    assert out["time_stop"] == pytest.approx(out["fixed"])       # 24 bars never elapsed


def test_time_stop_closes_at_the_open_after_24_bars() -> None:
    flat = [(1.1000, 1.1004, 1.0996, 1.1000)] * 30
    bars = bars_from(flat)
    atr = np.full(len(bars), 0.0010)
    trade = management.replay(long_position(), bars, 0, 29, 1.1000, "opposite", management.RULES["time_stop"], atr,
                              EURUSD_SPEC, TEST_COSTS)
    # Bars open counts from the signal bar (as live trades do): 24 at the close of bar 23,
    # so the position closes at the next open.
    assert trade.exit_reason == "time_stop" and trade.exit_index == 24


@pytest.mark.parametrize("seed", range(6))
def test_policies_never_exit_after_the_fixed_exit_nor_lose_more(seed: int) -> None:
    rng = np.random.default_rng(seed)
    bars = synthetic_bars(400, seed=seed)
    atr = np.full(len(bars), 0.0015)
    for _ in range(25):
        start = int(rng.integers(0, 300))
        entry = float(bars["open"][start])
        side = int(rng.choice([-1, 1]))
        pos = VirtualPosition(side, start, int(bars["time"][start]), entry, entry - side * 0.0030,
                              entry + side * 0.0060, 1.0, 300.0)
        fixed = None
        for i in range(start, len(bars)):
            b = bars[i]
            hit = exit_for_bar(pos, float(b["open"]), float(b["high"]), float(b["low"]), float(b["spread"]),
                               EURUSD_SPEC.point, TEST_COSTS.slippage_points, i == start)
            if hit is not None:
                fixed = (i, hit[0], hit[1])
                break
        if fixed is None:
            continue
        end, price, reason = fixed
        base = management.replay(pos, bars, start, end, price, reason, management.RULES["fixed"], atr, EURUSD_SPEC,
                                 TEST_COSTS)
        for name, rules in management.RULES.items():
            trade = management.replay(pos, bars, start, end, price, reason, rules, atr, EURUSD_SPEC, TEST_COSTS)
            assert trade.exit_index <= end
            assert trade.r >= min(base.r, -1.0) - 0.2, name  # a tighter stop never loses more


def test_stop_update_only_tightens() -> None:
    rules = management.RULES["trail_atr"]
    # Trail level below the current stop: no update.
    assert management.stop_update(rules, 1, 1.1000, 1.1010, 1.5, 1.1020, 0.0010, 0.00001) is None
    up = management.stop_update(rules, 1, 1.1000, 1.0980, 1.5, 1.1030, 0.0010, 0.00001)
    assert up is not None and up[0] == pytest.approx(1.1010)
    down = management.stop_update(rules, -1, 1.1000, 1.1020, 1.5, 1.0970, 0.0010, 0.00001)
    assert down is not None and down[0] == pytest.approx(1.0990)
    assert management.stop_update(management.RULES["fixed"], 1, 1.1, 1.098, 5.0, 1.11, 0.001, 0.00001) is None


# ------------------------------------------------------------------ choice


def paired(n: int, edge: dict[str, float], noise: float = 0.3, seed: int = 1) -> dict[str, list[float]]:
    rng = np.random.default_rng(seed)
    base = rng.normal(0.0, 1.0, n)
    return {p: list(base + edge.get(p, 0.0) + rng.normal(0.0, noise, n)) for p in management.POLICIES}


def test_choice_needs_enough_paired_trades() -> None:
    choice = management.choose("fixed", paired(10, {"breakeven": 0.5}), 30, 0.6)
    assert choice.policy == "fixed" and not choice.changed


def test_clear_evidence_switches_policy() -> None:
    choice = management.choose("fixed", paired(60, {"breakeven": 0.3}), 30, 0.6)
    assert choice.policy == "breakeven" and choice.changed and "P(better)" in choice.reason


def test_noise_does_not_flip_the_incumbent() -> None:
    choice = management.choose("breakeven", paired(40, {"fixed": 0.02}, noise=1.0, seed=3), 30, 0.9)
    assert choice.policy == "breakeven" and not choice.changed


def test_thin_arms_borrow_the_strategy_evidence() -> None:
    arms = [SimpleNamespace(id=i, strategy="donchian", timeframe="H1", policy="fixed") for i in (1, 2, 3)]
    evidence = {i: paired(15, {"trail_atr": 0.4}, seed=i) for i in (1, 2, 3)}
    choices = management.choose_for_arms(arms, evidence, 30, 0.6)
    assert all(c.policy == "trail_atr" and c.level == "strategy" for c in choices.values())


# ------------------------------------------------------------- evidence store


def engine(tmp_path: Path, **management_cfg: Any) -> Any:
    from mt5bot.arms import ArmEngine

    cfg = Config.from_dict({"management": management_cfg}) if management_cfg else Config()
    reg = Registry(tmp_path / "mt5bot.sqlite")
    return ArmEngine(reg, cfg.tiers, cfg.research, management_cfg=cfg.management)


def test_shadow_trades_are_replayed_under_every_policy(tmp_path: Path) -> None:
    eng = engine(tmp_path)
    arm = eng.ensure_arm("EURUSD", "H1", "donchian", {"channel": 20, "sl_mult": 1.5, "tp_mult": 2.0}, "test")
    bars = synthetic_bars(2100, seed=11, trend=0.15)
    eng.on_bar("EURUSD", "H1", bars[:200], EURUSD_SPEC, TEST_COSTS)
    eng.on_bar("EURUSD", "H1", bars, EURUSD_SPEC, TEST_COSTS)
    closed = eng.registry.trades(mode="shadow", status="closed", arm_id=arm.id)
    rows = eng.registry.policy_outcomes()
    assert len(closed) > 20 and len(rows) == 4 * len(closed)
    by_trade: dict[int, dict[str, float]] = {}
    for _id, arm_id, trade_id, policy, r in rows:
        assert arm_id == arm.id
        by_trade.setdefault(trade_id, {})[policy] = r
    for trade in closed:
        assert by_trade[trade.id]["fixed"] == pytest.approx(trade.r, abs=1e-9)
    eng.registry.close()


def record(eng: Any, arm: Any, n: int, edge: dict[str, float]) -> None:
    for i, rs in enumerate(zip(*(paired(n, edge)[p] for p in management.POLICIES))):
        eng.registry.record_policy_outcomes(arm.id, 10_000 + i, dict(zip(management.POLICIES, rs)))


def test_engine_switches_policy_with_an_audit_row(tmp_path: Path) -> None:
    eng = engine(tmp_path)
    arm = eng.ensure_arm("EURUSD", "H1", "donchian", {"channel": 20, "sl_mult": 1.5, "tp_mult": 2.0}, "test")
    record(eng, arm, 60, {"breakeven": 0.3})
    changed = eng.refresh_policies(force=True)
    assert [(a.id, c.policy) for a, c in changed] == [(arm.id, "breakeven")]
    assert eng.store.get(arm.id).policy == "breakeven"
    audit = eng.registry.promotions(1)[0]
    assert audit["action"] == "policy" and audit["new_desc"].startswith("breakeven")
    assert eng.refresh_policies(force=True) == []  # stable: no second audit row
    eng.registry.close()


def test_disabled_policies_change_nothing(tmp_path: Path) -> None:
    eng = engine(tmp_path, policies_enabled=False)
    arm = eng.ensure_arm("EURUSD", "H1", "donchian", {"channel": 20, "sl_mult": 1.5, "tp_mult": 2.0}, "test")
    record(eng, arm, 60, {"breakeven": 0.3})
    assert eng.refresh_policies(force=True) == [] and eng.store.get(arm.id).policy == "fixed"
    eng.registry.close()


# ------------------------------------------------------------- live manager


def manager(**management_cfg: Any) -> PositionManager:
    pm = PositionManager.__new__(PositionManager)  # pure planning: no team services needed
    cfg = Config.from_dict({"management": management_cfg}) if management_cfg else Config()
    pm.services = SimpleNamespace(cfg=cfg)
    pm._partial_done = set()
    pm.set_state = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return pm


def view(policy: str, unrealized_r: float, bars_open: int = 3, sl: float = 1.0980) -> PositionView:
    return PositionView("EURUSD", "BUY", 0.1, 1.1000, sl, 1.1060, 7, 0.5, unrealized_r, bars_open, policy=policy,
                        timeframe="H1")


BARS = {"EURUSD": synthetic_bars(100, seed=2)}
POINT = {"EURUSD": 0.00001}


def plan(pm: PositionManager, p: PositionView, news: Any = None, spiking: bool = False) -> list:
    return pm.plan([p], BARS, POINT, {"EURUSD": news}, {"EURUSD": spiking})


def test_fixed_policy_leaves_the_position_alone() -> None:
    assert plan(manager(), view("fixed", 1.5)) == []


def test_breakeven_policy_moves_the_stop_to_entry() -> None:
    actions = plan(manager(), view("breakeven", 1.2))
    assert len(actions) == 1 and actions[0].action == "modify_sl"
    assert actions[0].new_sl == pytest.approx(1.10001) and "policy breakeven" in actions[0].reason


def test_time_stop_policy_closes_even_while_the_spread_spikes() -> None:
    actions = plan(manager(), view("time_stop", 0.2, bars_open=24), spiking=True)
    assert [a.action for a in actions] == ["close"]


def test_stop_moves_wait_for_a_normal_spread() -> None:
    assert plan(manager(), view("breakeven", 1.2), spiking=True) == []
    assert plan(manager(spread_spike_protect=False), view("breakeven", 1.2), spiking=True)


def test_news_derisking_moves_profitable_positions_to_break_even() -> None:
    actions = plan(manager(), view("fixed", 0.6), news=10.0)
    assert len(actions) == 1 and actions[0].new_sl == pytest.approx(1.10001) and "news" in actions[0].reason
    assert plan(manager(), view("fixed", 0.3), news=10.0) == []        # not enough profit to lock
    assert plan(manager(), view("fixed", 0.6), news=40.0) == []        # news too far away
    assert plan(manager(news_derisk_minutes=None), view("fixed", 0.6), news=10.0) == []


def test_disabled_policies_fall_back_to_fixed_stops() -> None:
    assert plan(manager(policies_enabled=False), view("breakeven", 1.2)) == []


# ------------------------------------------------------------- integration


def test_live_position_follows_its_arm_policy(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot(source=FixedSource(None))
    bot.startup()
    bot.run_once()
    tf = bot.cfg.timeframe
    last = bot._last_bar[("EURUSD", tf)]
    pos = fake_mt5.add_position("EURUSD", "BUY", bot.cfg.magic, volume=0.1, price_open=1.09900, sl=1.09800,
                                tp=1.10500)
    # +10 USD at the current bid on 9 USD at risk: +1.11R, past the break-even trigger.
    bot.registry.open_trade("live", "EURUSD", None, "BUY", 1.09900, 1.09800, 1.10500, 0.1, 9.0, ticket=pos.ticket,
                            open_bar_time=last, extra={"policy": "breakeven"}, timeframe=tf)
    from mt5bot import teambridge

    views = teambridge.position_views(bot, fake_mt5.account_info())
    assert views[0].policy == "breakeven" and views[0].timeframe == tf
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    assert pos.sl == pytest.approx(1.09901)
    views = teambridge.position_views(bot, fake_mt5.account_info())
    assert views[0].bars_open == 1  # counted on the arm's timeframe (regression: was always 0)
    bot.close()
