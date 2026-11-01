"""The kernel's order desk: entries, closes, the journal of fills, protection and reconciliation.

All broker traffic goes through ``execution.ExecutionEngine``; this module turns its reports into journal rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import numpy as np

from . import indicators
from .arms import EntryIntent
from .core import broker as br
from .core import orders as oc
from .core.broker import OrderRequest, select_filling
from .execution import ExecReport, OrderRecord
from .risk import RiskViolation, SymbolSpec, TradePlan, validate_open_request, validate_sl_change

log = logging.getLogger("mt5_bot")

ORDER_COMMENT = "mt5_bot"
MARKET_CLOSED_BACKOFF_SECONDS = 900.0
QUARANTINE_AFTER = 3          # consecutive execution failures before a symbol is paused
QUARANTINE_MINUTES = 30.0
QUARANTINE_MAX_MINUTES = 480.0
EMERGENCY_SL_ATR_MULT = 2.0
EMERGENCY_TP_R = 1.5


@dataclass(frozen=True)
class EntryOutcome:
    """What happened to one entry attempt. ``risk_allowed`` is None when the attempt stopped
    before the risk layer (no quote, market closed, outside trading hours...)."""

    risk_allowed: Optional[bool]
    reason: str
    sent: bool = False
    ok: bool = False
    volume: float = 0.0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    slippage_points: float = 0.0
    ticket: Optional[int] = None
    dry_run: bool = False
    base_volume: float = 0.0
    risk_pct: float = 0.0
    code: str = ""


class OrdersMixin:
    def _note_execution(self, symbol: str, outcome: EntryOutcome) -> None:
        """Self-healing: QUARANTINE_AFTER consecutive execution failures pause new entries on the
        symbol (30 min, doubling per strike up to 8 h); one executed order clears it."""
        if outcome.code == "ORDER_OK":
            self._exec_failures.pop(symbol, None)
            self._quarantine.pop(symbol, None)
            return
        if outcome.code not in ("ORDER_CHECK_FAIL", "ORDER_SEND_FAIL"):
            return
        failures = self._exec_failures.get(symbol, 0) + 1
        self._exec_failures[symbol] = failures
        if failures < QUARANTINE_AFTER:
            return
        strikes = self._quarantine.get(symbol, (0.0, 0))[1] + 1
        minutes = min(QUARANTINE_MINUTES * 2 ** (strikes - 1), QUARANTINE_MAX_MINUTES)
        self._quarantine[symbol] = (self._monotonic() + minutes * 60, strikes)
        self._exec_failures[symbol] = 0
        text = (f"{symbol}: {failures} execution failures in a row; new entries on this symbol paused for "
                f"{minutes:.0f} min (open positions keep their SL/TP)")
        log.warning(text)
        self.notifier.send(text, key=f"quarantine-{symbol}")

    def _send_entry(self, symbol: str, intent: EntryIntent, plan: TradePlan, info: Any, tick: Any, filling: int,
                    multiplier: float, dossier_id: Optional[str], base_volume: float, group: Optional[str],
                    part: int, parts: int) -> EntryOutcome:
        sig = intent.signal
        tf = intent.timeframe
        decision_id = f"{symbol}:{tf}:{intent.bar_time}"
        # The same decision always maps to the same id, so a restart cannot send it twice. Only a
        # test source without a bar time needs the counter.
        self._order_nonce += 1
        nonce = 0 if intent.bar_time else self._order_nonce
        cid = oc.client_id("open", self.cfg.magic, symbol, tf, intent.bar_time, intent.arm_id, intent.strategy,
                           sig.side, part, nonce)

        def build(t: Any) -> OrderRequest:
            return self._open_request(symbol, plan, info, t, filling, oc.order_comment(cid))

        def wire_plan(request: OrderRequest) -> None:
            validate_open_request({"sl": request.sl, "tp": request.tp, "price": request.price,
                                   "volume": request.volume}, plan)

        def block(code: str, reason: str, level: int = logging.WARNING, **extra: Any) -> EntryOutcome:
            log.log(level, "%s: %s", symbol, reason)
            self._record("execution", "block", code, symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"detail": reason[:200]})
            self._ledger("decision.blocked", decision_id, {"stage": "execution", "code": code, "detail": reason[:200]})
            return EntryOutcome(True, reason, code=code, base_volume=base_volume, **extra)

        request = build(tick)
        try:
            wire_plan(request)
        except RiskViolation as exc:
            return block("RISK_STOPS", f"request blocked by the risk layer: {exc}", logging.ERROR)
        check = self.broker.check(request)
        if check is None:
            return block("ORDER_CHECK_FAIL", f"order_check returned nothing ({self._last_error()}); skipping")
        if check.retcode not in (0, oc.RC_DONE):
            if check.retcode == oc.RC_MARKET_CLOSED:
                self._closed_until[symbol] = self._monotonic() + MARKET_CLOSED_BACKOFF_SECONDS
            return block("ORDER_CHECK_FAIL", f"order_check rejected the order: retcode={check.retcode} "
                                             f"comment={check.comment}")
        if self.cfg.dry_run:
            log.info("DRY RUN: would send %s %s %.2f lots at %s SL %s TP %s (order_check OK, risk %.2f, x%.2f, %s)",
                     sig.side, symbol, plan.volume, request.price, request.sl, request.tp, plan.planned_risk,
                     multiplier, intent.tier)
            self._record("execution", "pass", "DRY_RUN", symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"tier": intent.tier, "volume": plan.volume})
            return EntryOutcome(True, "dry run: order_check OK, not sent", sent=False, ok=True, volume=plan.volume,
                                price=float(request.price), sl=float(request.sl), tp=float(request.tp),
                                dry_run=True, base_volume=base_volume, risk_pct=plan.risk_pct, code="DRY_RUN")
        if self.guard.halted_reason:
            self._entries_while_halted += 1  # defence in depth: the Guardian treats this as a kill condition
        meta = self._order_meta(symbol, intent, plan, multiplier, dossier_id, group, part, parts, tick, info)

        def fresh() -> Any:
            return self.market.tick(symbol)

        try:
            report = self.engine.open(cid, build, tick, fresh, wire_plan, meta, decision_id)
        except RiskViolation as exc:
            return block("RISK_STOPS", f"request blocked by the risk layer: {exc}", logging.ERROR)
        if report.retcode == oc.RC_MARKET_CLOSED:
            self._closed_until[symbol] = self._monotonic() + MARKET_CLOSED_BACKOFF_SECONDS
        self._record_tca(cid, symbol, meta, report)
        if not report.ok:
            self._record("execution", "block", report.code, symbol, tf, arm_id=intent.arm_id, bar_time=intent.bar_time,
                         values={"detail": report.reason[:200]})
            return EntryOutcome(True, report.reason, sent=report.attempts > 0, ok=False, base_volume=base_volume,
                                code=report.code)
        return self._after_fill(symbol, meta, report, multiplier, base_volume)

    def _order_meta(self, symbol: str, intent: EntryIntent, plan: TradePlan, multiplier: float,
                    dossier_id: Optional[str], group: Optional[str], part: int, parts: int, tick: Any,
                    info: Any) -> dict[str, Any]:
        """Everything needed to journal this entry later, even if only reconciliation finds its fill."""
        meta: dict[str, Any] = {
            "symbol": symbol, "side": intent.signal.side, "param_set_id": intent.param_set_id, "arm_id": intent.arm_id,
            "timeframe": intent.timeframe, "tier": intent.tier, "bar_time": intent.bar_time, "sl": plan.sl,
            "tp": plan.tp, "loss_per_lot": plan.loss_per_lot, "strategy": intent.strategy,
            "exploration": intent.exploration, "policy": intent.policy, "regime": intent.regime,
            "min_lot_exception": plan.exception, "sizing_binding": (intent.sizing or {}).get("binding"),
            "size_tag": "MIN_VIABLE_SIZE" if plan.exception else None, "ev_r": intent.ev_r,
            "planned_risk_pct": plan.risk_pct, "multiplier": multiplier, "volume": plan.volume,
            "decision_price": float(tick.ask if intent.signal.side == "BUY" else tick.bid),
            "spread_decision": (float(tick.ask) - float(tick.bid)) / float(info.point), "point": float(info.point)}
        if group:
            meta.update(group=group, part=part + 1, parts=parts)
        if dossier_id:
            meta["dossier_id"] = dossier_id
        if intent.posterior is not None:
            meta.update(p_positive=round(intent.posterior.p_positive, 4),
                        posterior_mean=round(intent.posterior.mean, 4))
        return meta

    def _after_fill(self, symbol: str, meta: Mapping[str, Any], report: ExecReport, multiplier: float,
                    base_volume: float) -> EntryOutcome:
        request = report.request
        slippage = abs(report.fill_price - float(request.price)) / float(meta["point"]) if report.fill_price else 0.0
        if slippage > self.cfg.max_slippage_points:
            self._slippage_pause[symbol] = self._monotonic() + self.cfg.slippage_pause_minutes * 60
            log.warning("%s: slippage %.1f points > max %.1f; new entries paused for %.0f min", symbol, slippage,
                        self.cfg.max_slippage_points, self.cfg.slippage_pause_minutes)
            self.notifier.send(f"{symbol}: slippage {slippage:.1f} points; entries paused", key=f"slip-{symbol}")
        volume, risk_money, risk_pct = self._journal_fill(meta, report.filled_volume, report.fill_price,
                                                          report.position_ticket or report.ticket, report.deal,
                                                          slippage, float(request.sl), float(request.tp))
        rec = self.engine.journal.get(report.client_id)
        protection = ""
        ticket = report.position_ticket or report.ticket
        if rec is not None and ticket:
            protection = self.engine.verify_protection(rec, int(ticket), float(request.sl), float(request.tp),
                                                       float(meta["point"]), self._close_for_protection)
        self._record("execution", "pass", "ORDER_OK", symbol, meta["timeframe"], arm_id=meta["arm_id"],
                     bar_time=meta["bar_time"], values={"tier": meta["tier"], "volume": volume,
                                                        "risk_pct": round(risk_pct, 4),
                                                        "exploration": meta["exploration"]})
        log.info("%s: opened %s %.2f lots at %s SL %s TP %s (order %s, deal %s, risk %.2f = %.3f%%, %s, size x%.2f, %s)",
                 symbol, meta["side"], volume, report.fill_price, request.sl, request.tp, report.ticket, report.deal,
                 risk_money, risk_pct, meta["tier"], multiplier, report.state.value)
        return EntryOutcome(True, "opened", sent=True, ok=protection != "closed", volume=volume,
                            price=report.fill_price, sl=float(request.sl), tp=float(request.tp),
                            slippage_points=slippage, ticket=ticket, base_volume=base_volume, risk_pct=risk_pct,
                            code="ORDER_OK")

    def _journal_fill(self, meta: Mapping[str, Any], volume: float, price: float, ticket: Optional[int],
                      deal: Optional[int], slippage: float, sl: float, tp: float) -> tuple[float, float, float]:
        """Write the journal row of a fill. Used right after the fill and by late reconciliation."""
        risk_money = float(meta["loss_per_lot"]) * volume
        acc_now = self.broker.account()
        equity = float(acc_now.equity) if acc_now is not None else 0.0
        risk_pct = risk_money / equity * 100.0 if equity > 0 else float(meta.get("planned_risk_pct") or 0.0)
        extra: dict[str, Any] = {"deal": deal, "strategy": meta["strategy"], "slippage_points": slippage,
                                 "signal_bar": meta["bar_time"], "exploration": meta["exploration"],
                                 "risk_pct": round(risk_pct, 6), "policy": meta["policy"], "regime": meta["regime"],
                                 "min_lot_exception": meta["min_lot_exception"],
                                 "sizing_binding": meta["sizing_binding"]}
        for key in ("group", "part", "parts", "p_positive", "posterior_mean", "size_tag", "ev_r"):
            if meta.get(key) is not None:
                extra[key] = meta[key]
        if meta.get("dossier_id"):
            extra.update(dossier_id=meta["dossier_id"], size_multiplier=round(float(meta["multiplier"]), 6))
        self.registry.open_trade("live", meta["symbol"], meta["param_set_id"], meta["side"], price, sl, tp, volume,
                                 risk_money, ticket=int(ticket) if ticket else None, open_bar_time=meta["bar_time"],
                                 extra=extra, arm_id=meta["arm_id"], timeframe=meta["timeframe"], tier=meta["tier"],
                                 risk_pct=risk_pct)
        return volume, risk_money, risk_pct

    def _on_late_fill(self, rec: OrderRecord) -> None:
        """Reconciliation found the fill of an uncertain order: journal and protect it now."""
        meta = rec.meta
        ticket = rec.position_ticket or rec.ticket
        if ticket and self.registry.trade_by_ticket(int(ticket)) is not None:
            return    # the position was already adopted into the journal
        volume, price = rec.filled_volume or rec.volume, rec.fill_price or rec.price or 0.0
        self._journal_fill(meta, volume, price, ticket, None, 0.0, float(meta["sl"]), float(meta["tp"]))
        self._record("execution", "pass", "ORDER_OK", rec.symbol, meta.get("timeframe"), arm_id=meta.get("arm_id"),
                     bar_time=meta.get("bar_time"), values={"tier": meta.get("tier"), "volume": volume,
                                                            "late": True})
        self.notifier.send(f"{rec.symbol}: ordem incerta confirmada mais tarde como executada", key=rec.client_id)
        if ticket:
            self.engine.verify_protection(rec, int(ticket), float(meta["sl"]), float(meta["tp"]),
                                          float(meta["point"]), self._close_for_protection)

    def _close_for_protection(self, position: Any, reason: str) -> bool:
        return self._close_position(position, reason)

    def _settle_orders(self) -> None:
        """Every loop: unresolved orders are reconciled until the broker's records settle them."""
        if self.engine is None or self.cfg.dry_run or not self.engine.journal.unresolved():
            return
        try:
            self.engine.reconcile(self._on_late_fill)
        except Exception:  # noqa: BLE001 - entries on the symbol stay blocked while it is unresolved
            log.exception("Order reconciliation failed; new entries on those symbols stay blocked")

    def _record_tca(self, cid: str, symbol: str, meta: Mapping[str, Any], report: ExecReport) -> None:
        """Every order leaves a TCA row. Measuring must never break trading."""
        if self.tca is None:
            return
        try:
            self.tca.record(symbol, meta, report, int((self.clock.now() % 86400) // 3600))
        except Exception:  # noqa: BLE001
            log.exception("%s: TCA row not written", symbol)

    def _open_request(self, symbol: str, plan: TradePlan, info: Any, tick: Any, filling: int,
                      comment: str = ORDER_COMMENT) -> OrderRequest:
        point, digits = info.point, info.digits
        if plan.side == "BUY":
            price = tick.ask
            sl, tp = price - plan.sl_points * point, price + plan.tp_points * point
        else:
            price = tick.bid
            sl, tp = price + plan.sl_points * point, price - plan.tp_points * point
        return OrderRequest("open", symbol, plan.side, plan.volume, price, round(sl, digits), round(tp, digits),
                            self.cfg.deviation_points, self.cfg.magic, comment, filling=filling)

    def reconcile(self) -> None:
        """Rebuild state from the broker at start-up: settle unresolved orders first (a restart
        must never re-send them), then journal every bot position and repair its SL/TP."""
        if self.engine is not None and not self.cfg.dry_run:
            counts = self.engine.reconcile_startup()
            self.engine.reconcile(self._on_late_fill)
            if any(counts.values()):
                log.warning("Reconciliation: %d order(s) cancelled (never sent), %d uncertain after the restart",
                            counts["cancelled"], counts["uncertain"])
        acc = self.broker.account()
        try:
            positions = self._bot_positions()
        except RuntimeError as exc:
            log.error("Reconciliation: %s", exc)
            return
        recovered = self._adopt_positions(positions, acc)
        self._started_reconcile = True
        pending = [o for o in (self.broker.pending_orders() or ()) if o.magic == self.cfg.magic]
        for order in pending:
            if not order.sl or not order.tp:
                log.warning("Pending order %s has no SL/TP; cancelling it (the bot only uses market orders)", order.ticket)
                self._remove_order(order)
            else:
                log.info("Pending order %s with this magic left in place (SL %s, TP %s)", order.ticket, order.sl, order.tp)
        repaired = self.protect_positions(positions, acc)
        self._track_closed_live_trades(positions)
        log.info("Reconciliation: %d bot position(s) open, %d recovered into the journal, %d repaired, "
                 "%d pending order(s)", len(positions), recovered, repaired, len(pending))

    def _adopt_positions(self, positions: list[Any], acc: Any) -> int:
        """Any bot position the journal does not know is recorded (conservatively, in full)."""
        recovered = 0
        for p in positions:
            if self.registry.trade_by_ticket(int(p.ticket)) is None:
                risk = self._open_risks([p], float(acc.equity))[0].risk_money if p.sl else \
                    float(acc.equity) * self.risk.risk_pct / 100
                self.registry.open_trade("live", p.symbol, None, self._side_of(p), float(p.price_open),
                                         float(p.sl or 0.0), float(p.tp or 0.0), float(p.volume), risk,
                                         ticket=int(p.ticket), extra={"recovered": True})
                if self.events is not None:
                    self.events.append("reconcile.adopted", str(p.ticket), {"symbol": p.symbol, "volume": p.volume})
                log.warning("%s: position %s was not in the journal; adopted", p.symbol, p.ticket)
                if self._started_reconcile:
                    self.notifier.send(f"{p.symbol}: posição {p.ticket} fora do diário foi adoptada",
                                       key=f"adopted-{p.ticket}")
                recovered += 1
        return recovered

    def _remove_order(self, order: Any) -> bool:
        if self.cfg.dry_run:
            log.info("DRY RUN: would cancel pending order %s", order.ticket)
            return True
        return self.engine.remove_pending(order)

    def protect_positions(self, positions: list[Any], acc: Any) -> int:
        """Every bot position must carry SL and TP. Adds them when missing; never widens."""
        repaired = 0
        for p in positions:
            if p.sl and p.tp:
                continue
            try:
                if self._repair_position(p, acc):
                    repaired += 1
            except Exception:  # noqa: BLE001 - keep trying the others
                log.exception("%s: could not repair position %s", p.symbol, p.ticket)
        return repaired

    def _repair_position(self, p: Any, acc: Any) -> bool:
        symbol = p.symbol
        info = self.market.symbol_info(symbol)
        tick = self.market.tick(symbol)
        if info is None or tick is None:
            log.error("%s: position %s lacks SL/TP but there is no quote to repair it", symbol, p.ticket)
            return False
        spec = SymbolSpec.from_info(info)
        side = self._side_of(p)
        sign = 1 if side == "BUY" else -1
        entry = float(p.price_open)
        current = tick.bid if side == "BUY" else tick.ask
        risk_money = float(acc.equity) * self.risk.risk_pct / 100.0
        dist = risk_money / (spec.value_per_price * float(p.volume))
        rates = self.market.rates(symbol, self.cfg.timeframe, 0, 100)
        if rates is not None and len(rates) > 20:
            a = indicators.atr(rates["high"], rates["low"], rates["close"], 14)[-2]
            if np.isfinite(a) and a > 0:
                dist = min(dist, float(a) * EMERGENCY_SL_ATR_MULT)
        gap = (spec.stops_level + (tick.ask - tick.bid) / spec.point + 1) * spec.point
        new_sl = float(p.sl) if p.sl else round(entry - sign * dist, spec.digits)
        if not p.sl and (current - new_sl) * sign <= gap:
            log.critical("%s: position %s has no SL and is already beyond its allowed loss; closing it now",
                         symbol, p.ticket)
            self.notifier.send(f"{symbol}: unprotected position {p.ticket} beyond its risk limit was closed")
            return self._close_position(p, "unprotected and beyond the risk limit")
        new_tp = float(p.tp) if p.tp else round(entry + sign * dist * EMERGENCY_TP_R, spec.digits)
        if not p.tp and (new_tp - current) * sign <= gap:
            new_tp = round(current + sign * max(gap * 2, dist), spec.digits)
        validate_sl_change(side, float(p.sl or 0.0), new_sl)
        log.warning("%s: position %s missing %s; setting SL %s TP %s", symbol, p.ticket,
                    "SL and TP" if not p.sl and not p.tp else ("SL" if not p.sl else "TP"), new_sl, new_tp)
        if self.cfg.dry_run:
            log.info("DRY RUN: would modify position %s SL %s TP %s", p.ticket, new_sl, new_tp)
            return True
        if not self.engine.modify_stops(p, new_sl, new_tp, "protect"):
            log.error("%s: SL/TP repair of %s failed", symbol, p.ticket)
            return False
        row = self.registry.trade_by_ticket(int(p.ticket))
        if row is not None:
            self.registry.update_trade_sl(row.id, new_sl, new_tp)
        self.notifier.send(f"{symbol}: protective SL/TP set on position {p.ticket}")
        return True

    def _settle_tca(self, ticket: int, deals: Any) -> None:
        """Commission and swap are only known when the position closes."""
        if self.tca is None:
            return
        found = self.registry.conn.execute("SELECT client_id FROM orders WHERE kind = 'open' AND position_ticket = ?",
                                           (ticket,)).fetchone()
        if found is not None:
            self.tca.settle_costs(found["client_id"], sum(float(d.commission) for d in deals),
                                  sum(float(d.swap) for d in deals))

    def _track_closed_live_trades(self, positions: list[Any]) -> None:
        """Close journal rows whose broker position disappeared, using the deal history."""
        open_tickets = {int(p.ticket) for p in positions}
        for row in self.registry.trades(mode="live", status="open"):
            if row.ticket is None or int(row.ticket) in open_tickets:
                continue
            deals = self.broker.deals_for_position(int(row.ticket))
            if deals is None:
                continue   # unknown: retried on the next pass
            out_types = {br.DEAL_ENTRY_OUT, br.DEAL_ENTRY_OUT_BY}
            exits = [d for d in deals if getattr(d, "entry", None) in out_types]
            if not exits:
                continue
            pnl = sum(float(d.profit) + float(d.commission) + float(d.swap) + float(getattr(d, "fee", 0.0))
                      for d in deals)
            exit_price = float(exits[-1].price)
            r = pnl / row.risk_money if row.risk_money > 0 else 0.0
            self.registry.close_trade(row.id, exit_price, pnl, r, "closed at broker")
            self._settle_tca(int(row.ticket), deals)
            log.info("%s: position %s closed at %s, P&L %.2f (%.2fR, %s)", row.symbol, row.ticket, exit_price, pnl, r,
                     row.tier or "?")
            if self.team is not None:
                from .team.events import TradeClosed

                self.team.bus.publish(TradeClosed("live", round(r, 4), round(pnl, 2), r > 0, "closed at broker"),
                                      agent="performance_analyst", symbol=row.symbol,
                                      correlation_id=(row.extra or {}).get("dossier_id"),
                                      severity="notice")

    def _close_position(self, position: Any, reason: str, volume: Optional[float] = None) -> bool:
        """Close a bot position (or ``volume`` lots of it, for a partial close)."""
        if position.magic != self.cfg.magic:  # defence in depth: never touch foreign positions
            return False
        close_volume = float(position.volume) if volume is None else min(float(volume), float(position.volume))
        symbol = position.symbol
        info = self.market.symbol_info(symbol)
        tick = self.market.tick(symbol)
        if info is None or tick is None:
            log.error("%s: cannot close position %s: no symbol info/tick (%s)", symbol, position.ticket,
                      self._last_error())
            return False
        is_buy = position.type == br.POSITION_TYPE_BUY
        filling = select_filling(info.filling_mode)

        def build(t: Any) -> OrderRequest:
            # Hedging: close this exact position. Netting: one position per symbol, an opposite deal closes it.
            return OrderRequest("close", symbol, "SELL" if is_buy else "BUY", close_volume, t.bid if is_buy else t.ask,
                                deviation=self.cfg.deviation_points, magic=self.cfg.magic, comment=ORDER_COMMENT,
                                position=int(position.ticket) if self.hedging else None, filling=filling)

        if self.cfg.dry_run:
            log.info("DRY RUN: would close position %s (%s %.2f %s) because: %s", position.ticket,
                     "BUY" if is_buy else "SELL", close_volume, symbol, reason)
            return True
        report = self.engine.close(position, close_volume, build, tick, lambda: self.market.tick(symbol), reason)
        if report.retcode == oc.RC_MARKET_CLOSED:
            self._closed_until[symbol] = self._monotonic() + MARKET_CLOSED_BACKOFF_SECONDS
        if not report.ok:
            return False
        log.info("%s: closed position %s (%s)", symbol, position.ticket, reason)
        return True

    def close_all_positions(self, reason: str) -> bool:
        """Close every position with this bot's magic. Returns True if all closed."""
        try:
            positions = self._bot_positions()
        except RuntimeError as exc:
            log.error("Cannot list positions to close: %s", exc)
            return False
        if not positions:
            log.info("No bot positions to close (%s).", reason)
            return True
        all_closed = True
        for pos in positions:
            all_closed = self._close_position(pos, reason) and all_closed
        return all_closed
