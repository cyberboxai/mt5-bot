"""Synthetic end-to-end simulation with a fake MT5 that replays data and fills SL/TP.

Bootstrap (observation) -> optimisation -> shadow trading -> promotion -> live orders ->
adverse regime -> demotion -> re-optimisation. The decision log is asserted at the end.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from conftest import FakeMT5, synthetic_bars
from mt5bot.bot import Bot
from mt5bot.config import Config
from mt5bot.cycle import run_cycle

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
    """Stands in for the low-priority optimiser subprocess: runs the cycle synchronously."""

    def __init__(self, cfg: Config, state_dir: Path, trigger: str) -> None:
        from mt5bot.registry import Registry

        reg = Registry(state_dir / "registry.sqlite")
        try:
            run_cycle(cfg, reg, trigger)
        finally:
            reg.close()

    def poll(self) -> int:
        return 0


def test_bootstrap_promotion_live_trading_and_demotion(tmp_path: Path) -> None:
    history = synthetic_bars(24 * 365, seed=201, trend=0.15)
    cfg = Config.from_dict({
        "symbols": ["EURUSD"], "timeframe": "H1", "bars": 600, "trading_hours_server": [0, 24],
        "state_root": str(tmp_path / "state"), "reports_dir": str(tmp_path / "reports"),
        "health_file": str(tmp_path / "health.json"), "kill_switch_file": str(tmp_path / "KILL"),
        "log_file": str(tmp_path / "bot.log"), "enabled_strategies": ["donchian", "ema_cross"],
        "trials_per_strategy": 5, "bootstrap_samples": 500, "min_shadow_trades": 10, "min_shadow_trades_floor": 5,
        "min_shadow_days": 3, "max_spread_points": 30, "commission_per_lot": 7.0,
    })
    fake = ReplayMT5(history)
    clock = {"t": float(history["time"][-1]) + 60}
    utc = lambda: clock["t"]  # noqa: E731
    launches: list[str] = []

    def spawn(args: list[str], **kwargs: Any) -> InlineProcess:
        trigger = args[args.index("--trigger") + 1]
        launches.append(trigger)
        return InlineProcess(cfg, Path(args[args.index("--state-dir") + 1]), trigger)

    bot = Bot(cfg, api=fake, wait=lambda s: None, env={}, monotonic=utc, utc_now=utc,
              config_path=str(tmp_path / "config.json"), spawn=spawn, globber=lambda p: [])
    bot.startup()
    reg = bot.registry
    assert reg.champion("EURUSD").is_notrade
    assert "OBSERVATION" in bot.status_line("EURUSD")

    # Phase 1: cold start launches the first optimisation; candidates become challengers.
    bot.run_once()
    assert launches and launches[0].startswith("cold start")
    assert reg.latest_run("EURUSD") is not None
    bot.run_once()
    assert reg.challengers("EURUSD"), "the injected trend edge should produce survivors"

    # Phase 2: the edge persists live; challengers shadow trade until one is promoted.
    live = synthetic_bars(24 * 120, seed=202, trend=0.15)
    live_close = live["close"] / live["close"][0] * float(history["close"][-1])
    scale = live_close / live["close"]
    promoted_at = None
    for i in range(len(live)):
        clock["t"] += H1
        fake.push_bar(float(live["open"][i] * scale[i]), float(live["high"][i] * scale[i]),
                      float(live["low"][i] * scale[i]), float(live_close[i]))
        bot.run_once()
        if not reg.champion("EURUSD").is_notrade:
            promoted_at = i
            break
    assert promoted_at is not None, "a validated challenger must be promoted after shadow evidence"
    champion = reg.champion("EURUSD")
    assert champion.strategy in ("donchian", "ema_cross")

    # Phase 3: an adverse regime. Every live position is driven to its stop loss; when flat
    # the price drifts so that breakouts keep triggering new (losing) entries.
    price = float(fake.rates["EURUSD"][-1]["close"])
    for _ in range(24 * 120):
        clock["t"] += H1
        pos = next((p for p in fake.positions if p.magic == cfg.magic), None)
        if pos is not None:
            buy = pos.type == fake.POSITION_TYPE_BUY
            target = pos.sl - 0.0002 if buy else pos.sl + 0.0002
            o, c = price, target
        else:
            o, c = price, price * (1 + 0.004 * (1 if (int(clock["t"]) // (H1 * 48)) % 2 else -1))
        h, lo = max(o, c) + 0.0003, min(o, c) - 0.0003
        fake.push_bar(o, h, lo, c)
        price = c
        bot.run_once()
        if reg.champion("EURUSD").is_notrade or reg.champion("EURUSD").param_set_id != champion.param_set_id:
            break
    actions = [r["action"] for r in reversed(reg.promotions(200))]
    assert "promote" in actions and "demote" in actions
    assert actions.index("promote") < actions.index("demote")
    demote = next(r for r in reg.promotions(200) if r["action"] == "demote")
    assert demote["reason"] and json.loads(demote["metrics_json"])
    assert reg.trades(mode="live"), "the promoted champion placed real (simulated) orders"
    closed_live = [t for t in reg.trades(mode="live") if t.status == "closed"]
    assert closed_live and all(t.r is not None for t in closed_live)
    assert all(t.sl and t.tp for t in reg.trades(mode="live"))

    # Demotion triggers a re-optimisation on the next housekeeping pass.
    bot._next_housekeeping = 0
    bot.run_once()
    assert any("demotion" in t for t in launches[1:])
    assert len(reg.runs()) >= 2

    # The decision log is what --history prints.
    first = reg.promotions(200)[-1]
    assert first["action"] == "optimization"
    bot.close()
