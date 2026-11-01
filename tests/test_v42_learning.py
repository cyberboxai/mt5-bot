"""Selection by expected value, entry and scaling gates, the first-trade objective, Flow Doctor v2,
continuous learning, migration and the recommended config."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from conftest import synthetic_bars
from mt5bot import features as fl
from mt5bot import flow, learning, limits
from mt5bot.allocation import AllocationInputs, allocate
from mt5bot.arms import Arm, ArmEngine, ArmSignal, stop_variant_params
from mt5bot.config import Config
from mt5bot.config_migration import apply_migrated, migrate_config_dict
from mt5bot.evidence import ArmSample, EvidenceSettings, Posterior, build_sample, family_of, fit
from mt5bot.expected_value import estimate, expected_value
from mt5bot.gates import EntryRules, cost_to_edge, entry_gate
from mt5bot.profiles import effective_limits
from mt5bot.recommend import recommendation, write_recommended
from mt5bot.registry import Registry
from mt5bot.research import ResearchScheduler, research_order, research_settings, sprint_key
from mt5bot.riskreport import money_view
from mt5bot.slo import FirstTradeClock
from mt5bot.strategies import get as get_strategy
from mt5bot.strategies.base import Signal
from mt5bot.strategies.structure_stop import MAX_ATR, MIN_ATR, structure_distance, swing_distance
from mt5bot.tiers import TierSettings, decide_tier, thompson_select

PROJECT = Path(__file__).resolve().parent.parent


def post(mean: float = 0.1, sd: float = 0.05, n_prior: int = 40, n_obs: int = 0, shrink: float = 0.5) -> Posterior:
    from statistics import NormalDist

    return Posterior(mean, sd, NormalDist().cdf(mean / sd), mean - 1.645 * sd, mean + 1.645 * sd, shrink, -0.03, 0.05,
                     n_obs, n_prior, float(n_obs + n_prior), mean, 1.1)


class TestEntryGates:
    RULES = EntryRules()

    def test_passes_when_every_condition_holds(self) -> None:
        assert entry_gate(post(), 0.05, self.RULES) is None

    @pytest.mark.parametrize("p,cost,code", [
        (post(mean=-0.01), 0.05, "ENTRY_GATE_EDGE"), (post(mean=0.01, sd=0.2), 0.001, "ENTRY_GATE_P"),
        (post(n_prior=29), 0.05, "ENTRY_GATE_EVIDENCE"), (post(mean=0.05), 0.05, "ENTRY_GATE_COST"),
        (None, 0.05, "ENTRY_GATE_EVIDENCE")])
    def test_fails_with_its_reason(self, p: Any, cost: float, code: str) -> None:
        assert entry_gate(p, cost, self.RULES) == code

    def test_cost_ratio_edges(self) -> None:
        assert cost_to_edge(0.1, 0.05) == pytest.approx(2.0) and cost_to_edge(-0.1, 0.05) == 0.0
        assert cost_to_edge(0.1, 0.0) == float("inf")

    def test_tier_one_needs_a_positive_mean_after_costs(self) -> None:
        settings = TierSettings(t1_min_evidence=30)
        assert decide_tier("T0", post(), settings, 1e6, None).tier == "T1"
        assert decide_tier("T0", post(mean=0.0, sd=0.05), settings, 1e6, None).tier == "T0"
        thin = post(n_prior=10)
        assert decide_tier("T0", thin, settings, 1e6, None).tier == "T0"

    def test_entry_gates_config_drives_the_tier(self) -> None:
        cfg = Config.from_dict({"entry_gates": {"min_oos_trades": 50, "min_p": 0.6}})
        s = TierSettings.from_config(cfg.tiers, None, cfg.entry_gates)
        assert s.t1_min_evidence == 50 and s.t1_enter_p == 0.6 and s.t1_exit_p < 0.6

    @pytest.mark.parametrize("data", [{"min_p": 0.51}, {"min_cost_to_edge": 0.9}, {"min_oos_trades": 19}])
    def test_floors_cannot_be_configured_away(self, data: dict) -> None:
        with pytest.raises(ValueError):
            Config.from_dict({"entry_gates": data})


class TestExploration:
    def test_threshold_is_on_the_prior_mean(self) -> None:
        rng = np.random.default_rng(1)
        low = post(mean=-0.05, sd=0.3)
        assert not any(thompson_select(low, rng, -0.02)[0] for _ in range(200))
        ok = post(mean=-0.01, sd=0.1)
        assert any(thompson_select(ok, rng, -0.02)[0] for _ in range(200))


class TestExpectedValue:
    def test_formula(self) -> None:
        assert expected_value(0.5, 2.0, 1.0, 0.1) == pytest.approx(0.4)

    def test_no_data_equals_the_posterior_mean(self) -> None:
        assert estimate(0.08, 1.5, (), 1.0).ev_r == pytest.approx(0.08, abs=1e-6)

    def test_full_shrinkage_ignores_the_arms_own_luck(self) -> None:
        assert estimate(0.0, 1.5, [1.5] * 20, 1.0).ev_r == pytest.approx(0.0, abs=1e-3)

    def test_trusted_data_moves_the_estimate(self) -> None:
        assert estimate(0.0, 1.5, [1.5] * 15 + [-1.0] * 5, 0.2).ev_r > 0.5

    def test_cost_above_the_usual_lowers_it(self) -> None:
        assert estimate(0.08, 1.5, (), 1.0, 0.1).ev_r == pytest.approx(-0.02, abs=1e-6)


def arm(arm_id: int, tier: str, p: Posterior, strategy: str = "ema_cross") -> Arm:
    return Arm(arm_id, "EURUSD", "M15", strategy, {}, arm_id, "default", tier=tier, posterior=p)


def signal_of(a: Arm) -> ArmSignal:
    return ArmSignal(a, Signal("BUY", 0.001, 2.0, 3.0), 1000)


class Recorder:
    def __init__(self) -> None:
        self.rows: list = []

    def __call__(self, stage: str, outcome: str, reason: str, **kw: Any) -> None:
        self.rows.append((stage, outcome, reason, kw.get("arm_id")))

    def blocks(self) -> dict:
        return {r[3]: r[2] for r in self.rows if r[1] == "block"}


class TestSelection:
    def run(self, arms: list[Arm], evs: dict[int, float]) -> tuple[Any, Recorder]:
        rec = Recorder()

        def ev(sig: ArmSignal, view: Any) -> Any:
            return SimpleNamespace(ev_r=evs[sig.arm.id], to_dict=lambda: {})

        inp = AllocationInputs("EURUSD", "M15", [signal_of(a) for a in arms], expected_value=ev)
        return allocate(inp, np.random.default_rng(1), rec), rec

    def test_best_ev_wins_whatever_the_tier(self) -> None:
        intent, _ = self.run([arm(1, "T2", post()), arm(2, "T1", post())], {1: 0.02, 2: 0.2})
        assert intent is not None and intent.arm_id == 2

    def test_negative_ev_is_never_traded_and_says_so(self) -> None:
        intent, rec = self.run([arm(1, "T2", post())], {1: -0.01})
        assert intent is None and rec.blocks() == {1: "EV_NEGATIVE"}

    def test_mix_of_good_and_bad_signals_trades_mostly_the_good(self) -> None:
        rng = np.random.default_rng(5)
        taken_good = taken_bad = 0
        for _ in range(500):
            good = rng.random() < 0.5
            mean = rng.normal(0.15 if good else -0.15, 0.05)
            intent, _ = self.run([arm(1, "T1", post(mean=mean))], {1: mean})
            if intent is not None:
                taken_good += good
                taken_bad += not good
        assert taken_good > 200 and taken_bad < 10

    def test_random_walk_losses_stay_inside_the_trade_budget(self) -> None:
        """Pure noise: no arm passes the entry gates, so only exploration can spend, and it is bounded."""
        from mt5bot.tiers import exploration_room

        rng = np.random.default_rng(9)
        equity, trade_risk, lost_week = 1000.0, 5.0, 0.0
        for _day in range(5):
            lost_day = 0.0
            room = exploration_room([], 0.0, 0, equity, equity, 0.15, 0.5, 4, 12, trade_risk, 5.0, 10.0)
            for _ in range(20):
                if lost_day + trade_risk > room.day_budget or lost_week + trade_risk > room.week_budget:
                    break
                result = trade_risk * (1.5 if rng.random() < 0.4 else -1.0) - 0.2
                lost_day += max(-result, 0.0)
                lost_week += max(-result, 0.0)
        assert lost_week <= 12 * trade_risk * 1.1

    def test_scaling_gates_rarely_promote_noise(self) -> None:
        rng = np.random.default_rng(11)
        settings = TierSettings(t2_enter_p=0.8, t2_min_trades=50)
        promoted = 0
        samples = {}
        for i in range(100):
            rs = rng.normal(-0.05, 1.1, 60)
            samples[i] = build_sample(0, None, None, rs.tolist())
        posts = fit(samples, {i: "x" for i in samples})
        for p in posts.values():
            promoted += decide_tier("T1", p, settings, 1e6, None).tier == "T2"
        assert promoted <= 2


class TestStopPolicies:
    def test_variants_stay_inside_the_strategy_grid_and_keep_reward(self) -> None:
        strat = get_strategy("ema_cross")
        base = strat.default_params()
        variants = stop_variant_params(strat, base)
        assert variants and {v["sl_mult"] for v in variants} <= {1.5, 2.0, 2.5} and all(strat.validate_params(v) for v in variants)
        assert all(v["sl_mult"] != base["sl_mult"] for v in variants)

    def test_strategy_without_a_stop_parameter_has_no_variants(self) -> None:
        assert stop_variant_params(SimpleNamespace(param_space=()), {}) == []

    def test_structure_stop_is_bounded_and_pools_with_its_family(self) -> None:
        bars = synthetic_bars(400, 3)
        from mt5bot import indicators

        atr = indicators.atr(bars["high"], bars["low"], bars["close"], 14)
        d = swing_distance(bars["high"].astype(float), bars["low"].astype(float), bars["close"].astype(float), atr)
        ok = np.isfinite(d) & np.isfinite(atr)
        assert np.all(d[ok] >= MIN_ATR * atr[ok] - 1e-12) and np.all(d[ok] <= MAX_ATR * atr[ok] + 1e-12)
        assert structure_distance(bars) is not None and family_of("structstop_ema_cross") == "ema_cross"

    def test_structure_wrapper_never_looks_ahead(self) -> None:
        strat = get_strategy("structstop_ema_cross")
        bars = synthetic_bars(500, 4)
        full = strat.signals(bars, strat.default_params())
        part = strat.signals(bars[:400], strat.default_params())
        assert np.array_equal(full.side[:400], part.side)
        np.testing.assert_allclose(full.atr[:400], part.atr, equal_nan=True)


class TestFeatures:
    def test_no_lookahead_and_expected_keys(self) -> None:
        bars = synthetic_bars(800, 5)
        a = fl.signal_features(bars, 700, 1, 0.001, 2.0, 0.00001)
        b = fl.signal_features(bars[:701], 700, 1, 0.001, 2.0, 0.00001)
        assert a == b and {"session", "rollover", "spread_stop", "htf", "vol"} <= set(a)

    def test_measured_not_assumed(self) -> None:
        rng = np.random.default_rng(1)
        rows = [({"htf": "with"}, float(rng.normal(0.3, 1.0))) for _ in range(400)]
        rows += [({"htf": "against"}, float(rng.normal(-0.3, 1.0))) for _ in range(400)]
        table = fl.uplift(rows)
        good = next(u for u in table if u.value == "with")
        assert good.validated and good.diff > 0.3
        assert fl.adjustment({"htf": "with"}, table) > 0 > fl.adjustment({"htf": "against"}, table)

    def test_a_feature_without_effect_changes_nothing(self) -> None:
        rng = np.random.default_rng(2)
        rows = [({"htf": v}, float(rng.normal(0.0, 1.0))) for v in ("with", "against") for _ in range(300)]
        assert sum(abs(fl.adjustment({"htf": v}, fl.uplift(rows))) for v in ("with", "against")) < 0.5 * 0.4


class TestSlo:
    def test_only_counts_while_trading_is_possible(self) -> None:
        c = FirstTradeClock()
        c.update(0.0, True, True, 2.0)
        for t in range(600, 4000, 600):
            c.update(float(t), False, True, 2.0)      # market closed
        assert c.eligible_seconds == 0.0
        for t in range(4200, 8000, 600):
            c.update(float(t), True, False, 2.0)      # no eligible arm
        assert c.eligible_seconds == 0.0 and not c.missed

    def test_missed_and_met(self) -> None:
        c = FirstTradeClock()
        now = 0.0
        c.update(now, True, True, 2.0)
        for _ in range(13):
            now += 600.0
            c.update(now, True, True, 2.0)
        assert c.missed and c.remaining_hours(2.0) == 0.0
        c.update(now + 600, True, True, 2.0, last_order_ts=now)
        assert c.met and not c.missed

    def test_first_order_inside_the_window_never_misses(self) -> None:
        c = FirstTradeClock()
        c.update(0.0, True, True, 2.0)
        c.update(3600.0, True, True, 2.0, last_order_ts=1800.0)
        for t in range(4200, 20000, 600):
            c.update(float(t), True, True, 2.0, last_order_ts=1800.0)
        assert c.met and c.snapshot(2.0)["first_order_ts"] == 1800.0

    def test_survives_a_restart_and_a_stopped_bot_is_not_elapsed_time(self) -> None:
        c = FirstTradeClock()
        c.update(0.0, True, True, 2.0)
        c.update(600.0, True, True, 2.0)
        again = FirstTradeClock.from_json(c.to_json())
        again.update(600.0 + 86400.0, True, True, 2.0)
        assert again.eligible_seconds <= 600.0 + 900.0
        assert FirstTradeClock.from_json("garbage").eligible_seconds == 0.0


def flow_inputs(**kw: Any) -> flow.FlowInputs:
    base: dict[str, Any] = dict(now=100_000.0, is_real=True, markets_open=True, last_order_ts=None, started_ts=90_000.0,
                                signals_in_window=20,
                                top_blockers=[{"knob": "entry_cost", "reason": "COST_TO_EDGE", "count": 20, "share": 1.0}],
                                counterfactuals={"COST_TO_EDGE": {"n": 15, "virtual_mean_r": 0.2}}, trades_since={},
                                daily_loss_used_frac=0.0, drawdown_frac=0.0, tier_counts={"T1": 1}, min_agreement_cfg=2)
    base.update(kw)
    return flow.FlowInputs(**base)


class TestFlowDoctorV2:
    CFG = Config().flow_doctor

    def test_reacts_to_a_missed_slo_before_the_idle_limit(self) -> None:
        state = flow.FlowState()
        idle = flow_inputs(started_ts=99_000.0)
        assert flow.evaluate(idle, state, [], self.CFG) == []
        [adj] = flow.evaluate(replace(idle, slo_missed=True), state, [], self.CFG)
        assert adj.knob == "entry_cost" and adj.new == pytest.approx(-0.05)

    def test_slo_reaction_waits_at_most_an_hour_between_steps(self) -> None:
        state = flow.FlowState(last_adjustment_ts=100_000.0 - 3700.0)
        assert flow.evaluate(flow_inputs(slo_missed=True), state, [], self.CFG)

    def test_counterfactual_can_refuse_the_change(self) -> None:
        losing = {"COST_TO_EDGE": {"n": 15, "virtual_mean_r": -0.5}}
        assert flow.evaluate(flow_inputs(slo_missed=True, counterfactuals=losing), flow.FlowState(), [], self.CFG) == []

    def test_entry_gates_never_pass_their_fixed_floors(self) -> None:
        cfg = Config()
        state = flow.FlowState()
        state.knobs.update(entry_cost=-0.20, entry_oos=-10.0, t1_entry_p=-0.03)
        out = flow.overrides(cfg, state)
        assert out["entry_gates.min_cost_to_edge"] >= limits.ENTRY_MIN_COST_TO_EDGE_FLOOR
        assert out["entry_gates.min_oos_trades"] >= limits.ENTRY_MIN_OOS_TRADES_FLOOR
        assert out["entry_gates.min_p"] >= limits.ENTRY_MIN_P_FLOOR

    def test_no_knob_reaches_a_ceiling_margin_or_account_mode(self) -> None:
        cfg = Config()
        state = flow.FlowState()
        for name, (_d, _s, lo, hi, _l) in flow.KNOBS.items():
            state.knobs[name] = lo if lo else hi
        keys = set(flow.overrides(cfg, state))
        forbidden = ("risk", "daily", "drawdown", "margin", "account_mode", "open_positions", "tail.", "kelly")
        assert not [k for k in keys if any(f in k for f in forbidden)]

    def test_timeframe_knob_stays_in_the_bounded_set(self) -> None:
        cfg = Config.from_dict({"universe": {"timeframes": ["M15"]}})
        state = flow.FlowState()
        state.knobs["timeframe_extra"] = 2.0
        assert flow.overrides(cfg, state)["universe.timeframes"] == ("M15", "H1", "M5")

    def test_min_viable_knob_only_acts_when_off(self) -> None:
        blocked = [{"knob": "min_viable_on", "reason": "MIN_LOT_EXCEEDS_RISK", "count": 20, "share": 1.0}]
        cf = {"MIN_LOT_EXCEEDS_RISK": {"n": 15, "virtual_mean_r": 0.2}}
        inp = flow_inputs(top_blockers=blocked, counterfactuals=cf, slo_missed=True)
        assert flow.evaluate(inp, flow.FlowState(), [], self.CFG) == []
        [adj] = flow.evaluate(replace(inp, min_viable_active=False), flow.FlowState(), [], self.CFG)
        assert adj.knob == "min_viable_on"


class TestDrift:
    @pytest.mark.parametrize("kind", ["page_hinkley", "cusum"])
    def test_detects_a_real_fall_quickly(self, kind: str) -> None:
        rng = np.random.default_rng(3)
        d = learning.make_detector(kind, 0.15, 15.0, 30)
        assert not any(d.update(x) for x in rng.normal(0.3, 1.2, 200))
        fired_at = next((i for i, x in enumerate(rng.normal(-0.7, 1.2, 200)) if d.update(x)), None)
        assert fired_at is not None and fired_at < 80

    def test_rare_false_alarms_on_a_stationary_arm(self) -> None:
        rng = np.random.default_rng(7)
        alarms = 0
        for _ in range(100):
            d = learning.make_detector("page_hinkley", 0.15, 15.0, 30)
            alarms += any(d.update(x) for x in rng.normal(0.05, 1.2, 300))
        assert alarms <= 30

    def test_an_arm_getting_better_is_no_alarm(self) -> None:
        d = learning.make_detector("page_hinkley", 0.15, 15.0, 30)
        rng = np.random.default_rng(4)
        assert not any(d.update(x) for x in np.concatenate([rng.normal(0.0, 1.0, 100), rng.normal(1.0, 1.0, 100)]))

    def test_off(self) -> None:
        assert learning.make_detector("off", 0.1, 1.0, 5) is None


@pytest.fixture
def engine(tmp_path: Path):
    from mt5bot.db import db_path

    cfg = Config()
    reg = Registry(db_path(tmp_path))
    now = [1_760_000_000.0]
    drifted: list = []
    eng = ArmEngine(reg, cfg.tiers, cfg.research, clock=lambda: now[0], learning_cfg=cfg.learning,
                    on_drift=lambda arm, detail: drifted.append(arm.id), entry_gates=cfg.entry_gates)
    yield eng, reg, now, drifted
    reg.close()


def add_trades(reg: Registry, arm: Arm, rs: Any, start: float, regime: str = "") -> None:
    for i, r in enumerate(rs):
        tid = reg.open_trade("shadow", arm.symbol, arm.param_set_id, "BUY", 1.1, 1.09, 1.12, 0.1, 10.0, arm_id=arm.id,
                             timeframe=arm.timeframe, open_ts=start + i, tier="T0", open_bar_time=int(start) + i,
                             extra={"signal_bar": int(start) + i, "regime": regime})
        reg.close_trade(tid, 1.1, float(r) * 10, float(r), "sl", close_ts=start + i + 1)


class TestOnlineLearning:
    def make(self, eng: ArmEngine, name: str = "ema_cross") -> Arm:
        return eng.ensure_arm("EURUSD", "M15", name, get_strategy(name).default_params(), "default")

    def test_closed_trade_updates_on_the_next_pass(self, engine) -> None:
        eng, reg, now, _ = engine
        arm = self.make(eng)
        eng.refresh(force=True)
        before = arm.posterior.n_obs
        add_trades(reg, arm, [1.0] * 5, now[0])
        eng._dirty = True          # what on_bar sets when a shadow book closes a trade
        eng.refresh()
        assert eng.arms[arm.id].posterior.n_obs == before + 5

    def test_recent_trades_weigh_more_than_old_ones(self, engine) -> None:
        eng, reg, now, _ = engine
        arm = self.make(eng)
        add_trades(reg, arm, [-1.0] * 50, now[0] - 400 * 86400)
        add_trades(reg, arm, [1.0] * 50, now[0] - 86400)
        eng._ingest_new()
        sample, _ = eng.sample(arm, now[0])
        assert sample.mean > 0.8

    def test_half_life_is_configurable_and_default_is_sixty(self) -> None:
        assert Config().learning.half_life_days == 60.0

    def test_regime_conditioning_changes_the_view(self, engine) -> None:
        eng, reg, now, _ = engine
        arm = self.make(eng)
        add_trades(reg, arm, [1.0] * 60, now[0], "trend")
        add_trades(reg, arm, [-1.0] * 60, now[0] + 100, "range")
        eng.refresh(force=True)
        assert eng.regime_view(arm, "trend").mean > eng.regime_view(arm, "range").mean

    def test_drift_demotes_one_tier_and_asks_for_research(self, engine) -> None:
        eng, reg, now, drifted = engine
        arm = self.make(eng)
        eng.store.change_tier(arm, eng_decision("T2"), now[0])
        add_trades(reg, arm, [1.2] * 60, now[0])
        add_trades(reg, arm, [-1.0] * 60, now[0] + 100)
        eng._ingest_new()
        assert eng._drift_alarms
        now[0] += 1000
        changes = eng.refresh(force=True)
        assert drifted == [arm.id] and any(d.previous == "T2" and d.tier == "T1" for _a, d in changes)
        assert reg.conn.execute("SELECT COUNT(*) AS n FROM drift_events").fetchone()["n"] == 1

    def test_restart_replays_the_history_without_old_alarms(self, engine, tmp_path: Path) -> None:
        eng, reg, now, _ = engine
        arm = self.make(eng)
        add_trades(reg, arm, [1.2] * 60, now[0])
        add_trades(reg, arm, [-1.0] * 60, now[0] + 100)
        cfg = Config()
        again = ArmEngine(reg, cfg.tiers, cfg.research, clock=lambda: now[0], learning_cfg=cfg.learning)
        assert not again._drift_alarms

    def test_hierarchy_shrinks_inside_family_and_group(self) -> None:
        samples = {i: ArmSample(n_obs=100, w=100.0, s1=100.0 * m, s2=100.0 * (1.2 + m * m))
                   for i, m in enumerate((0.3, 0.3, 0.3, -0.3, -0.3, -0.3))}
        families = {i: "ema_cross" for i in samples}
        groups = {i: ("fx|intraday" if i < 3 else "crypto|intraday") for i in samples}
        out = learning.fit_hierarchical(samples, families, groups, EvidenceSettings())
        assert out[0].mean > out[3].mean
        assert all(p.family == "ema_cross" for p in out.values())

    @pytest.mark.parametrize("symbol,cluster", [("EURUSD", "fx"), ("EURUSD.m", "fx"), ("XAUUSD", "metal"),
                                                ("BTCUSD", "crypto"), ("US30", "index")])
    def test_clusters(self, symbol: str, cluster: str) -> None:
        assert learning.symbol_cluster(symbol) == cluster


def eng_decision(tier: str) -> Any:
    from mt5bot.tiers import TierDecision

    return TierDecision(tier, "T0", "test")


class TestModelRegistry:
    def test_versioned_and_restorable(self, engine) -> None:
        eng, reg, now, _ = engine
        arm = eng.ensure_arm("EURUSD", "M15", "ema_cross", get_strategy("ema_cross").default_params(), "default")
        eng.store.set_prior(arm.id, 40, 0.1, 1.0)
        v1 = learning.snapshot_models(reg.conn, "sprint", "snapshot:a", "code1", clock=lambda: now[0])
        eng.store.set_prior(arm.id, 40, -0.5, 1.0)
        v2 = learning.snapshot_models(reg.conn, "nightly", "snapshot:b", "code1", clock=lambda: now[0])
        assert [m.id for m in learning.list_models(reg.conn)] == [v2, v1]
        assert learning.list_models(reg.conn)[0].data_ref == "snapshot:b"
        assert learning.restore_models(reg.conn, v1) == 1
        assert eng.store.get(arm.id).prior_mean == 0.1

    def test_unknown_version_raises(self, engine) -> None:
        _eng, reg, _now, _ = engine
        with pytest.raises(KeyError):
            learning.restore_models(reg.conn, 999)


class TestResearchSchedule:
    def scheduler(self, reg: Registry, now: float = 1_760_000_000.0) -> ResearchScheduler:
        return ResearchScheduler(Config(), reg, reg.path.parent, "config.json", lambda mode: True, clock=lambda: now,
                                 spawn=lambda *a, **k: SimpleNamespace(poll=lambda: None))

    def test_cold_start_is_a_sprint(self, engine) -> None:
        _eng, reg, _now, _ = engine
        assert self.scheduler(reg).due(False)[1] == "sprint"

    def test_sprint_uses_its_reduced_trials_and_full_history(self) -> None:
        cfg = Config()
        assert research_settings(cfg, "M15", "sprint").trials_per_strategy == cfg.startup.sprint_trials

    def test_nightly_runs_once_per_server_day(self, engine) -> None:
        _eng, reg, now, _ = engine
        reg.set_kv("last_research_finished", str(now[0]))
        reg.set_kv("last_full_research", str(now[0]))
        s = self.scheduler(reg, now[0])
        night = float(int(now[0]) // 86400 * 86400 + 23 * 3600 + 60)
        assert s.due(False, night) == ("nightly research on the grown dataset", "nightly")
        assert s.due(False, night + 60) is None
        assert s.due(False, night + 86400)[1] == "nightly"

    def test_feasible_pairs_are_researched_first(self, engine) -> None:
        _eng, reg, _now, _ = engine
        reg.set_kv("affordability", json.dumps({"rows": [{"symbol": "EURUSD", "timeframe": "H1", "feasible": True},
                                                         {"symbol": "XAUUSD", "timeframe": "M15", "feasible": False}]}))
        order = research_order(["XAUUSD", "EURUSD"], ["M15", "H1"], reg)
        assert order[0] == ("EURUSD", "H1")

    def test_sprint_key_changes_with_universe_and_week_not_with_the_day(self) -> None:
        monday = 1_760_313_600.0
        assert sprint_key(["A"], monday) == sprint_key(["A"], monday + 86400)
        assert sprint_key(["A"], monday) != sprint_key(["B"], monday)
        assert sprint_key(["A"], monday) != sprint_key(["A"], monday + 8 * 86400)


OWNER = {
    "symbols": ["EURUSD", "BTCUSD", "XAUUSD"], "timeframe": "M15", "risk_profile": "aggressive", "account_mode": "any",
    "real_money_confirmed": True,
    "universe": {"candidates": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY", "XAUUSD"],
                 "timeframes": ["M5", "M15", "H1"], "max_symbols": 6},
    "tiers": {"exploration_enabled": True, "exploration_slots": 2, "exploration_threshold": -0.05},
    "flow_doctor": {"enabled": True}, "commission_fallback_per_lot": 7.0, "trading_hours_server": [2, 22],
    "claude": {"enabled": False},
}


class TestMigration:
    def test_owner_config_migrates_to_real_and_keeps_every_choice(self) -> None:
        migrated, notices = migrate_config_dict(OWNER)
        cfg = Config.from_dict(migrated)
        assert cfg.account_mode == "real" and cfg.real_money_confirmed is True and cfg.risk_profile == "aggressive"
        assert "BTCUSD" in cfg.universe.candidates and "symbols" not in migrated and "timeframe" not in migrated
        assert cfg.tiers.exploration_threshold == -0.05 and cfg.config_version == "4.2"
        assert any("deprecated" in n for n in notices)

    def test_migration_never_turns_on_real_money(self) -> None:
        migrated, _ = migrate_config_dict({"account_mode": "any", "real_money_confirmed": False})
        assert migrated["account_mode"] == "auto" and migrated["real_money_confirmed"] is False

    @pytest.mark.parametrize("old,new", [("demo_only", "demo"), ("real_only", "real")])
    def test_old_modes(self, old: str, new: str) -> None:
        assert migrate_config_dict({"account_mode": old})[0]["account_mode"] == new

    def test_money_band_is_removed_and_remembered(self) -> None:
        migrated, _ = migrate_config_dict({"tail": {"small_account_equity": 25000}})
        assert "small_account_equity" not in migrated["tail"]
        assert migrated["_legacy_v30"]["small_account_equity"] == 25000
        Config.from_dict(migrated)

    def test_applied_with_a_backup(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(OWNER), encoding="utf-8")
        migrated, _ = migrate_config_dict(OWNER)
        backup = apply_migrated(path, migrated, "20261101")
        assert backup.name == "config.json.bak-20261101" and json.loads(backup.read_text()) == OWNER
        assert Config.load(path).account_mode == "real"
        assert migrate_config_dict(json.loads(path.read_text()))[1] == []

    def test_shipped_config_has_every_new_key(self) -> None:
        data = json.loads((PROJECT / "config.json").read_text(encoding="utf-8"))
        for key in ("account_mode", "real_money_confirmed", "risk_profile", "universe", "sizing", "budget", "startup",
                    "entry_gates", "scaling_gates", "learning", "slo", "capacity"):
            assert key in data


class TestRecommended:
    REPORT = {"equity": 65.0, "currency": "USD", "floor": 20.0, "mode": "min_viable", "rows": [
        {"symbol": "EURUSD", "timeframe": "M5", "feasible": True, "min_lot_risk": 0.41},
        {"symbol": "BTCUSD", "timeframe": "M15", "feasible": True, "min_lot_risk": 1.2},
        {"symbol": "XAUUSD", "timeframe": "M5", "feasible": False, "min_lot_risk": 6.2}]}

    def test_affordable_universe_and_gold_excluded(self) -> None:
        out = recommendation({"risk_profile": "aggressive", "universe": {"candidates": ["XAUUSD"]}}, self.REPORT)
        assert out["universe"]["candidates"] == ["EURUSD", "BTCUSD"] and out["universe"]["timeframes"] == ["M5", "M15"]
        assert out["universe"]["exclude"] == ["XAUUSD"] and out["risk_profile"] == "aggressive"
        Config.from_dict(out)

    def test_written_beside_the_config_and_never_over_it(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        target = write_recommended(path, {"a": 1})
        assert target.name == "config.recommended.json" and path.read_text(encoding="utf-8") == "{}"


class TestRiskView:
    def test_in_money_with_a_zero_edge_scenario(self) -> None:
        lim = effective_limits(Config(risk_profile="aggressive"), True)
        view = money_view(lim, 65.0, "USD", 3.0, None, 0.05, 0.6, paths=200)
        assert view["currency"] == "USD" and [s["name"] for s in view["scenarios"]][0] == "sem vantagem"
        for s in view["scenarios"]:
            assert 0.0 <= s["p_lose_50"] <= s["p_lose_20"] <= s["p_lose_10"] <= 1.0
            assert s["weekly_worst"] <= s["weekly_median"]

    def test_aggressiveness_has_a_visible_price(self) -> None:
        low = money_view(effective_limits(Config(risk_profile="conservative"), True), 1000.0, "USD", 4.0, None, None, 0.0, 300)
        high = money_view(effective_limits(Config(risk_profile="aggressive"), True), 1000.0, "USD", 4.0, None, None, 0.0, 300)
        assert high["scenarios"][0]["weekly_worst"] < low["scenarios"][0]["weekly_worst"]


class TestSharedSignals:
    def test_stop_variants_get_the_same_entries_and_their_own_stop(self) -> None:
        from mt5bot.arms import arm_signals, shared_signals

        strat = get_strategy("ema_cross")
        bars = synthetic_bars(600, 8)
        base = strat.default_params()
        memo: dict = {}
        for i, params in enumerate([base] + stop_variant_params(strat, base)):
            a = Arm(i, "EURUSD", "M15", "ema_cross", params, i, "default")
            got, direct = shared_signals(a, bars, memo), arm_signals(a, bars)
            assert got.sl_mult == direct.sl_mult and got.tp_mult == direct.tp_mult
            assert np.array_equal(got.side, direct.side)
            np.testing.assert_allclose(got.atr, direct.atr, equal_nan=True)
        assert len(memo) == 1

    def test_structure_stops_are_never_shared(self) -> None:
        from mt5bot.arms import arm_signals, shared_signals

        strat = get_strategy("structstop_ema_cross")
        bars = synthetic_bars(600, 8)
        memo: dict = {}
        a = Arm(1, "EURUSD", "M15", "structstop_ema_cross", strat.default_params(), 1, "default")
        np.testing.assert_allclose(shared_signals(a, bars, memo).atr, arm_signals(a, bars).atr, equal_nan=True)
        assert memo == {}
