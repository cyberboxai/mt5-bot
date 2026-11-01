"""Live wiring of ``feasibility``: broker specifications and prices in, candidates, the feasibility
floor, the capacity ceiling, the operating mode and the sliced orders out.

Everything here is recomputed from current broker data on every decision or scan. No money
amount appears in this file.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from . import feasibility as fz
from . import limits
from .arms import EntryIntent
from .core.broker import select_filling
from .gates import EntryRules, entry_gate
from .learning import make_detector
from .opportunity import atr_and_spread
from .risk import SymbolSpec, TradePlan
from .strategies.structure_stop import structure_distance

log = logging.getLogger("mt5_bot")


@dataclass
class ChildOrder:
    """One slice still to be sent. Lost on restart on purpose: an entry that shrinks is the safe side."""

    symbol: str
    intent: EntryIntent
    plan: TradePlan
    multiplier: float
    dossier_id: Optional[str]
    base_volume: float
    group: Optional[str]
    index: int
    total: int
    not_before: float


class FeasibilityMixin:
    # ------------------------------------------------------------ candidates

    def _margin_floor_level(self, acc: Any) -> float:
        """Projected margin level (percent) an entry must keep: the guard's own number."""
        stop_out = float(getattr(acc, "margin_so_so", 50.0) or 50.0)
        if int(getattr(acc, "margin_so_mode", 0) or 0) == 1:     # stop-out quoted in money, not percent
            equity = float(getattr(acc, "equity", 0.0) or 0.0)
            stop_out = stop_out / equity * 100.0 if equity > 0 else stop_out
        return stop_out * self.cfg.tail.margin_stop_out_multiple

    def _feasibility_rules(self, acc: Any, risk_target_pct: Optional[float] = None) -> fz.FeasibilityRules:
        lim = self.risk.limits
        return fz.FeasibilityRules(lim.min_viable_cap_pct, lim.t1_risk_pct if risk_target_pct is None else risk_target_pct,
                                   self._margin_floor_level(acc), limits.MAX_MIN_LOT_EXPOSURE_X,
                                   self.cfg.entry_gates.min_cost_to_edge, self.cfg.universe.max_cost_r)

    def _candidate(self, symbol: str, tf: str, policy: str, stop_distance: float, side: str, acc: Any,
                   edge_r: Optional[float] = None) -> Optional[fz.Candidate]:
        """None when the broker gave no usable quote: unknown is not the same as infeasible."""
        spec = self.specs.get(symbol)
        tick = self.market.tick(symbol)
        if spec is None or tick is None or stop_distance <= 0 or tick.bid <= 0 or tick.ask <= 0:
            return None
        sign = 1 if side == "BUY" else -1
        price = float(tick.ask if side == "BUY" else tick.bid)
        worst = price + sign * self.cfg.deviation_points * spec.point
        loss = self._loss_per_lot_fn(symbol)(side, worst, price - sign * stop_distance, 1.0)
        if loss is None:
            loss = spec.money(abs(worst - (price - sign * stop_distance)), 1.0)
        commission = max(self.commission.get(symbol, 0.0), 0.0)
        spread = (float(tick.ask) - float(tick.bid)) / spec.point
        margin = self.broker.calc_margin(side, symbol, 1.0, price)
        margin = float(margin) if margin is not None and math.isfinite(margin) and margin > 0 else None
        notional = self._notional(symbol, side, 1.0, price, acc)
        cost = self.expected_cost_per_lot(symbol, tf, side, spread, spec)
        return fz.Candidate(symbol, tf, policy, stop_distance, float(loss) + commission, spec.volume_min,
                            spec.volume_step, spec.volume_max, margin, notional, cost, edge_r,
                            bool(self._market_state.get(symbol, True)))

    def _signal_skip(self, symbol: str, tf: str, sig: Any, acc: Any) -> Optional[str]:
        """Reason code when the broker's minimum lot cannot carry this signal at this balance.

        Checked before a signal counts as a signal: it can never appear as a blocked trade. The
        cost uses only the structural check here: each arm's edge is judged by the entry gates."""
        equity = float(acc.equity)
        if not math.isfinite(equity) or equity <= 0:
            return "NO_EQUITY"
        stop = sig.signal.atr * (sig.signal.sl_mult or 2.0)
        cand = self._candidate(symbol, tf, f"atr_{sig.signal.sl_mult or 2.0:g}", stop, sig.signal.side, acc)
        if cand is None:
            return None
        tier = "T1" if sig.arm.tier == "T0" else sig.arm.tier
        row = fz.evaluate(cand, equity, self._feasibility_rules(acc, self.risk.limits.tier_risk_pct(tier)))
        return row.reason or None

    # -------------------------------------------------------------- capacity

    def _ticks_per_minute(self, symbol: str) -> Optional[float]:
        """Activity from the primary timeframe, the one every symbol is sure to have.

        Only an estimate for an optional cap: without it there is no liquidity cap, never a lost
        order. This runs on the order path, so it must not be able to stop an entry."""
        try:
            rows = self.market.rates(symbol, self.cfg.timeframe, 1, 48)
            if rows is None or len(rows) < 6:
                return None
            return float(np.median(np.asarray(rows["tick_volume"], dtype=float))) / (self.cfg.bar_seconds / 60.0)
        except Exception:  # noqa: BLE001 - see the docstring
            log.debug("%s: no activity estimate", symbol, exc_info=True)
            return None

    def _capacity(self, symbol: str, spec: SymbolSpec, side: str, price: float, acc: Any) -> fz.Capacity:
        c = self.cfg.capacity
        margin = self.broker.calc_margin(side, symbol, 1.0, price)
        margin = float(margin) if margin is not None and math.isfinite(margin) and margin > 0 else None
        slippage = self.tca.slippage_points(symbol, self.cfg.backtest_slippage_points) if self.tca is not None else None
        liquidity = fz.liquidity_lots(self._ticks_per_minute(symbol), c.lots_per_tick, c.child_interval_seconds,
                                      slippage, float(self.cfg.max_slippage_points))
        room = fz.margin_lots(float(acc.equity), float(getattr(acc, "margin", 0.0) or 0.0), margin,
                              self._margin_floor_level(acc))
        return fz.capacity_lots(spec.volume_max, c.max_child_orders, spec.volume_limit, liquidity, room)

    def _exposure_lots(self, symbol: str, spec: SymbolSpec, side: str, price: float, acc: Any) -> Optional[float]:
        """Lots allowed by the notional cap. Above the minimum lot a plan shrinks to this, it is not
        refused, or a larger account could be blocked where a smaller one trades."""
        notional = self._notional(symbol, side, 1.0, price, acc)
        if notional is None or notional <= 0:
            return None
        return self.risk.max_symbol_exposure_x * float(acc.equity) / notional

    def _volume_cap(self, symbol: str, spec: SymbolSpec, side: str, price: float, acc: Any,
                    capacity: fz.Capacity) -> float:
        cap = capacity.lots
        exposure = self._exposure_lots(symbol, spec, side, price, acc)
        if exposure is not None:
            cap = min(cap, exposure)
        return max(cap, spec.volume_min)

    # ------------------------------------------------------------------ scan

    def _scan_feasibility(self, acc: Any) -> dict[str, Any]:
        """Every (symbol, timeframe, stop policy) at the current balance, the floor and the ceiling."""
        equity = float(acc.equity)
        lim = self.risk.limits
        rules = self._feasibility_rules(acc)
        rows: list[fz.Row] = []
        ceilings: dict[str, float] = {}
        candidates: dict[tuple[str, str, str], fz.Candidate] = {}
        symbols = list(self.universe) + sorted(self._universe_skipped - set(self.universe))
        best_edge = self._best_edges()
        for symbol in symbols:
            spec = self.specs.get(symbol)
            if spec is None:
                continue
            tick = self.market.tick(symbol)
            lots = self._capacity(symbol, spec, "BUY", float(tick.ask), acc).lots if tick is not None else 0.0
            for tf in self.timeframes():
                bars = self.market.rates(symbol, tf, 1, 300)
                atr, _spread, _vol = atr_and_spread(bars)
                if atr is None or atr <= 0:
                    continue
                stops = {f"atr_{m:g}": atr * m for m in fz.STOP_ATR_MULTS}
                structure = structure_distance(bars, atr)
                if structure is not None:
                    stops[fz.STRUCTURE_STOP] = structure
                for policy, distance in stops.items():
                    edge = best_edge.get((symbol, tf))
                    cand = self._candidate(symbol, tf, policy, distance, "BUY", acc, edge if edge and edge > 0 else None)
                    if cand is None:
                        continue
                    candidates[(symbol, tf, policy)] = cand
                    row = fz.evaluate(cand, equity, rules)
                    rows.append(row)
                    ceiling = fz.capacity_ceiling(lots, cand.loss_per_lot, lim.t1_risk_pct)
                    if ceiling is not None:
                        ceilings[symbol] = max(ceilings.get(symbol, 0.0), ceiling)
        floor = fz.feasibility_floor(rows, candidates)
        ceiling = fz.total_ceiling(ceilings, lim.max_positions)
        feasible = [r for r in rows if r.feasible]
        best = max(feasible, key=lambda r: r.headroom or 0.0) if feasible else None
        capacity_ratio = None
        if best is not None and ceilings.get(best.symbol):
            capacity_ratio = equity / ceilings[best.symbol]
        mode = self.modes.update(best is not None, best.headroom if best else None, capacity_ratio)
        self._typical_risk = fz.typical_min_lot_risk(rows)
        currency = str(getattr(acc, "currency", "") or "")
        by_symbol = fz.floors_by_symbol(rows)
        message = "" if best is not None else fz.below_floor_message(equity, floor, currency, by_symbol)
        undeployed = fz.undeployed(equity, ceiling)
        out = {"ts": self._utc_now(), "equity": equity, "currency": currency, "floor": floor, "ceiling": ceiling,
               "floors_by_symbol": by_symbol, "ceilings_by_symbol": ceilings, "mode": mode,
               "mode_pt": fz.MODE_PT[mode], "label": fz.size_label(best.headroom if best else None, capacity_ratio),
               "headroom": best.headroom if best else None, "capacity_ratio": capacity_ratio,
               "undeployed": undeployed, "below_floor": best is None, "message": message,
               "typical_trade_risk": self._typical_risk, "profile": lim.profile,
               "min_viable_cap_pct": lim.min_viable_cap_pct, "rows": [r.to_dict() for r in rows]}
        if undeployed > 0:
            out["undeployed_text"] = (f"capital acima da capacidade de execução: {undeployed:,.2f} {currency} "
                                      "permanece sem uso").replace(",", " ")
        self.feasibility = out
        return out

    def _best_edges(self) -> dict[tuple[str, str], float]:
        """Best posterior mean per (symbol, timeframe), only from arms with enough evidence."""
        need = self.cfg.entry_gates.min_oos_trades
        out: dict[tuple[str, str], float] = {}
        if self.arms is None:
            return out
        for arm in self.arms.arms.values():
            post = arm.posterior
            if post is not None and arm.active and post.n_prior + post.n_obs >= need:
                key = (arm.symbol, arm.timeframe)
                out[key] = max(out.get(key, -math.inf), post.mean)
        return out

    # ---------------------------------------------------------------- slices

    def _drain_children(self, acc: Any) -> None:
        """Send the slices that are due. Anything suspicious drops the rest: never chase an entry."""
        if not self._children:
            return
        now = self._monotonic()
        due = [c for c in self._children if c.not_before <= now]
        self._children = [c for c in self._children if c.not_before > now]
        for child in due:
            block = self._entry_block_reason()
            if block or not self._group_alive(child):
                log.info("%s: slice %d/%d dropped (%s)", child.symbol, child.index + 1, child.total,
                         block or "the entry is no longer open")
                self._children = [c for c in self._children if c.group != child.group]
                continue
            info = self.market.symbol_info(child.symbol)
            tick = self.market.tick(child.symbol)
            if info is None or tick is None or tick.bid <= 0 or tick.ask <= 0:
                self._children = [c for c in self._children if c.group != child.group]
                continue
            outcome = self._send_entry(child.symbol, child.intent, child.plan, info, tick,
                                       select_filling(info.filling_mode), child.multiplier, child.dossier_id,
                                       child.base_volume, child.group, child.index, child.total)
            self._note_execution(child.symbol, outcome)
            if not outcome.ok:
                log.warning("%s: slice %d/%d failed; the remaining slices are dropped", child.symbol, child.index + 1,
                            child.total)
                self._children = [c for c in self._children if c.group != child.group]

    def _group_alive(self, child: ChildOrder) -> bool:
        try:
            return any(p.symbol == child.symbol for p in self._bot_positions(child.symbol))
        except RuntimeError:
            return False

    def first_trade_today(self) -> bool:
        """True once per server day, for the office to react to the first trade."""
        day = int(self.clock.now() // 86400)
        first = self._last_trade_day != day
        self._last_trade_day = day
        return first

    # ------------------------------------------------------------------ slo

    def _arms_ready(self) -> bool:
        """At least one feasible arm passes the entry gates right now."""
        if self.arms is None:
            return False
        rules = EntryRules.from_config(self.cfg.entry_gates)
        feasible = {(r["symbol"], r["timeframe"]) for r in self.affordability if r.get("feasible")}
        return any((a.symbol, a.timeframe) in feasible and entry_gate(a.posterior, None, rules) is None
                   for a in self.arms.arms.values() if a.active)

    def _update_slo(self, acc: Any) -> None:
        row = self.registry.conn.execute("SELECT MIN(open_ts) AS t FROM trades WHERE mode = 'live'").fetchone()
        first = float(row["t"]) if row is not None and row["t"] is not None else None
        was_missed = self.slo.missed
        self.slo.update(float(self._utc_now()), any(self._market_state.values()), self._arms_ready(),
                        self.cfg.slo.first_trade_hours, first)
        self.registry.set_kv("slo_state", self.slo.to_json())
        if self.slo.missed and not was_missed:
            log.warning("First-trade objective missed: %.1f h of market activity with an eligible arm and no order",
                        self.cfg.slo.first_trade_hours)
            self.notifier.send("Primeiro trade fora do objectivo: o Doutor de Fluxo vai analisar o bloqueio.", key="slo")
            self._next_flow = 0.0

    def _watch_gap_drift(self, pairs: list[tuple[float, float]]) -> None:
        """Drift on the reality gap: a fall in live results against the simulation of the same signals.

        The first pass after a start only replays the history, so an old alarm is not fired again."""
        cfg = self.cfg.learning
        if self._gap_detector is None:
            self._gap_detector = make_detector(cfg.drift_detector, cfg.drift_delta_r, cfg.drift_threshold_r,
                                               cfg.drift_min_trades)
            if self._gap_detector is None:
                return
            for live, sim in pairs:
                self._gap_detector.update(live - sim)
            self._gap_seen = len(pairs)
            return
        for live, sim in pairs[self._gap_seen:]:
            if not self._gap_detector.update(live - sim):
                continue
            detail = "resultados reais a cair face à simulação dos mesmos sinais"
            self.registry.conn.execute("INSERT INTO drift_events (ts, arm_id, scope, detail) VALUES (?, NULL, 'gap', ?)",
                                       (float(self._utc_now()), detail))
            log.warning("Reality gap drift: %s; recalibrating the cost model", detail)
            self.notifier.send(f"Deriva na distância à realidade: {detail}", key="gap-drift")
            self._next_calibration = 0.0
        self._gap_seen = len(pairs)

    # -------------------------------------------------------------- learning

    def _learning_housekeeping(self, acc: Any) -> None:
        now = self._monotonic()
        if self.arms is not None and now >= self._next_learning_point:
            self._next_learning_point = now + 3600.0
            try:
                self.arms.record_learning_points(float(self._utc_now()))
            except Exception:  # noqa: BLE001 - a report must never stop trading
                log.debug("learning point not stored", exc_info=True)
        # On a change of profile or mode, when arms first pass the gates, and every 6 h: equity and
        # posteriors move, and an old report would show a price that no longer applies.
        ready = bool(self.arms and any(a.tier != "T0" for a in self.arms.arms.values()))
        key = (self.risk.limits.profile, str(self.feasibility.get("mode")), ready)
        if self.feasibility and (key != self._risk_view_key or now >= self._next_risk_view):
            self._risk_view_key = key
            self._next_risk_view = now + 6 * 3600.0
            try:
                self._refresh_risk_view(acc)
            except Exception:  # noqa: BLE001 - a report must never stop trading
                log.exception("Risk report failed")

    def _refresh_risk_view(self, acc: Any) -> None:
        """The price of the profile and mode in force, in account money. Runs when either changes."""
        from .riskreport import estimate_inputs, money_view

        trades_per_day, mix = estimate_inputs(self.registry)
        ready = [a.posterior.mean for a in self.arms.arms.values() if a.tier != "T0" and a.posterior] if self.arms else []
        edge = float(np.mean(ready)) if ready else None
        rows = [r for r in self.affordability if r.get("feasible")]
        minimum = min((float(r["min_lot_risk_pct"]) for r in rows if r.get("min_lot_risk_pct")), default=0.0)
        self.risk_view = money_view(self.risk.limits, float(acc.equity), str(getattr(acc, "currency", "") or ""),
                                    trades_per_day, mix, edge, minimum)
        self.registry.set_kv("risk_view", json.dumps(self.risk_view, default=str))

    # ---------------------------------------------------------------- weekly

    def _update_weekly_limit(self, acc: Any) -> None:
        """Soft limit: past it, new entries pause until the next server week. Positions stay."""
        equity = float(acc.equity)
        server_now = self.clock.now()
        _day, week = self._budget_refs(acc, server_now)
        limit = self.risk.limits.weekly_loss_pct
        lost = (week - equity) / week * 100.0 if week > 0 else 0.0
        reached = limit > 0 and lost >= limit
        if reached and not self._weekly_block:
            text = f"weekly soft loss limit: {lost:.2f}% lost this week >= {limit:.2f}%; new entries pause until next week"
            log.warning(text)
            self.notifier.send(text, key="weekly-limit")
            self._weekly_block = text
        elif not reached:
            self._weekly_block = None

    def _rebase_week(self, amount: float) -> None:
        """A deposit or withdrawal is not a weekly result."""
        raw = self.registry.get_kv("week_ref")
        if raw:
            ref = json.loads(raw)
            ref["equity"] = max(float(ref.get("equity", 0.0)) + amount, 0.0)
            self.registry.set_kv("week_ref", json.dumps(ref))

