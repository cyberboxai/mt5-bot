"""Entry gates that need live account or market data: cost to edge, margin, currency and gap risk, and the
portfolio factor. The rules themselves live in ``core``; this is the wiring.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from .arms import EntryIntent
from .config import TIMEFRAME_SECONDS
from .core.costs import all_in_cost_per_lot, cost_to_edge_ratio, minute_of_day
from .core.portfolio import size_factor
from .core.tail import gap_allowance_points, margin_guard, may_cross_break, open_gaps
from .risk import EntryContext, SymbolSpec, TradePlan

log = logging.getLogger("mt5_bot")

class GatesMixin:
    def _portfolio_factor(self, symbol: str, positions: list[Any], equity: float, intent: EntryIntent) -> float:
        """Risk parity, cluster caps and the correlated-risk target: a factor in [0, 1]."""
        returns: dict[str, np.ndarray] = {}
        for name, closes in self._closes.items():
            if len(closes) >= 31:
                returns[name] = np.diff(np.log(closes))
        open_pct: dict[str, float] = {}
        for risk in self._open_risks(positions, equity):
            open_pct[risk.symbol] = open_pct.get(risk.symbol, 0.0) + risk.risk_money / equity * 100.0
        peers = sorted(set(open_pct) | {symbol})
        factor, info = size_factor(symbol, peers, returns, open_pct, self.risk.limits.tier_risk_pct(
            "T1" if intent.tier in ("T0", "EXT") else intent.tier), self.cfg.portfolio.rules())
        self._portfolio_info = info
        return factor

    def _capital_guards(self, symbol: str, spec: SymbolSpec, plan: TradePlan, ctx: EntryContext,
                        acc: Any) -> Optional[tuple[str, str]]:
        """(code, reason) when margin or per-currency risk forbids this entry."""
        leverage = float(getattr(acc, "leverage", 0) or 0)
        new_margin = float(ctx.notional_money or 0.0) / leverage if leverage > 0 else 0.0
        stop_out = float(getattr(acc, "margin_so_so", 50.0) or 50.0)
        if int(getattr(acc, "margin_so_mode", 0) or 0) == 1:     # stop-out quoted in money, not percent
            used = float(getattr(acc, "margin", 0.0) or 0.0) + new_margin
            stop_out = stop_out / used * 100.0 if used > 0 else 0.0
        refused = margin_guard(float(acc.equity), float(getattr(acc, "margin", 0.0) or 0.0), new_margin, stop_out,
                               self.cfg.tail.rules())
        if refused:
            return "MARGIN_GUARD", refused
        cap = self.cfg.portfolio.max_currency_risk_pct
        equity = ctx.equity if ctx.equity > 0 else 1.0
        new_pct = plan.planned_risk / equity * 100.0
        by_currency: dict[str, float] = {}
        for risk in ctx.bot_positions:
            other = self.specs.get(risk.symbol)
            for currency in {other.currency_base, other.currency_profit} if other else ():
                by_currency[currency] = by_currency.get(currency, 0.0) + risk.risk_money / equity * 100.0
        for currency in {spec.currency_base, spec.currency_profit} - {""}:
            if by_currency.get(currency, 0.0) + new_pct > cap + 1e-9:
                return "CURRENCY_CAP", (f"{currency} risk {by_currency.get(currency, 0.0) + new_pct:.2f}% would exceed "
                                        f"{cap:.2f}%")
        return None

    def _gap_points(self, symbol: str, tf: Optional[str], price: float, spec: SymbolSpec) -> float:
        """Extra adverse distance for sizing when this trade may stay open over a break in trading."""
        seconds = TIMEFRAME_SECONDS.get(tf or self.cfg.timeframe, self.cfg.bar_seconds)
        server_now = self.clock.now()
        hours_left = (1440 - minute_of_day(server_now)) / 60.0
        tail = self.cfg.tail.rules()
        if not may_cross_break(seconds, self.cfg.costs.expected_hold_bars, hours_left, tail):
            return 0.0
        day = int(server_now // 86400)
        cached = self._gap_cache.get(symbol)
        if cached is None or cached[0] != day:
            bars = self.market.rates(symbol, self.cfg.timeframe, 1, min(self.cfg.bars, 5000))
            gaps = np.zeros(0)
            if bars is not None and len(bars) > 2:
                gaps = open_gaps(bars["open"], bars["close"], bars["time"], 2 * self.cfg.bar_seconds)
            cached = (day, gaps)
            self._gap_cache[symbol] = cached
        return gap_allowance_points(cached[1], price, spec.point, tail)

    def expected_cost_per_lot(self, symbol: str, tf: Optional[str], side: str, spread_points: float,
                              spec: SymbolSpec) -> float:
        """All-in round-turn cost of 1 lot from the calibrated model (own fills, then priors)."""
        now = float(self._utc_now())
        hour = int((self.clock.now() % 86400) // 3600)
        cell = self.tca.cell(symbol, now, hour) if self.tca is not None else None
        spread = max(spread_points, cell.spread_points) if cell is not None else spread_points
        slippage = cell.slippage_points if cell is not None else self.cfg.backtest_slippage_points
        seconds = TIMEFRAME_SECONDS.get(tf or self.cfg.timeframe, self.cfg.bar_seconds)
        nights = self.cfg.costs.expected_hold_bars * seconds / 86400.0
        return all_in_cost_per_lot(spread, slippage, spec.point, spec.value_per_price,
                                   self.commission.get(symbol, self.cfg.commission_fallback_per_lot),
                                   spec.swap_cost_per_lot_night(side), nights)  # type: ignore[arg-type]

    def _cost_gate(self, symbol: str, intent: EntryIntent, plan: TradePlan, spread_points: float, spec: SymbolSpec,
                   info: Any, entry: Optional[bool] = None) -> Optional[str]:
        """Why this arm may not trade at this cost, or None. An external test source has no evidence
        to gate; exploration is exempt because the prior threshold and the trade budget bound it.

        Entry gate (T1 and minimum-viable trades, or ``entry=True``): the cost-to-edge condition on
        the posterior mean. Scaling gate (size above that): on the lower quantile, with its higher k."""
        c = self.cfg.costs
        post = intent.posterior
        if not c.gate_enabled or post is None:
            return None
        if intent.exploration and (c.exploration_exempt_real if self.is_real else c.exploration_exempt_demo):
            return None
        cost = self.expected_cost_per_lot(symbol, intent.timeframe, intent.signal.side, spread_points, spec)
        if entry is None:
            entry = intent.tier == "T1" or plan.exception
        if entry:
            edge, k, name = post.mean, self.cfg.entry_gates.min_cost_to_edge, "mean"
        else:
            edge, k, name = post.mean - c.edge_z * post.sd, max(c.k, self.cfg.scaling_gates.min_cost_to_edge), "lower"
        ratio = cost_to_edge_ratio(edge, plan.loss_per_lot, cost)
        if ratio >= k:
            return None
        return (f"cost-to-edge {ratio:.2f} < {k:.2f}: {name} edge {edge:+.3f}R x {plan.loss_per_lot:.2f} per lot "
                f"vs all-in cost {cost:.2f} per lot")
