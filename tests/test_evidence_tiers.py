"""Evidence model, tiers, exploration budget and Thompson sampling.

Includes the two property tests the design rests on, run on a fast tier-level simulator
(R outcomes drawn per trade, the real evidence/tier/budget code deciding):

* no edge (random walk plus costs): losses attributable to T1/T2 stay within the exploration
  budget plus the cost drag, and false promotion to T3 is rare;
* a genuine edge: the system promotes it through the tiers and its risk scales up.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from mt5bot.evidence import ArmSample, EvidenceSettings, Posterior, build_sample, family_of, fit, max_drawdown_r
from mt5bot.profiles import PROFILES, effective_limits
from mt5bot.tiers import (BudgetRoom, TierDecision, TierSettings, decide_tier, exploration_room, server_day_start,
                          server_week_start, thompson_select)

ES = EvidenceSettings()


def post(p: float = 0.5, mean: float = 0.0, n: int = 0, prior: int = 0, dd: float = 0.0) -> Posterior:
    return Posterior(mean, 0.1, p, mean - 0.16, mean + 0.16, 0.5, 0.0, 0.05, n, prior, float(n), mean, 1.1, dd)


# -------------------------------------------------------------------- evidence


class TestEvidence:
    def test_sample_combines_discounted_prior_and_observations(self) -> None:
        s = build_sample(100, 0.2, 1.0, [1.0, -1.0, 1.0], [False, True, False], ES)
        assert s.n_obs == 3 and s.n_prior == 100 and s.live_n == 1
        assert s.w == pytest.approx(100 * ES.prior_discount + 3)
        assert s.mean == pytest.approx((30 * 0.2 + 1.0) / 33)

    def test_custom_weights_fade_old_evidence(self) -> None:
        s = build_sample(0, None, None, [5.0, -1.0], None, ES, weights=[0.0, 1.0])
        assert s.mean == pytest.approx(-1.0) and s.n_obs == 2
        assert s.dd_r == pytest.approx(1.0)

    def test_no_data_sits_at_the_skeptical_family_mean(self) -> None:
        posts = fit({1: ArmSample(), 2: ArmSample()}, {1: "a", 2: "a"}, ES)
        assert posts[1].mean == pytest.approx(ES.global_mean, abs=1e-9)
        assert posts[1].p_positive < 0.5 and posts[1].shrinkage == 1.0

    def test_strong_own_evidence_separates_from_the_family(self) -> None:
        rng = np.random.default_rng(1)
        samples = {i: build_sample(0, None, None, rng.choice([1.45, -1.05], 150, p=[0.4, 0.6]).tolist(), None, ES)
                   for i in range(12)}
        samples[99] = build_sample(0, None, None, rng.choice([1.45, -1.05], 400, p=[0.6, 0.4]).tolist(), None, ES)
        posts = fit(samples, {k: "f" for k in samples}, ES)
        null_best = max(posts[i].p_positive for i in range(12))
        assert posts[99].p_positive > 0.9 > null_best
        assert posts[99].mean < samples[99].mean  # still shrunk toward the family

    def test_hierarchical_shrinkage_protects_against_lucky_arms(self) -> None:
        """Among many no-edge arms the luckiest one is pulled back toward the family mean."""
        rng = np.random.default_rng(2)
        samples = {i: build_sample(0, None, None, rng.choice([1.45, -1.05], 60, p=[0.4, 0.6]).tolist(), None, ES)
                   for i in range(40)}
        posts = fit(samples, {k: "f" for k in samples}, ES)
        luckiest = max(samples, key=lambda k: samples[k].mean)
        assert samples[luckiest].mean > 0.2
        assert posts[luckiest].mean < samples[luckiest].mean / 2
        assert posts[luckiest].p_positive < 0.80  # never T2 material on luck alone

    def test_posterior_interval_and_kelly(self) -> None:
        p = fit({1: build_sample(0, None, None, [1.5, -1.0] * 100, None, ES)}, {1: "x"}, ES)[1]
        assert p.lo < p.mean < p.hi and 0 < p.p_positive < 1
        assert p.kelly_fraction == pytest.approx(p.mean / (p.sigma ** 2 + p.mean ** 2))
        assert post(mean=-0.1).kelly_fraction == 0.0

    def test_family_and_drawdown_helpers(self) -> None:
        assert family_of("volfilter_donchian") == "donchian" and family_of("ema_cross") == "ema_cross"
        assert max_drawdown_r([1, -1, -1, 2, -3]) == pytest.approx(3.0)
        assert max_drawdown_r([]) == 0.0


# ----------------------------------------------------------------------- tiers


TS = TierSettings(promote_cooldown_hours=4)


class TestTiers:
    def test_promotion_is_one_step_and_needs_evidence(self) -> None:
        assert decide_tier("T0", post(0.9, 0.2, n=0, prior=0), TS, 1e6, None).tier == "T0"  # no own evidence
        d = decide_tier("T0", post(0.9, 0.2, n=0, prior=60), TS, 1e6, None)
        assert d.tier == "T1" and d.direction == "up" and "walk-forward" in d.reason
        assert decide_tier("T1", post(0.95, 0.2, n=10), TS, 1e6, None).tier == "T1"  # too few trades for T2
        assert decide_tier("T1", post(0.95, 0.2, n=TS.t2_min_trades), TS, 1e6, None).tier == "T2"
        assert decide_tier("T2", post(0.99, 0.3, n=TS.t3_min_trades), TS, 1e6, None).tier == "T3"

    def test_hysteresis_and_cooldown(self) -> None:
        between = (TS.t2_exit_p + TS.t2_enter_p) / 2
        assert decide_tier("T2", post(between, n=80), TS, 1e6, None).tier == "T2"   # stays: hysteresis
        assert decide_tier("T1", post(between, n=80), TS, 1e6, None).tier == "T1"   # not yet promoted
        recent = 1e6 - 3600
        assert decide_tier("T1", post(0.99, n=80), TS, 1e6, recent).reason == "promotion cooldown"
        assert decide_tier("T1", post(0.99, n=80), TS, 1e6 + 4 * 3600, recent).tier == "T2"

    def test_demotion_is_immediate_even_in_cooldown_or_pinned(self) -> None:
        d = decide_tier("T2", post(TS.t2_exit_p - 0.01, n=80), TS, 1e6, 1e6 - 1, pinned_until=1e9)
        assert d.tier == "T1" and d.direction == "down"
        assert decide_tier("T1", post(0.4, n=80), TS, 1e6, 1e6).tier == "T0"

    def test_drawdown_budgets_demote(self) -> None:
        d = decide_tier("T3", post(0.99, 0.3, n=200, dd=TS.t3_max_dd_r + 1), TS, 1e6, None)
        assert d.tier == "T2" and "drawdown" in d.reason
        d = decide_tier("T2", post(0.99, 0.3, n=80), TS, 1e6, None, dd_since_change_r=TS.t2_max_dd_r + 0.5)
        assert d.tier == "T1" and "since promotion" in d.reason

    def test_pins_block_promotions(self) -> None:
        assert decide_tier("T0", post(0.9, n=50), TS, 1e6, None, pinned_until=2e6).tier == "T0"


# ----------------------------------------------------------------- exploration


def row(status: str, pnl: float = 0.0, close_ts: float = 0.0, risk: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(status=status, pnl=pnl, close_ts=close_ts, risk_money=risk)


class TestExplorationBudget:
    NOW = 1_767_571_200 + 3 * 86400 + 12 * 3600  # a Thursday midday (server time)

    def test_day_and_week_windows(self) -> None:
        assert server_day_start(self.NOW) == self.NOW - 12 * 3600
        assert server_week_start(self.NOW) == 1_767_571_200  # Monday 00:00

    def test_losses_open_risk_and_wins(self) -> None:
        day0 = server_day_start(self.NOW)
        rows = [row("closed", -6.0, day0 + 60), row("closed", -10.0, day0 - 86400), row("open", risk=5.0),
                row("closed", +4.0, day0 + 120)]
        room = exploration_room(rows, self.NOW, 0, 10_000, 10_000, 0.2, 0.5)
        assert room.day_budget == pytest.approx(20.0) and room.week_budget == pytest.approx(50.0)
        assert room.day_used == pytest.approx(2.0)          # -6 + 4 today
        assert room.week_used == pytest.approx(12.0)        # -16 + 4 this week
        assert room.room == pytest.approx(20.0 - 2.0 - 5.0)  # the tighter window, minus open risk
        assert room.allows(13.0) and not room.allows(13.5)

    def test_wins_never_create_more_than_the_budget(self) -> None:
        rows = [row("closed", +500.0, server_day_start(self.NOW) + 1)]
        room = exploration_room(rows, self.NOW, 0, 10_000, 10_000, 0.2, 0.5)
        assert room.room == pytest.approx(20.0)

    def test_spent_budget_has_no_room(self) -> None:
        rows = [row("closed", -25.0, server_day_start(self.NOW) + 1)]
        assert exploration_room(rows, self.NOW, 0, 10_000, 10_000, 0.2, 0.5).room == 0.0
        assert BudgetRoom(10, 10, 10, 0, 0).day_fraction == 1.0

    def test_thompson_prefers_uncertain_plausible_arms(self) -> None:
        rng = np.random.default_rng(3)
        hopeful = Posterior(-0.02, 0.10, 0.42, -0.2, 0.15, 1.0, -0.03, 0.08, 0, 0, 0.0, 0.0, 1.1)
        refuted = Posterior(-0.30, 0.03, 0.0, -0.35, -0.25, 0.2, -0.03, 0.08, 300, 0, 300.0, -0.3, 1.1)
        picks_h = sum(thompson_select(hopeful, rng)[0] for _ in range(2000))
        picks_r = sum(thompson_select(refuted, rng)[0] for _ in range(2000))
        assert 500 < picks_h < 1100 and picks_r == 0


# ------------------------------------------------------------ property tests


def tier_world(seed: int, edges: dict[int, float], n_arms: int = 120, days: int = 60, per_day: float = 1.5,
               payoff: float = 1.5, cost_r: float = 0.05, profile: str = "balanced", is_real: bool = False):
    """Fast tier-level world: every arm signals ~per_day times a day; shadow books record every
    signal; T0 arms may get budgeted exploration trades (Thompson); T1+ arms trade at their tier.
    Returns per-tier realised P&L (in % of equity) and the tier history."""
    from mt5bot.config import Config

    rng = np.random.default_rng(seed)
    lim = effective_limits(Config(risk_profile=profile), is_real)
    settings = TierSettings(promote_cooldown_hours=0)
    families = {i: f"f{i % 12}" for i in range(n_arms)}
    obs: dict[int, list[float]] = {i: [] for i in range(n_arms)}
    tiers = {i: "T0" for i in range(n_arms)}
    max_tier = dict(tiers)
    pnl = {"T1": 0.0, "T2": 0.0, "T3": 0.0}
    t1_trades = t2_trades = 0
    post_map: dict[int, Posterior] = {}
    order = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    for day in range(days):
        if day % 5 == 0:
            week_loss = 0.0
        day_loss = 0.0
        open_slots = 0
        for i in rng.permutation(n_arms):
            for _ in range(rng.poisson(per_day)):
                # ``edge`` is the expectancy BEFORE costs: a random walk has none, so after the
                # spread/commission drag every no-edge arm loses ``cost_r`` per trade on average.
                edge = edges.get(int(i), 0.0)
                p_win = (edge + 1.0) / (payoff + 1.0)
                r = (payoff if rng.random() < p_win else -1.0) - cost_r
                obs[i].append(r)
                tier = tiers[i]
                post_i = post_map.get(int(i))
                explore = False
                if tier == "T0" and post_i is not None and open_slots < 2:
                    explore = thompson_select(post_i, rng)[0]
                if tier == "T0" and not explore:
                    continue
                trade_tier = "T1" if explore else tier
                risk = lim.tier_risk_pct(trade_tier)
                if trade_tier == "T1":
                    if day_loss + risk > lim.explore_day_pct or week_loss + risk > lim.explore_week_pct:
                        continue
                    t1_trades += 1
                else:
                    t2_trades += 1
                result = risk * r
                pnl[trade_tier] += result
                if trade_tier == "T1" and result < 0:
                    day_loss -= result
                    week_loss -= result
        samples = {i: build_sample(0, None, None, obs[i], None, ES) for i in range(n_arms)}
        post_map = fit(samples, families, ES)
        for i in range(n_arms):
            tiers[i] = decide_tier(tiers[i], post_map[i], settings, day * 86400.0, None).tier
            if order[tiers[i]] > order[max_tier[i]]:
                max_tier[i] = tiers[i]
    return pnl, max_tier, t1_trades, t2_trades, lim


class TestRandomWalkSafety:
    @pytest.mark.parametrize("seed", [11, 12, 13, 14, 15, 16])
    def test_no_edge_losses_are_bounded_and_t3_is_rare(self, seed: int) -> None:
        days = 60
        pnl, max_tier, t1, t2, lim = tier_world(seed, {}, days=days)
        weeks = math.ceil(days / 5)
        budget = lim.explore_week_pct * weeks
        cost_drag = 0.05 * (t1 * lim.t1_risk_pct + t2 * lim.t2_risk_pct)
        loss = -(pnl["T1"] + pnl["T2"] + pnl["T3"])
        # Bounded: T1 can never lose more than the weekly budgets; promoted no-edge arms only add
        # their cost drag plus noise, which the tiers' exit thresholds cut short.
        assert -pnl["T1"] <= budget + 1e-9
        # Two standard deviations of the noise of the (few) promoted no-edge trades (R sd ~1.25).
        noise = 2.0 * 1.25 * lim.t2_risk_pct * math.sqrt(t2)
        assert loss <= budget + cost_drag + noise, (pnl, t1, t2)
        assert sum(1 for t in max_tier.values() if t == "T2") <= 6       # at most 5% of 120 no-edge arms
        assert sum(1 for t in max_tier.values() if t == "T3") == 0       # no false T3

    def test_false_t3_rate_across_many_worlds(self) -> None:
        t3 = sum(sum(1 for t in tier_world(seed, {}, n_arms=80, days=40)[1].values() if t == "T3")
                 for seed in range(30, 40))
        assert t3 / (80 * 10) < 0.002


class TestGenuineEdge:
    def test_edge_is_promoted_and_risk_scales_up(self) -> None:
        edges = {7: 0.40, 19: 0.40}
        promoted = 0
        for seed in (21, 22, 23):
            pnl, max_tier, _t1, t2, lim = tier_world(seed, edges, n_arms=60, days=80, per_day=2.5)
            promoted += sum(1 for a in edges if max_tier[a] in ("T2", "T3"))
            assert lim.t2_risk_pct > lim.t1_risk_pct  # promotion means a larger risk per trade
        assert promoted >= 4  # most genuine edges reach T2/T3 within 80 days


class TestProfiles:
    def test_profiles_are_ordered_and_below_ceilings(self) -> None:
        c, b, a = (PROFILES[n] for n in ("conservative", "balanced", "aggressive"))
        assert c.t3_risk_pct < b.t3_risk_pct < a.t3_risk_pct
        assert c.daily_loss_pct < b.daily_loss_pct < a.daily_loss_pct
        assert c.lock_fraction > b.lock_fraction > a.lock_fraction

    def test_real_accounts_risk_less_on_exploration(self) -> None:
        from mt5bot.config import Config

        for name in PROFILES:
            cfg = Config(risk_profile=name)
            assert effective_limits(cfg, True).t1_risk_pct <= effective_limits(cfg, False).t1_risk_pct
            assert effective_limits(cfg, True).explore_day_pct <= effective_limits(cfg, False).explore_day_pct

    def test_user_caps_only_tighten(self) -> None:
        from mt5bot.config import Config

        loose = effective_limits(Config(risk_per_trade_pct_demo=2.0, max_daily_loss_pct_demo=5.0), False)
        assert loose.t3_risk_pct == PROFILES["balanced"].t3_risk_pct
        assert loose.daily_loss_pct == PROFILES["balanced"].daily_loss_pct
        tight = effective_limits(Config(risk_per_trade_pct_demo=0.2), False)
        assert tight.t3_risk_pct == 0.2 and tight.t2_risk_pct == 0.2 and tight.t1_risk_pct <= 0.2


def test_tier_decision_direction() -> None:
    assert TierDecision("T2", "T1").direction == "up" and TierDecision("T1", "T2").direction == "down"
    assert TierDecision("T1", "T1").direction is None
