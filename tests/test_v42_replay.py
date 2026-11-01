"""Replay of a SIMULATED session with the owner's specifications and stop distances.

This is not the owner's recorded day: that data does not exist on the development machine, and
``docs/DIAGNOSIS_v4_2.md`` says so. These tests prove the mechanism only: where the previous rule
could not place a single trade, this version places trades. The R results come from synthetic bars
and say nothing about real markets. On the Windows machine ``--replay-day`` runs this same code on
the cached bars.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from conftest import make_rates
from mt5bot.arms import ArmStore
from mt5bot.config import Config
from mt5bot.db import db_path
from mt5bot.evidence import Posterior
from mt5bot.profiles import effective_limits
from mt5bot.registry import Registry
from mt5bot.replay_day import format_report, replay_day
from mt5bot.risk import SymbolSpec
from mt5bot.slo import FirstTradeClock
from mt5bot.strategies import get as get_strategy

DAY_START = 1_761_004_800                          # 2025-10-21 00:00 UTC
EURUSD = SymbolSpec("EURUSD", 0.00001, 5, 1.0, 0.00001, 100_000, 0.01, 100.0, 0.01)
XAUUSD = SymbolSpec("XAUUSD", 0.01, 2, 1.0, 0.01, 100, 0.01, 50.0, 0.01)
BAR = 900


def session(seed: int, n: int = 1500, price: float = 1.16, vol: float = 0.00012) -> np.ndarray:
    """A choppy synthetic session whose stops match the owner's M15 distances (about 5 to 10 pips)."""
    rng = np.random.default_rng(seed)
    closes = price * np.exp(np.cumsum(rng.normal(0.0, vol, n) + vol * np.sin(np.arange(n) / 9.0)))
    return make_rates(closes, start_time=DAY_START - (n - 90) * BAR, half_range=price * vol, spread=8)


def positive(mean: float = 0.12) -> Posterior:
    return Posterior(mean, 0.05, 0.99, mean - 0.08, mean + 0.08, 0.5, 0.0, 0.05, 0, 60, 60.0, mean, 1.1)


@pytest.fixture
def registry(tmp_path: Path):
    reg = Registry(db_path(tmp_path))
    yield reg
    reg.close()


def seed_arm(reg: Registry, symbol: str, spec: SymbolSpec, bars: np.ndarray, tier: str = "T1", post: Any = None) -> Any:
    reg.store_spec(symbol, spec.to_json(), 0.0)
    reg.store_bars(symbol, "M15", bars)
    store = ArmStore(reg)
    arm = store.upsert(symbol, "M15", "ema_cross", get_strategy("ema_cross").default_params(), "default")
    arm.tier, arm.posterior = tier, post if post is not None else positive()
    return arm


AGGRESSIVE = Config(risk_profile="aggressive")
DAY = datetime.fromtimestamp(DAY_START, tz=timezone.utc).date()


@pytest.mark.parametrize("equity", [65.09, 110.0])
def test_the_new_rule_trades_where_the_old_one_could_not(registry: Registry, equity: float) -> None:
    arm = seed_arm(registry, "EURUSD", EURUSD, session(3))
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, equity, {"EURUSD": 116_000.0}, [arm])
    assert report.signals > 0 and report.previous_rule_trades == 0
    assert report.trades and all(t.risk_pct <= 2.0 and t.volume == 0.01 for t in report.trades)
    assert all(t.min_viable for t in report.trades)
    assert "Regra anterior teria aceite: 0" in format_report(report, "USD")


def test_gold_is_skipped_with_a_reason(registry: Registry) -> None:
    bars = session(4, price=4000.0, vol=0.001)
    arm = seed_arm(registry, "XAUUSD", XAUUSD, bars)
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, 110.0, {"XAUUSD": 400_000.0}, [arm])
    assert not report.trades and report.signals > 0 and report.skipped.get("MIN_LOT_ABOVE_CAP", 0) > 0


def test_an_arm_without_edge_never_trades_even_when_it_fits(registry: Registry) -> None:
    arm = seed_arm(registry, "EURUSD", EURUSD, session(3), tier="T0", post=positive(-0.05))
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, 110.0, {"EURUSD": 116_000.0}, [arm])
    assert not report.trades and report.signals > 0


def test_one_position_per_symbol_at_a_time(registry: Registry) -> None:
    arm = seed_arm(registry, "EURUSD", EURUSD, session(3))
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, 110.0, {"EURUSD": 116_000.0}, [arm])
    spans = sorted((t.entry_time, t.exit_time) for t in report.trades)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_first_trade_inside_the_objective_in_a_simulated_session(registry: Registry) -> None:
    """Market active from the first bar, an eligible arm, the first accepted signal is the first order."""
    arm = seed_arm(registry, "EURUSD", EURUSD, session(3))
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, 110.0, {"EURUSD": 116_000.0}, [arm])
    first = report.trades[0].entry_time
    clock = FirstTradeClock()
    for t in range(DAY_START, first + BAR, BAR):
        clock.update(float(t), True, True, 8.0, last_order_ts=float(first) if t >= first else None)
    assert clock.met and not clock.missed


def test_notional_ceiling_applies_in_the_replay_too(registry: Registry) -> None:
    arm = seed_arm(registry, "EURUSD", EURUSD, session(3))
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, 60.0, {"EURUSD": 200_000.0}, [arm])
    assert not report.trades and report.skipped.get("NOTIONAL_CAP", 0) > 0


def test_arms_signalling_on_the_same_bar_get_one_entry(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a trade that opens and closes inside one bar left exit == entry, so eight arms entered at once."""
    from types import SimpleNamespace

    from mt5bot import replay_day as module

    arm = seed_arm(registry, "EURUSD", EURUSD, session(3))
    twin = seed_arm(registry, "EURUSD", EURUSD, session(3))
    twin.id = arm.id + 1000
    entry = DAY_START + 3600
    same_bar = SimpleNamespace(entry_time=entry, exit_time=entry, risk_money=70.0, volume=1.0, entry_price=1.16, sl=1.1593,
                               side="BUY", r=-1.0)
    monkeypatch.setattr(module, "simulate", lambda *a, **k: [same_bar])
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, 110.0, {"EURUSD": 116_000.0}, [arm, twin])
    assert len(report.trades) == 1 and report.skipped.get("ONE_PER_SYMBOL") == 1


def test_open_risk_limit_holds_across_symbols(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from mt5bot import replay_day as module

    arms = [seed_arm(registry, name, replace(EURUSD, name=name), session(3)) for name in ("EURUSD", "GBPUSD", "AUDUSD")]
    for i, a in enumerate(arms):
        a.id += i * 1000
    entry = DAY_START + 3600
    long_trade = SimpleNamespace(entry_time=entry, exit_time=entry + 7200, risk_money=98.0, volume=1.0, entry_price=1.16,
                                 sl=1.15, side="BUY", r=1.0)
    monkeypatch.setattr(module, "simulate", lambda *a, **k: [long_trade])
    lim = effective_limits(AGGRESSIVE, True)
    report = replay_day(AGGRESSIVE, registry, lim, DAY, 65.09, None, arms)
    assert sum(t.risk_money for t in report.trades) <= 65.09 * lim.max_open_risk_pct / 100.0
    assert report.skipped.get("OPEN_RISK", 0) >= 1
