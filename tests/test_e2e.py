"""Synthetic end-to-end lifecycle with a fake MT5 that replays H1 data and fills SL/TP.

Cold start (arms in shadow, exploration with micro risk from the first bars) -> research
priors -> live evidence with a genuine edge -> tier promotion and larger risk -> adverse
regime -> demotion. The audit log and the funnel are asserted at the end.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from conftest import FakeMT5, synthetic_bars
from mt5bot.bot import Bot
from mt5bot.config import Config
from mt5bot.db import db_path
from mt5bot.research import run_research

H1 = 3600


class ReplayMT5(FakeMT5):
    """Fake terminal on H1 bars that fills server-side SL/TP like a broker would."""

    def __init__(self, history: np.ndarray) -> None:
        super().__init__()
        self.rates["EURUSD"] = history.copy()
        self._deal = 10_000
        self._sync_tick()

    def _sync_tick(self) -> None:
        last = self.rates["EURUSD"][-1]
        self.set_tick("EURUSD", bid=float(last["close"]), time=int(last["time"]) + 60)

    def copy_rates_from_pos(self, name: str, timeframe: int, start: int, count: int) -> Any:
        assert timeframe == self.TIMEFRAME_H1
        return super().copy_rates_from_pos(name, timeframe, start, count)

    def push_bar(self, o: float, h: float, lo: float, c: float) -> None:
        """The previously forming bar closes with these prices; a new bar starts forming."""
        rates = self.rates["EURUSD"]
        rates[-1]["open"], rates[-1]["high"], rates[-1]["low"], rates[-1]["close"] = o, h, lo, c
        self._fill_stops(rates[-1])
        new = rates[-1:].copy()
        new["time"] += H1
        new["open"] = new["high"] = new["low"] = new["close"] = c
        self.rates["EURUSD"] = np.concatenate([rates, new])
        self._sync_tick()
        self.account.equity = self.account.balance

    def _fill_stops(self, bar: Any) -> None:
        spread = 10 * 0.00001
        for p in list(self.positions):
            buy = p.type == self.POSITION_TYPE_BUY
            hit_sl = (bar["low"] <= p.sl) if buy else (bar["high"] + spread >= p.sl)
            hit_tp = (bar["high"] >= p.tp) if buy else (bar["low"] + spread <= p.tp)
            if not p.sl or not (hit_sl or hit_tp):
                continue
            price = p.sl if hit_sl else p.tp
            profit = (price - p.price_open) * (1 if buy else -1) * p.volume * 100_000
            self._deal += 1
            self.deals.append(SimpleNamespace(position_id=p.ticket, entry=self.DEAL_ENTRY_OUT, price=price,
                                              profit=profit, commission=-7.0 * p.volume, swap=0.0, fee=0.0,
                                              symbol=p.symbol, volume=p.volume, type=1, ticket=self._deal))
            self.account.balance += profit - 7.0 * p.volume
            self.positions.remove(p)


class InlineProcess:
    """Stands in for the low-priority research subprocess: runs the cycle synchronously."""

    def __init__(self, cfg: Config, state_dir: Path, trigger: str, mode: str) -> None:
        from mt5bot.registry import Registry

        reg = Registry(db_path(state_dir))
        try:
            run_research(cfg, reg, trigger, mode)
        finally:
            reg.close()

    def poll(self) -> int:
        return 0


def _scaled(live: np.ndarray, anchor: float) -> tuple[np.ndarray, np.ndarray]:
    close = live["close"] / live["close"][0] * anchor
    return close, close / live["close"]


@pytest.mark.slow
def test_lifecycle_exploration_research_promotion_and_demotion(tmp_path: Path) -> None:
    history = synthetic_bars(24 * 200, seed=201, trend=0.15)
    cfg = Config.from_dict({
        "symbols": ["EURUSD"], "timeframe": "H1", "bars": 600, "trading_hours_server": [0, 24],
        "state_root": str(tmp_path / "state"), "reports_dir": str(tmp_path / "reports"),
        "health_file": str(tmp_path / "health.json"), "kill_switch_file": str(tmp_path / "KILL"),
        "log_file": str(tmp_path / "bot.log"), "enabled_strategies": ["donchian", "ema_cross"],
        "trials_per_strategy": 5, "commission_per_lot": 7.0, "history_months": 6,
        "universe": {"candidates": ["EURUSD"], "timeframes": ["H1"], "max_symbols": 1},
        "research": {"startup_trials": 5, "startup_history_months": 6},
        "tiers": {"promote_cooldown_hours": 1.0}, "team": {"team_mode": "baseline"},
    })
    fake = ReplayMT5(history)
    clock = {"t": float(history["time"][-1]) + 60}
    utc = lambda: clock["t"]  # noqa: E731
    launches: list[str] = []

    def spawn(args: list[str], **kwargs: Any) -> InlineProcess:
        trigger = args[args.index("--trigger") + 1]
        launches.append(trigger)
        return InlineProcess(cfg, Path(args[args.index("--state-dir") + 1]), trigger,
                             args[args.index("--research-mode") + 1])

    bot = Bot(cfg, api=fake, wait=lambda s: None, env={}, monotonic=utc, utc_now=utc,
              config_path=str(tmp_path / "config.json"), spawn=spawn, globber=lambda p: [], durable_files=False)
    bot.startup()
    reg = bot.registry
    assert bot.arms.counts()["T0"] == 2  # one default-parameter arm per enabled strategy, in shadow

    # Phase 1: cold start launches research at once; its priors arrive on the next pass.
    bot.run_once()
    assert launches and launches[0].startswith("cold start")
    bot._next_housekeeping = 0
    bot.run_once()
    assert any(a.prior_n > 0 for a in bot.arms.arms.values()), "research must give the arms a prior"

    # Phase 2: the edge persists live; the bot trades from the first hours (exploration or
    # prior-qualified T1) and evidence lifts at least one arm to T2.
    live = synthetic_bars(24 * 150, seed=202, trend=0.15)
    close, scale = _scaled(live, float(history["close"][-1]))
    first_trade_hour = promoted_hour = None
    for i in range(len(live)):
        clock["t"] += H1
        fake.push_bar(float(live["open"][i] * scale[i]), float(live["high"][i] * scale[i]),
                      float(live["low"][i] * scale[i]), float(close[i]))
        bot.run_once()
        if first_trade_hour is None and reg.trades(mode="live"):
            first_trade_hour = i
        if any(a.tier in ("T2", "T3") for a in bot.arms.arms.values()):
            promoted_hour = i
            break
    assert first_trade_hour is not None and first_trade_hour < 48, "trading must start within the first two days"
    assert promoted_hour is not None, "a genuine edge must be promoted to T2 by evidence"
    promoted = next(a for a in bot.arms.arms.values() if a.tier in ("T2", "T3"))
    t1_risk = [t.risk_pct for t in reg.trades(mode="live") if t.tier == "T1" and t.risk_pct]
    assert t1_risk and max(t1_risk) <= bot.risk.limits.t1_risk_pct + 1e-6

    # Phase 3: an adverse regime for the promoted arm. Every trade it takes, live or in its
    # shadow book (the evidence counts both), is driven to its stop loss; when it is flat the
    # price swings so that breakouts keep triggering new (losing) entries.
    price = float(fake.rates["EURUSD"][-1]["close"])
    demoted = False
    for _ in range(24 * 120):
        clock["t"] += H1
        pos = next((p for p in fake.positions if p.magic == cfg.magic), None)
        mine = reg.trades(status="open", arm_id=promoted.id)
        if mine:
            buy = mine[-1].side == "BUY"
            o, c = price, (mine[-1].sl - 0.0002 if buy else mine[-1].sl + 0.0002)
        elif pos is not None:
            buy = pos.type == fake.POSITION_TYPE_BUY
            o, c = price, (pos.sl - 0.0002 if buy else pos.sl + 0.0002)
        else:
            o, c = price, price * (1 + 0.004 * (1 if (int(clock["t"]) // (H1 * 48)) % 2 else -1))
        fake.push_bar(o, max(o, c) + 0.0003, min(o, c) - 0.0003, c)
        price = c
        bot._next_housekeeping = 0
        bot.run_once()
        if bot.arms.arms[promoted.id].tier not in ("T2", "T3"):
            demoted = True
            break
    assert demoted, "losing evidence must demote the arm"
    actions = [r["action"] for r in reversed(reg.promotions(500))]
    assert "tier_up" in actions and "tier_down" in actions
    down = next(r for r in reg.promotions(500) if r["action"] == "tier_down")
    assert down["reason"].startswith("demote") and json.loads(down["metrics_json"])["arm_id"]
    live_rows = reg.trades(mode="live")
    assert live_rows and all(t.sl and t.tp for t in live_rows)
    assert all(t.r is not None for t in live_rows if t.status == "closed")
    bot.funnel.flush()
    reasons = {r["reason"] for r in reg.conn.execute("SELECT DISTINCT reason FROM funnel_events")}
    assert {"BAR_EVALUATED", "SIGNAL", "ORDER_OK"} <= reasons
    bot.close()
