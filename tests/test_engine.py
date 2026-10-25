"""Champion/challenger: promotion needs gates + shadow evidence + hysteresis + cooldown;
demotion on degradation; positions are never abandoned on a switch; cold start observes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from conftest import EURUSD_SPEC, TEST_COSTS, FakeMT5, synthetic_bars
from mt5bot import strategies as S
from mt5bot.backtest import simulate
from mt5bot.engine import AdaptiveEngine, EngineSettings, ShadowBook
from mt5bot.optimizer import CandidateResult
from mt5bot.registry import Registry

DAY = 86400.0
T0 = 1_760_000_000.0
SYMBOL = "EURUSD"


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t

    def advance(self, days: float) -> None:
        self.t += days * DAY


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(tmp_path: Path, clock: Clock) -> Registry:
    reg = Registry(tmp_path / "registry.sqlite", clock=clock)
    yield reg
    reg.close()


@pytest.fixture
def events() -> dict:
    return {"notify": [], "reopt": []}


@pytest.fixture
def engine(registry: Registry, clock: Clock, events: dict) -> AdaptiveEngine:
    return AdaptiveEngine(registry, EngineSettings(), notify=events["notify"].append,
                          request_reoptimization=lambda s, r: events["reopt"].append((s, r)), clock=clock)


def candidate(channel: int = 50, passed: bool = True, score: float = 1.0, expectancy: float = 0.3,
              win_rate: float = 0.45, dd: float = 6.0, strategy: str = "donchian") -> CandidateResult:
    params = {"channel": channel, "sl_mult": 2.0, "tp_mult": 3.0}
    metrics = {"expectancy_r": expectancy, "win_rate": win_rate, "max_drawdown_r": dd, "trades": 150.0,
               "profit_factor": 1.6}
    return CandidateResult(strategy, params, passed, [] if passed else ["failed a gate"], metrics, score)


def store_run(registry: Registry, *cands: CandidateResult) -> list[int]:
    run_id = registry.start_run(SYMBOL, "test")
    result = SimpleNamespace(symbol=SYMBOL, candidates=list(cands), trials=10, data_from=0, data_to=1, seconds=1.0,
                             survivors=[c for c in cands if c.passed])
    return registry.finish_run(run_id, result)


def add_trades(registry: Registry, clock: Clock, ps_id: Optional[int], rs: list[float], mode: str = "shadow",
               days: float = 0.5) -> None:
    for r in rs:
        tid = registry.open_trade(mode, SYMBOL, ps_id, "BUY", 1.1, 1.09, 1.12, 0.1, 50.0)
        clock.advance(days / 2)
        registry.close_trade(tid, 1.11, 50.0 * r, r, "tp" if r > 0 else "sl")
        clock.advance(days / 2)


def edge_rs(n: int, mean: float = 0.4) -> list[float]:
    # Alternating +1.8R / -1R produces a positive, stable expectancy.
    out = []
    while len(out) < n:
        out.extend([1.8, -1.0, 1.8, -1.0, 1.0])
    return out[:n]


class TestColdStart:
    def test_no_runs_means_observation(self, engine: AdaptiveEngine, registry: Registry) -> None:
        assert registry.champion(SYMBOL).is_notrade
        assert engine.check_promotion(SYMBOL) is None

    def test_failed_gates_are_never_challengers(self, engine: AdaptiveEngine, registry: Registry,
                                                clock: Clock) -> None:
        [cid] = store_run(registry, candidate(passed=False, score=5.0))
        engine.adopt_latest_run(SYMBOL)
        assert registry.challengers(SYMBOL) == []
        add_trades(registry, clock, registry.candidate(cid).param_set_id, edge_rs(40))
        assert engine.check_promotion(SYMBOL) is None

    def test_gates_alone_do_not_promote(self, engine: AdaptiveEngine, registry: Registry) -> None:
        store_run(registry, candidate())
        engine.adopt_latest_run(SYMBOL)
        assert len(registry.challengers(SYMBOL)) == 1
        assert engine.check_promotion(SYMBOL) is None
        [status] = engine.promotion_status(SYMBOL)
        assert status["enough_evidence"] is False


class TestPromotion:
    def test_promotes_with_gates_and_shadow_evidence(self, engine: AdaptiveEngine, registry: Registry,
                                                     clock: Clock, events: dict) -> None:
        [cid] = store_run(registry, candidate())
        engine.adopt_latest_run(SYMBOL)
        ps = registry.candidate(cid).param_set_id
        add_trades(registry, clock, ps, edge_rs(29), days=0.1)
        assert engine.check_promotion(SYMBOL) is None  # 29 < 30 trades and only ~3 days
        add_trades(registry, clock, ps, edge_rs(1), days=0.1)
        promo = engine.check_promotion(SYMBOL)
        assert promo is not None
        assert registry.champion(SYMBOL).param_set_id == ps
        row = registry.promotion(promo)
        assert row["action"] == "promote" and "shadow trades" in row["reason"]
        assert "challenger_score" in row["metrics_json"]
        assert events["notify"] and "PROMOTED" in events["notify"][0]

    def test_min_shadow_days_path_needs_floor(self, engine: AdaptiveEngine, registry: Registry,
                                              clock: Clock) -> None:
        [cid] = store_run(registry, candidate())
        engine.adopt_latest_run(SYMBOL)
        ps = registry.candidate(cid).param_set_id
        add_trades(registry, clock, ps, edge_rs(9), days=2.0)  # 18 days but only 9 trades
        assert engine.check_promotion(SYMBOL) is None
        add_trades(registry, clock, ps, edge_rs(1), days=2.0)  # floor of 10 reached
        assert engine.check_promotion(SYMBOL) is not None

    def test_shadow_much_worse_than_backtest_blocks(self, engine: AdaptiveEngine, registry: Registry,
                                                    clock: Clock) -> None:
        [cid] = store_run(registry, candidate(expectancy=1.5))
        engine.adopt_latest_run(SYMBOL)
        add_trades(registry, clock, registry.candidate(cid).param_set_id, [0.05] * 40)
        assert engine.check_promotion(SYMBOL) is None

    def test_negative_shadow_blocks(self, engine: AdaptiveEngine, registry: Registry, clock: Clock) -> None:
        [cid] = store_run(registry, candidate(expectancy=0.0))
        engine.adopt_latest_run(SYMBOL)
        add_trades(registry, clock, registry.candidate(cid).param_set_id, [-0.2, 0.1] * 20)
        assert engine.check_promotion(SYMBOL) is None

    def test_hysteresis_and_cooldown(self, engine: AdaptiveEngine, registry: Registry, clock: Clock) -> None:
        [a] = store_run(registry, candidate(channel=40, score=1.0))
        engine.adopt_latest_run(SYMBOL)
        add_trades(registry, clock, registry.candidate(a).param_set_id, edge_rs(30))
        assert engine.check_promotion(SYMBOL) is not None
        champion_score = engine.champion_score(SYMBOL)
        assert champion_score > 0

        # A challenger only marginally better than the champion: hysteresis blocks it.
        [b] = store_run(registry, candidate(channel=60, score=champion_score * 1.05))
        engine.adopt_latest_run(SYMBOL)
        clock.advance(8)
        add_trades(registry, clock, registry.candidate(b).param_set_id, edge_rs(30), days=0.01)
        assert engine.check_promotion(SYMBOL) is None

        # A much better challenger, but inside the cooldown: blocked; after it: promoted.
        [c] = store_run(registry, candidate(channel=80, score=champion_score * 10))
        engine.adopt_latest_run(SYMBOL)
        last_switch = registry.last_switch_ts(SYMBOL)
        clock.t = last_switch + 1 * DAY
        strong = [3.0, -1.0, 3.0, 3.0, -1.0, 3.0]
        add_trades(registry, clock, registry.candidate(c).param_set_id, strong * 5, days=0.01)
        assert engine.cooldown_left(SYMBOL) > 0
        assert engine.check_promotion(SYMBOL) is None
        clock.t = last_switch + 7.1 * DAY
        assert engine.check_promotion(SYMBOL) is not None
        assert registry.champion(SYMBOL).param_set_id == registry.candidate(c).param_set_id

    def test_bootstrap_days_delay_first_promotion(self, registry: Registry, clock: Clock) -> None:
        eng = AdaptiveEngine(registry, EngineSettings(bootstrap_days=20), clock=clock)
        [cid] = store_run(registry, candidate())
        eng.adopt_latest_run(SYMBOL)
        add_trades(registry, clock, registry.candidate(cid).param_set_id, edge_rs(30))
        assert eng.check_promotion(SYMBOL) is None
        clock.advance(20)
        assert eng.check_promotion(SYMBOL) is not None


class TestDemotion:
    def _promote(self, engine, registry, clock, **kw) -> int:
        [cid] = store_run(registry, candidate(**kw))
        engine.adopt_latest_run(SYMBOL)
        ps = registry.candidate(cid).param_set_id
        add_trades(registry, clock, ps, edge_rs(30))
        assert engine.check_promotion(SYMBOL) is not None
        return ps

    def test_losing_streak_demotes_to_notrade_and_reoptimises(self, engine, registry, clock, events) -> None:
        ps = self._promote(engine, registry, clock, win_rate=0.6)
        add_trades(registry, clock, ps, [-1.0] * 9, mode="live")
        reason = engine.check_degradation(SYMBOL)
        assert reason and "losing streak" in reason
        assert registry.champion(SYMBOL).is_notrade
        assert events["reopt"] and events["reopt"][0][0] == SYMBOL
        assert registry.promotions(1)[0]["action"] == "demote"

    def test_drawdown_budget_demotes(self, engine, registry, clock) -> None:
        ps = self._promote(engine, registry, clock, dd=2.0, win_rate=0.3)
        add_trades(registry, clock, ps, [-1.0, -1.0, 0.5, -1.0, -1.0, 0.5, -1.0, -1.0], mode="live")
        reason = engine.check_degradation(SYMBOL)
        assert reason and "drawdown" in reason  # 5R drawdown > budget 2 x 2R = 4R

    def test_negative_ucb_demotes(self, engine, registry, clock) -> None:
        ps = self._promote(engine, registry, clock, dd=50.0, win_rate=0.2)
        add_trades(registry, clock, ps, [-0.3, -0.2, -0.25, 0.05, -0.3] * 6, mode="shadow")
        reason = engine.check_degradation(SYMBOL)
        assert reason and "upper confidence bound" in reason

    def test_healthy_champion_is_kept(self, engine, registry, clock) -> None:
        ps = self._promote(engine, registry, clock)
        add_trades(registry, clock, ps, edge_rs(20), mode="live")
        assert engine.check_degradation(SYMBOL) is None
        assert registry.champion(SYMBOL).param_set_id == ps

    def test_demotion_switches_to_next_validated_candidate(self, engine, registry, clock) -> None:
        ps = self._promote(engine, registry, clock, win_rate=0.6)
        [other] = store_run(registry, candidate(channel=90, score=2.0))
        engine.adopt_latest_run(SYMBOL)
        other_ps = registry.candidate(other).param_set_id
        add_trades(registry, clock, other_ps, edge_rs(30), days=0.01)
        add_trades(registry, clock, ps, [-1.0] * 9, mode="live")
        engine.check_degradation(SYMBOL)
        assert registry.champion(SYMBOL).param_set_id == other_ps


class TestAuditAndRollback:
    def test_rollback_restores_previous_champion(self, engine, registry, clock) -> None:
        [cid] = store_run(registry, candidate())
        engine.adopt_latest_run(SYMBOL)
        ps = registry.candidate(cid).param_set_id
        add_trades(registry, clock, ps, edge_rs(30))
        promo = engine.check_promotion(SYMBOL)
        add_trades(registry, clock, ps, [-1.0] * 12, mode="live")
        engine.check_degradation(SYMBOL)
        assert registry.champion(SYMBOL).is_notrade
        back = engine.rollback(promo)
        assert registry.champion(SYMBOL).param_set_id == ps
        assert registry.promotion(back)["action"] == "rollback"
        with pytest.raises(KeyError):
            engine.rollback(9999)

    def test_every_decision_is_logged(self, engine, registry, clock) -> None:
        store_run(registry, candidate())
        engine.adopt_latest_run(SYMBOL)
        actions = [r["action"] for r in registry.promotions()]
        assert "optimization" in actions


class TestShadowBook:
    def test_shadow_matches_backtest_trades(self, registry: Registry) -> None:
        bars = synthetic_bars(1500, seed=11, trend=0.15)
        [cid] = store_run(registry, candidate(channel=30))
        cand = registry.candidate(cid)
        book = ShadowBook(registry, SYMBOL, cand)
        start = 400
        for k in range(start, len(bars) + 1):
            book.on_bars(bars[:k], EURUSD_SPEC, TEST_COSTS, 10_000.0, 0.5)
        shadow = [t for t in registry.trades(mode="shadow", param_set_id=cand.param_set_id) if t.status == "closed"]
        sig = S.get("donchian").signals(bars, cand.params)
        expected = [t for t in simulate(bars, sig, EURUSD_SPEC, TEST_COSTS, 10_000.0, 0.5, start=start - 1,
                                        compound=False) if t.exit_reason != "window_end"]
        assert len(shadow) == len(expected) > 5
        for s, e in zip(shadow, expected):
            assert s.side == e.side
            assert s.entry == pytest.approx(e.entry_price)
            assert s.exit == pytest.approx(e.exit_price)
            assert s.r == pytest.approx(e.r)

    def test_shadow_state_survives_restart(self, registry: Registry) -> None:
        bars = synthetic_bars(900, seed=12, trend=0.15)
        [cid] = store_run(registry, candidate(channel=30))
        cand = registry.candidate(cid)
        book = ShadowBook(registry, SYMBOL, cand)
        for k in range(400, 650):
            book.on_bars(bars[:k], EURUSD_SPEC, TEST_COSTS, 10_000.0, 0.5)
        restarted = ShadowBook(registry, SYMBOL, cand)
        assert restarted.last_bar_time == book.last_bar_time
        assert (restarted.pos is None) == (book.pos is None)
        restarted.on_bars(bars[:900], EURUSD_SPEC, TEST_COSTS, 10_000.0, 0.5)  # catch-up after downtime
        assert restarted.last_bar_time == int(bars["time"][899])

    def test_shadow_never_sends_orders(self, registry: Registry) -> None:
        fake = FakeMT5()
        bars = synthetic_bars(900, seed=13, trend=0.15)
        [cid] = store_run(registry, candidate(channel=30))
        book = ShadowBook(registry, SYMBOL, registry.candidate(cid))
        for k in range(400, 900):
            book.on_bars(bars[:k], EURUSD_SPEC, TEST_COSTS, 10_000.0, 0.5)
        assert fake.sends == [] and registry.trades(mode="live") == []


class TestSwitchKeepsPositions:
    def test_open_position_managed_by_original_logic(self, make_cfg, fake_mt5: FakeMT5, waits, tmp_path) -> None:
        from mt5bot.bot import Bot

        bot = Bot(make_cfg(close_on_opposite=True), api=fake_mt5, wait=waits.append, env={},
                  config_path=str(tmp_path / "config.json"), globber=lambda p: [], spawn=_no_spawn)
        bot.startup()
        reg, eng = bot.registry, bot.engine
        [a, b] = store_run(reg, candidate(channel=40), candidate(channel=70))
        cand_a, cand_b = reg.candidate(a), reg.candidate(b)
        reg.record_switch(SYMBOL, "promote", reg.champion(SYMBOL), cand_a, "test", {})
        pos = fake_mt5.add_position(SYMBOL, "BUY", magic=510001, sl=1.09, tp=1.12)
        reg.open_trade("live", SYMBOL, cand_a.param_set_id, "BUY", 1.1, 1.09, 1.12, 0.1, 50.0, ticket=pos.ticket)
        reg.record_switch(SYMBOL, "promote", cand_a, cand_b, "test switch", {})

        calls = []
        original = eng.exit_signal
        eng.exit_signal = lambda s, ps, side, closed: calls.append(ps) or original(s, ps, side, closed)
        bot.run_once()
        fake_mt5.advance_bar(SYMBOL)
        bot.run_once()
        assert calls == [cand_a.param_set_id]  # judged by the strategy that opened it
        assert pos in fake_mt5.positions and (pos.sl, pos.tp) == (1.09, 1.12)
        assert [r for r in fake_mt5.sends if r.get("position") == pos.ticket] == []
        bot.close()


def _no_spawn(*args, **kwargs):
    raise OSError("optimiser launches are disabled in this test")


def test_champion_report_tracks_live_vs_oos(engine: AdaptiveEngine, registry: Registry, clock: Clock) -> None:
    assert engine.champion_report(SYMBOL) == {}
    [cid] = store_run(registry, candidate(expectancy=0.3))
    engine.adopt_latest_run(SYMBOL)
    ps = registry.candidate(cid).param_set_id
    add_trades(registry, clock, ps, edge_rs(30))
    engine.check_promotion(SYMBOL)
    add_trades(registry, clock, ps, [1.0, -1.0, 0.5], mode="live")
    report = engine.champion_report(SYMBOL)
    assert report["live_trades"] == 3 and report["oos_expectancy_r"] == 0.3
    assert report["live_minus_oos_r"] == pytest.approx(0.5 / 3 - 0.3, abs=1e-3)
