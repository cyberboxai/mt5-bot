"""``--replay-day``: what this version would have done on one server day, from cached bars.

Offline and read-only. Every active arm runs over its cached bars with the backtester's fills and
costs; each signal of the day then goes through the same questions the live path asks: does the
minimum lot fit (``feasibility``), does the arm pass the entry gates, is the expected value positive,
and is there room left in the daily budget and the open-risk limit. One entry per symbol at a time.

It also says what the previous rule would have done (minimum lot within the exploration budget only),
so the difference is visible. Costs come from the bars' recorded spreads and the cached commission,
and posteriors are the ones stored NOW: a replay of a past day knows more than that day did.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from . import feasibility as fz
from .arms import Arm, ArmStore, arm_signals
from .backtest import simulate
from .expected_value import estimate
from .gates import EntryRules, entry_gate
from .research import cost_model
from .risk import SymbolSpec

DAY = 86400


@dataclass(frozen=True)
class ReplayTrade:
    symbol: str
    timeframe: str
    strategy: str
    side: str
    entry_time: int
    exit_time: int
    volume: float
    risk_money: float
    risk_pct: float
    r: float
    pnl: float
    min_viable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayReport:
    day: str
    equity: float
    signals: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    trades: list[ReplayTrade] = field(default_factory=list)
    previous_rule_trades: int = 0

    @property
    def total_r(self) -> float:
        return sum(t.r for t in self.trades)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    def skip(self, code: str) -> None:
        self.skipped[code] = self.skipped.get(code, 0) + 1


def _rules(lim: Any, cfg: Any) -> fz.FeasibilityRules:
    level = 50.0 * cfg.tail.margin_stop_out_multiple
    return fz.FeasibilityRules(lim.min_viable_cap_pct, lim.t1_risk_pct, level, max_cost_r=cfg.universe.max_cost_r)


def replay_day(cfg: Any, registry: Any, lim: Any, day: date, equity: float, price_notional: Optional[Mapping[str, float]] = None,
               arms: Optional[Sequence[Arm]] = None) -> ReplayReport:
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    end = start + DAY
    report = ReplayReport(day.isoformat(), equity)
    store = ArmStore(registry)
    arms = list(arms) if arms is not None else store.load()
    rules = EntryRules.from_config(cfg.entry_gates)
    candidates: list[tuple[int, float, Arm, Any, float, SymbolSpec]] = []
    for arm in arms:
        loaded = registry.load_spec(arm.symbol)
        if loaded is None:
            continue
        spec, commission = SymbolSpec.from_json(loaded[0]), float(loaded[1])
        bars = registry.load_bars(arm.symbol, arm.timeframe)
        if not len(bars) or int(bars["time"][-1]) < start or len(bars) < 50:
            continue
        sig = arm_signals(arm, bars)
        costs = cost_model(cfg, commission, arm.symbol)
        first = int(np.searchsorted(bars["time"], start))
        last = int(np.searchsorted(bars["time"], end))
        for trade in simulate(bars, sig, spec, costs, cfg.research.reference_equity, cfg.research.reference_risk_pct,
                              first, last, compound=False):
            report.signals += 1
            loss_per_lot = trade.risk_money / trade.volume if trade.volume > 0 else math.inf
            ev = estimate(arm.posterior.mean if arm.posterior else cfg.tiers.global_mean, sig.tp_mult / sig.sl_mult,
                          (), 1.0).ev_r
            candidates.append((int(trade.entry_time), -ev, arm, trade, loss_per_lot, spec))
    candidates.sort(key=lambda c: (c[0], c[1]))
    open_until: dict[str, tuple[int, float]] = {}    # symbol -> (exit time, money at risk)
    open_risk_limit = equity * lim.max_open_risk_pct / 100.0
    day_loss_limit = equity * lim.daily_loss_pct / 100.0
    lost = 0.0
    for entry_time, _ev, arm, trade, loss_per_lot, spec in candidates:
        notional = (price_notional or {}).get(arm.symbol)
        cand = fz.Candidate(arm.symbol, arm.timeframe, "signal", abs(trade.entry_price - trade.sl), loss_per_lot,
                            spec.volume_min, spec.volume_step, spec.volume_max, notional_per_lot=notional)
        row = fz.evaluate(cand, equity, _rules(lim, cfg))
        if row.min_lot_risk <= equity * lim.explore_day_pct / 100.0:
            report.previous_rule_trades += 1
        if not row.feasible:
            report.skip(row.reason)
            continue
        if arm.tier == "T0" and entry_gate(arm.posterior, None, rules) is not None:
            report.skip("ENTRY_GATES")
            continue
        if arm.posterior is None or arm.posterior.mean <= 0:
            report.skip("EV_NEGATIVE")
            continue
        # ">=": a trade that opens and closes inside one bar still held the symbol during that bar.
        if arm.symbol in open_until and open_until[arm.symbol][0] >= entry_time:
            report.skip("ONE_PER_SYMBOL")
            continue
        if lost >= day_loss_limit:
            report.skip("DAILY_LIMIT")
            continue
        risk = equity * lim.t1_risk_pct / 100.0
        volume = fz.clamp_volume(risk / loss_per_lot, spec.volume_min, spec.volume_max, spec.volume_step) or spec.volume_min
        money = volume * loss_per_lot
        still_open = [held for until, held in open_until.values() if until > entry_time]
        if len(still_open) >= lim.max_positions or sum(still_open) + money > open_risk_limit:
            report.skip("OPEN_RISK")
            continue
        pnl = trade.r * money
        lost += max(-pnl, 0.0)
        open_until[arm.symbol] = (int(trade.exit_time), money)
        report.trades.append(ReplayTrade(arm.symbol, arm.timeframe, arm.strategy, trade.side, entry_time,
                                         int(trade.exit_time), volume, money, money / equity * 100.0, trade.r, pnl,
                                         volume <= spec.volume_min + 1e-12 and money > risk))
    return report


def format_report(report: ReplayReport, currency: str = "") -> str:
    lines = [f"Repetição do dia {report.day} com capital {report.equity:.2f} {currency}".rstrip(),
             "Estimativa com velas em cache e as posteriores de hoje: sabe mais do que esse dia sabia.", "",
             f"Sinais vistos: {report.signals}. Regra anterior teria aceite: {report.previous_rule_trades}. "
             f"Esta versão aceitou: {len(report.trades)}."]
    for t in report.trades:
        when = datetime.fromtimestamp(t.entry_time, tz=timezone.utc).strftime("%H:%M")
        tag = " tamanho mínimo viável" if t.min_viable else ""
        lines.append(f"  {when} {t.symbol} {t.timeframe} {t.side} {t.strategy}: {fz.format_volume(t.volume, 0.01)} lote, "
                     f"risco {t.risk_money:.2f} ({t.risk_pct:.2f} %), resultado {t.r:+.2f}R = {t.pnl:+.2f}{tag}")
    if report.skipped:
        lines.append("Ignorados: " + ", ".join(f"{k} {v}" for k, v in sorted(report.skipped.items())))
    lines.append(f"Total: {report.total_r:+.2f}R = {report.total_pnl:+.2f} {currency}".rstrip())
    return "\n".join(lines)

