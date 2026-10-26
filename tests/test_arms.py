"""Arms: shadow books (paper trading identical to the backtester), the arm engine's
bookkeeping, and positions that stay with the arm that opened them."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import EURUSD_SPEC, TEST_COSTS, FakeMT5, synthetic_bars
from mt5bot import strategies as S
from mt5bot.arms import ArmEngine, ShadowBook
from mt5bot.backtest import simulate
from mt5bot.config import Config
from mt5bot.registry import Registry
from mt5bot.strategies.base import SignalArrays
from mt5bot.tiers import TierDecision

PARAMS = {"channel": 30, "sl_mult": 2.0, "tp_mult": 3.0}


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    reg = Registry(tmp_path / "mt5bot.sqlite")
    yield reg
    reg.close()


def engine(registry: Registry, **tiers: object) -> ArmEngine:
    cfg = Config.from_dict({"tiers": tiers}) if tiers else Config()
    return ArmEngine(registry, cfg.tiers, cfg.research, management_cfg=cfg.management)


def feed(book: ShadowBook, bars, start: int, end: int) -> None:
    """One bar at a time, like the live loop. The signals are computed once on the full series and
    sliced: equivalent, because every strategy is causal (tests/test_strategies.py proves it)."""
    full = S.get(book.arm.strategy).signals(bars, book.arm.params)
    for k in range(start, end + 1):
        sig = SignalArrays(full.side[:k], full.atr[:k], full.sl_mult, full.tp_mult)
        book.on_bars(bars[:k], sig, EURUSD_SPEC, TEST_COSTS)


class TestShadowBook:
    def test_shadow_matches_backtest_trades(self, registry: Registry) -> None:
        bars = synthetic_bars(1500, seed=11, trend=0.15)
        arm = engine(registry).ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        book = ShadowBook(registry, arm, 10_000.0, 0.5)
        start = 400
        feed(book, bars, start, len(bars))
        shadow = registry.trades(mode="shadow", status="closed", arm_id=arm.id)
        sig = S.get("donchian").signals(bars, arm.params)
        expected = [t for t in simulate(bars, sig, EURUSD_SPEC, TEST_COSTS, 10_000.0, 0.5, start=start - 1,
                                        compound=False) if t.exit_reason != "window_end"]
        assert len(shadow) == len(expected) > 5
        for s, e in zip(shadow, expected):
            assert s.side == e.side
            assert s.entry == pytest.approx(e.entry_price)
            assert s.exit == pytest.approx(e.exit_price)
            assert s.r == pytest.approx(e.r)
            assert s.tier == "T0" and s.timeframe == "H1" and s.extra["signal_bar"]

    def test_catching_up_in_one_call_gives_the_same_trades(self, registry: Registry, tmp_path: Path) -> None:
        bars = synthetic_bars(1500, seed=11, trend=0.15)
        arm = engine(registry).ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        step = ShadowBook(registry, arm, 10_000.0, 0.5)
        feed(step, bars, 400, len(bars))
        other = Registry(tmp_path / "other.sqlite")
        arm2 = engine(other).ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        bulk = ShadowBook(other, arm2, 10_000.0, 0.5)
        feed(bulk, bars, 400, 400)
        bulk.on_bars(bars, S.get("donchian").signals(bars, arm2.params), EURUSD_SPEC, TEST_COSTS)
        a = [(t.side, round(t.entry, 6), round(t.r, 6)) for t in registry.trades(mode="shadow", status="closed")]
        b = [(t.side, round(t.entry, 6), round(t.r, 6)) for t in other.trades(mode="shadow", status="closed")]
        other.close()
        assert a == b and len(a) > 5

    def test_shadow_state_survives_restart(self, registry: Registry) -> None:
        bars = synthetic_bars(900, seed=12, trend=0.15)
        arm = engine(registry).ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        book = ShadowBook(registry, arm, 10_000.0, 0.5)
        feed(book, bars, 400, 650)
        restarted = ShadowBook(registry, arm, 10_000.0, 0.5)
        assert restarted.last_bar_time == book.last_bar_time
        assert (restarted.pos is None) == (book.pos is None)
        assert restarted.pending == book.pending and restarted.pending_regime == book.pending_regime
        restarted.on_bars(bars, S.get("donchian").signals(bars, arm.params), EURUSD_SPEC, TEST_COSTS)
        assert restarted.last_bar_time == int(bars["time"][-1])

    def test_r_multiples_do_not_depend_on_the_account(self, registry: Registry, tmp_path: Path) -> None:
        bars = synthetic_bars(1200, seed=14, trend=0.15)
        arm = engine(registry).ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        feed(ShadowBook(registry, arm, 10_000.0, 1.0), bars, 400, len(bars))
        small = Registry(tmp_path / "small.sqlite")
        arm2 = engine(small).ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        feed(ShadowBook(small, arm2, 100.0, 0.05), bars, 400, len(bars))  # min lot binds at this size
        big_rs = [t.r for t in registry.trades(mode="shadow", status="closed")]
        small_rs = [t.r for t in small.trades(mode="shadow", status="closed")]
        small.close()
        assert big_rs and len(small_rs) < len(big_rs)  # why sizing uses a fixed reference equity

    def test_shadow_never_sends_orders(self, registry: Registry) -> None:
        fake = FakeMT5()
        bars = synthetic_bars(900, seed=13, trend=0.15)
        arm = engine(registry).ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        feed(ShadowBook(registry, arm, 10_000.0, 0.5), bars, 400, 900)
        assert fake.sends == [] and registry.trades(mode="live") == []


class TestEngine:
    def test_default_arms_are_bounded_and_start_in_shadow(self, registry: Registry) -> None:
        eng = engine(registry)
        created = eng.ensure_default_arms(["EURUSD", "GBPUSD"], ["M15", "H1"], ["donchian", "ema_cross"], 6)
        assert created == 6 and len(eng.arms) == 6
        assert all(a.tier == "T0" and a.policy == "fixed" for a in eng.arms.values())
        assert eng.ensure_default_arms(["EURUSD"], ["M15"], ["donchian"], 100) == 0  # idempotent
        assert {a.strategy for a in eng.arms_for("EURUSD", "M15")} == {"donchian", "ema_cross"}

    def test_on_bar_returns_only_arms_signalling_now(self, registry: Registry) -> None:
        eng = engine(registry)
        arm = eng.ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        bars = synthetic_bars(1200, seed=15, trend=0.15)
        sig = S.get("donchian").signals(bars, arm.params)
        fired = 0
        for k in range(600, 1200):
            out = eng.on_bar("EURUSD", "H1", bars[:k], EURUSD_SPEC, TEST_COSTS)
            expected = int(S.get("donchian").signals(bars[:k], arm.params).side[-1])
            assert [s.signal.side for s in out] == ([] if expected == 0 else ["BUY" if expected > 0 else "SELL"])
            fired += bool(out)
        assert fired > 3 and len(sig.side)

    def test_tier_changes_are_audited_and_pins_are_manual(self, registry: Registry) -> None:
        eng = engine(registry)
        arm = eng.ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        audit = eng.store.change_tier(arm, TierDecision("T1", "T0", "promote: test"), 1_000.0)
        assert arm.tier == "T1" and registry.promotions(1)[0]["id"] == audit
        pin = eng.pin(arm.id, "T0", 24, "operator rollback")
        row = registry.promotions(1)[0]
        assert row["id"] == pin and row["action"] == "manual"
        stored = eng.store.get(arm.id)
        assert stored.tier == "T0" and stored.pinned_until is not None
        with pytest.raises(ValueError):
            eng.pin(arm.id, "T9", 1, "bad tier")
        with pytest.raises(KeyError):
            eng.pin(999_999, "T0", 1, "unknown arm")

    def test_reload_keeps_posteriors_and_drops_inactive_books(self, registry: Registry) -> None:
        eng = engine(registry)
        a = eng.ensure_arm("EURUSD", "H1", "donchian", PARAMS, "test")
        b = eng.ensure_arm("EURUSD", "H1", "ema_cross", S.get("ema_cross").default_params(), "test")
        eng.refresh(force=True)
        assert a.posterior is not None
        eng._book(b)
        eng.store.set_active(b.id, False)
        eng.reload()
        assert b.id not in eng.arms and b.id not in eng.books
        assert eng.arms[a.id].posterior is not None


def test_open_position_is_judged_by_the_arm_that_opened_it(make_bot, fake_mt5: FakeMT5) -> None:
    bot = make_bot(source=None, close_on_opposite=True)
    bot.startup()
    reg, eng = bot.registry, bot.arms
    first = eng.ensure_arm("EURUSD", bot.cfg.timeframe, "donchian", {"channel": 40, "sl_mult": 2.0, "tp_mult": 3.0},
                           "test")
    eng.ensure_arm("EURUSD", bot.cfg.timeframe, "donchian", {"channel": 50, "sl_mult": 2.0, "tp_mult": 3.0}, "test")
    pos = fake_mt5.add_position("EURUSD", "BUY", magic=bot.cfg.magic, sl=1.09, tp=1.12)
    reg.open_trade("live", "EURUSD", first.param_set_id, "BUY", 1.1, 1.09, 1.12, 0.1, 50.0, ticket=pos.ticket,
                   arm_id=first.id, timeframe=bot.cfg.timeframe, tier="T1")
    calls = []
    original = eng.exit_signal
    eng.exit_signal = lambda arm_id, ps, side, closed: calls.append(arm_id) or original(arm_id, ps, side, closed)
    bot.run_once()
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    assert calls == [first.id]  # judged by its own arm, whatever the other arms do
    assert pos in fake_mt5.positions and (pos.sl, pos.tp) == (1.09, 1.12)
    assert [r for r in fake_mt5.sends if r.get("position") == pos.ticket] == []
    bot.close()
