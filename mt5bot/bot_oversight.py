"""Periodic oversight of the running kernel: tail risk, reality gap, governance and operations.

Everything here observes the account and can only stop or shrink trading. It runs from housekeeping and
never raises into the loop.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import governance, realitygap, stress
from .core.costs import learn_rollover, pre_weekend
from .core.tail import margin_pressure, portfolio_cvar_pct
from .market import server_datetime
from .observability import broker_drift, ntp_offset
from .persistence import atomic_write_text, mask

log = logging.getLogger("mt5_bot")

BAR_CAPTURE_ROWS = 400


class OversightMixin:
    def _maybe_calibrate(self) -> None:
        """Refit the cost model and the learned rollover window from own data."""
        if self.tca is None or self._monotonic() < self._next_calibration:
            return
        self._next_calibration = self._monotonic() + self.cfg.costs.calibrate_every_hours * 3600
        try:
            cells = self.tca.calibrate(self.cfg.backtest_slippage_points)
            rows = self.registry.conn.execute(
                "SELECT time, spread FROM bars_cache WHERE timeframe = ? AND time > (SELECT COALESCE(MAX(time), 0) - "
                "60 * 86400 FROM bars_cache)", (self.cfg.timeframe,)).fetchall()
            learned = learn_rollover([r["time"] for r in rows], [r["spread"] for r in rows], self.cfg.costs.rules())
            if learned != self.schedule:
                self.schedule = learned
                self.registry.set_kv("rollover_window", json.dumps({
                    "rollover_start_minute": learned.rollover_start_minute,
                    "rollover_end_minute": learned.rollover_end_minute}))
                self.events.append("adaptation.rollover", "schedule", {"start": learned.rollover_start_minute,
                                                                       "end": learned.rollover_end_minute})
            log.info("Cost model calibrated: %d cell(s)", cells)
        except Exception:  # noqa: BLE001 - the previous model stays in use
            log.exception("Cost calibration failed")

    def _tail_checks(self, acc: Any, positions: Optional[list[Any]], server_now: float) -> None:
        """Margin pressure and the nightly stress scenarios. Neither may ever stop the loop."""
        tail = self.cfg.tail.rules()
        level = float(getattr(acc, "margin_level", 0.0) or 0.0)
        call = float(getattr(acc, "margin_so_call", 100.0) or 100.0)
        reason = None
        if margin_pressure(level, call, tail):
            reason = f"margin pressure: level {level:.0f}% is approaching the call level {call:.0f}%"
        if reason != self._margin_freeze:
            if reason:
                log.critical("%s; new entries stopped", reason)
                self.notifier.send(f"Margem sob pressão ({level:.0f}%): novas entradas paradas", key="margin")
            self._margin_freeze = reason
        day = server_datetime(server_now).date().isoformat()
        if positions and self._last_stress_day != day:
            self._last_stress_day = day
            try:
                self._run_stress(acc, positions)
            except Exception:  # noqa: BLE001 - a failed report keeps the previous reduction
                log.exception("Stress scenarios failed")

    def _run_stress(self, acc: Any, positions: list[Any]) -> None:
        equity = float(acc.equity)
        quotes = {p.symbol: self.market.tick(p.symbol) for p in positions}
        if any(q is None for q in quotes.values()):
            return
        specs = {p.symbol: self.specs.get(p.symbol) or self._refresh_spec(p.symbol) for p in positions}
        daily_room = self.risk.max_daily_loss_pct - max(self.guard.daily_loss_pct(equity), 0.0)
        dd_room = self.risk.max_drawdown_pct - self.guard.drawdown_pct(equity)
        report = stress.run_all(positions, specs, quotes, equity, float(acc.balance),
                                int(getattr(acc, "leverage", 100) or 100),
                                float(getattr(acc, "margin_so_so", 50.0) or 50.0), daily_room, dd_room)
        outcomes = [float(r.r) for r in self.registry.trades(status="closed") if r.r is not None][-2000:]
        report["cvar_95_pct_estimate"] = portfolio_cvar_pct(
            [r.risk_money / equity * 100.0 for r in self._open_risks(positions, equity)], outcomes)
        self.registry.conn.execute(
            "INSERT INTO stress_reports (ts, worst_scenario, worst_loss_pct, worst_margin_level, breached, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)", (float(self._utc_now()), report["worst"], report["worst_loss_pct"],
                                           report["worst_margin_level"], int(report["breached"]), json.dumps(report)))
        factor = self.cfg.tail.stress_reduction if report["breached"] else 1.0
        if factor != self._stress_factor:
            self._stress_factor = factor
            self.registry.set_kv("stress_factor", str(factor))
            self.events.append("adaptation.stress", "risk", {"factor": factor, "worst": report["worst"],
                                                             "loss_pct": report["worst_loss_pct"]})
        if report["breached"]:
            log.warning("Stress scenario %s would lose %.2f%%: new risk reduced to x%.2f", report["worst"],
                        report["worst_loss_pct"], factor)
            self.notifier.send(f"Teste de stress: {report['worst']} perderia {report['worst_loss_pct']:.2f}%; "
                               f"risco novo reduzido", key="stress")

    def _weekend_flatten(self, acc: Any, server_now: float) -> None:
        """Small accounts are flat at the weekend unless the arm has proven a multi-day edge (T3)."""
        rules = self.schedule
        market_time = self.clock.latest_tick_time or server_now   # the broker's clock, not the PC's
        if not rules.flat_before_weekend or not pre_weekend(market_time, rules) or self.cfg.dry_run:
            return
        if float(acc.equity) >= self.cfg.tail.small_account_equity:
            return
        try:
            positions = self._bot_positions()
        except RuntimeError:
            return
        for p in positions:
            row = self.registry.trade_by_ticket(int(p.ticket))
            if row is None or row.tier != "T3":
                self._close_position(p, "flat before the weekend")

    def _capture_bars(self) -> None:
        """Bars reach the research store continuously, in hourly batches, not only before research."""
        now = self._monotonic()
        if now < self._next_bar_capture or not self.cfg.research.capture_ticks:
            return
        self._next_bar_capture = now + 3600.0
        for (symbol, tf), series in self._series.items():
            self.registry.store_bars(symbol, tf, series.view()[-BAR_CAPTURE_ROWS:])

    def _ops_checks(self, server_now: float) -> None:
        """Backups and clock discipline. Their failures alert; they never touch trading."""
        if self.backups is not None:
            self.backups.maybe_run(server_now)
        now = self._monotonic()
        if now < self._next_clock_check:
            return
        self._next_clock_check = now + 3600.0
        latest, offset = self.clock.latest_tick_time, self.clock.offset
        if latest and offset is not None and self.clock.is_fresh(self.universe[0], 120.0):
            self.clock_drift = round(broker_drift(float(self._utc_now()), float(latest), float(offset)), 2)
        if self.cfg.ops.ntp_server:
            measured = ntp_offset(self.cfg.ops.ntp_server)
            if measured is not None:
                self.clock_drift = round(measured, 2)

    def _collect_advisor_reports(self) -> None:
        """Non-urgent advisor reports come back from the batch queue when they are ready."""
        if self.advisor is None or self.state_dir is None:
            return
        try:
            self.advisor.poll_reports()
            for kind, text in self.advisor.take_reports():
                day = datetime.fromtimestamp(float(self._utc_now()), tz=timezone.utc).strftime("%Y-%m-%d")
                out = Path(self.cfg.reports_dir) / self.state_dir.name / f"nota_{kind}_{day}.md"
                atomic_write_text(out, text + "\n")
        except Exception:  # noqa: BLE001 - the advisor is optional
            log.debug("advisor reports not collected", exc_info=True)

    def _explain_incident(self, kind: str, detail: str) -> None:
        """Optional plain-language note for the owner. It explains; it never decides anything."""
        if self.advisor is None:
            return
        try:
            self.advisor.submit_report("incident", {"kind": kind, "detail": detail})
        except Exception:  # noqa: BLE001 - the advisor is optional
            log.debug("incident report not queued", exc_info=True)

    def _check_reality_gap(self) -> None:
        """Hourly: live results against the simulation of the same signals."""
        now = self._monotonic()
        if self.arms is None or now < self._next_gap_check:
            return
        self._next_gap_check = now + 3600.0
        try:
            conn = self.registry.conn
            fill_rate, slip_gap = realitygap.fill_and_slippage(conn, self.cfg.backtest_slippage_points)
            report = realitygap.evaluate_gap(realitygap.twin_pairs(conn), slip_gap, fill_rate, realitygap.GapSettings())
            realitygap.record_gap(conn, float(self._utc_now()), report)
        except Exception:  # noqa: BLE001 - the monitor must never stop trading
            log.exception("Reality gap check failed")
            return
        frozen = "; ".join(report.reasons) if report.status == realitygap.DIVERGED else None
        if frozen != self.arms.promotions_frozen:
            self.arms.promotions_frozen = frozen
            if frozen:
                log.critical("Reality gap: %s. Promotions frozen, cost model recalibrating", frozen)
                self.notifier.send(f"Distância à realidade excessiva: {frozen}. Promoções congeladas", key="reality-gap")
                self._explain_incident("reality_gap", frozen)
                self.events.append("adaptation.gap", "research", {"frozen": True, "reasons": list(report.reasons)})
                self._next_calibration = 0.0
                self._maybe_calibrate()
            else:
                log.info("Reality gap back inside its limits; promotions allowed again")
                self.events.append("adaptation.gap", "research", {"frozen": False})

    def _check_governance(self, acc: Any) -> None:
        """Hourly: the verdict on net expectancy, and the stop rule on a real account."""
        now = self._monotonic()
        if now < self._next_governance:
            return
        self._next_governance = now + 3600.0
        rules = self.cfg.governance.rules()
        ts = float(self._utc_now())
        try:
            verdict = governance.evaluate_account(self.registry.conn, rules, ts)
            self.registry.set_kv(governance.VERDICT_KEY, governance.verdict_json(verdict, ts, rules))
        except Exception:  # noqa: BLE001
            log.exception("Governance check failed")
            return
        if not (self.is_real and self.cfg.governance.enabled and verdict.stop_real_trading) or self.research_only:
            return
        path = None
        try:
            out = Path(self.cfg.reports_dir) / self.state_dir.name
            path = governance.write_stop_report(out, verdict, ts, f"REAL {mask(self._login)}")
        except OSError as exc:
            log.error("Could not write the stop report: %s", exc)
        self.research_only = {"active": True, "since": ts, "reason": verdict.reason, "report": str(path)}
        self.registry.set_kv(governance.RESEARCH_ONLY_KEY, json.dumps(self.research_only))
        self.events.append("governance.research_only", "governance", {"reason": verdict.reason, "n": verdict.n,
                                                                      "mean": verdict.mean, "ci_high": verdict.ci_high},
                           durable=True)
        log.critical("STOP RULE: no edge after costs (%s). The real account is now research-only.", verdict.reason)
        self._explain_incident("stop_rule", verdict.reason)
        self.notifier.send(f"Regra de paragem: sem vantagem depois de custos. Conta real em modo só investigação. "
                           f"Relatório: {path}")
