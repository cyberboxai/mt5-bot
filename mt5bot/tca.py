"""Transaction cost analysis: what every order really cost, and the model calibrated from it.

For each order: decision price, spread at decision and at submit, fill price, slippage in points
and in R, submit-to-fill latency, commission, swap and rejections, stored per symbol, session and
hour. ``calibrate`` turns those rows (with priors from historical bar spreads until enough fills
exist) into ``cost_models``, which the simulator and the cost-to-edge gate both read, so backtests
reflect this broker and this account.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from statistics import median
from typing import Any, Callable, Mapping, Optional

from .core.costs import (CostCell, CostPrior, FillObservation, calibrate_cell, hour_of, lookup_cell, session_of)
from .core.orders import OrderState
from .execution import ExecReport

log = logging.getLogger("mt5_bot")
_FILLED = {OrderState.FILLED, OrderState.PARTIALLY_FILLED}


class TcaStore:
    def __init__(self, registry: Any, clock: Callable[[], float]) -> None:
        self.registry = registry
        self.conn = registry.conn
        self.clock = clock
        self.cells: dict[tuple[str, str, int], CostCell] = {}
        self.load()

    def record(self, symbol: str, meta: Mapping[str, Any], report: ExecReport, server_hour: int) -> None:
        """One row per order, including rejected and uncertain ones: failures are part of the cost."""
        filled = report.state in _FILLED
        point = float(meta.get("point") or 0.0)
        decision = float(meta.get("decision_price") or 0.0)
        side = 1.0 if meta.get("side") == "BUY" else -1.0
        slip = None
        slip_r = None
        if filled and point > 0 and decision and report.fill_price:
            slip = (report.fill_price - decision) * side / point      # positive: paid worse than decided
            stop_points = abs(decision - float(meta.get("sl") or decision)) / point
            slip_r = slip / stop_points if stop_points > 0 else None
        now = float(self.clock())
        volume = report.filled_volume or float(meta.get("volume") or 0.0)
        risk = float(meta.get("loss_per_lot") or 0.0) * volume
        self.conn.execute(
            "INSERT OR REPLACE INTO tca (client_id, ts, status, symbol, timeframe, side, session, hour, decision_price,"
            " spread_decision, spread_submit, fill_price, slippage_points, slippage_r, latency_ms, requotes, rejects,"
            " volume, risk_money, arm_id, tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report.client_id, now, report.state.value, symbol, meta.get("timeframe"), meta.get("side"),
             session_of(now), server_hour, decision or None, meta.get("spread_decision"),
             report.spread_submit / point if point > 0 else None, report.fill_price or None, slip, slip_r,
             report.latency_ms if filled else None, report.requotes, report.rejects, volume, risk,
             meta.get("arm_id"), meta.get("tier")))

    def settle_costs(self, client_id: str, commission: float, swap: float) -> None:
        self.conn.execute("UPDATE tca SET commission = ?, swap = ? WHERE client_id = ?", (commission, swap, client_id))

    # ------------------------------------------------------------- calibration

    def _bar_spread_priors(self) -> dict[tuple[str, int], float]:
        """Median spread per symbol and server hour from cached bars (the prior before any fill)."""
        rows = self.conn.execute("SELECT symbol, time, spread FROM bars_cache WHERE spread > 0 AND time > "
                                 "(SELECT COALESCE(MAX(time), 0) - 90 * 86400 FROM bars_cache)")
        grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
        for row in rows:
            grouped[(row["symbol"], hour_of(int(row["time"])))].append(float(row["spread"]))
        return {k: float(median(v)) for k, v in grouped.items()}

    def calibrate(self, default_slippage: float) -> int:
        """Refit every cell. Returns the number of cells written."""
        now = float(self.clock())
        priors = self._bar_spread_priors()
        grouped: dict[tuple[str, str, int], list[FillObservation]] = defaultdict(list)
        for r in self.conn.execute("SELECT * FROM tca"):
            rejected = r["status"] not in ("FILLED", "PARTIALLY_FILLED")
            grouped[(r["symbol"], r["session"], int(r["hour"]))].append(FillObservation(
                r["symbol"], r["session"], int(r["hour"]), float(r["spread_submit"] or 0.0),
                float(r["slippage_points"] or 0.0), float(r["latency_ms"] or 0.0), rejected))
        written = 0
        with self.registry.tx():
            for key, observations in grouped.items():
                symbol, session, hour = key
                prior = CostPrior(priors.get((symbol, hour), 0.0) or observations[0].spread_points, default_slippage)
                cell = calibrate_cell(observations, prior)
                self.conn.execute(
                    "INSERT OR REPLACE INTO cost_models (symbol, session, hour, n, spread_points, spread_p90,"
                    " slippage_points, slippage_p90, latency_ms, reject_rate, source, updated) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, session, hour, cell.n, cell.spread_points, cell.spread_p90, cell.slippage_points,
                     cell.slippage_p90, cell.latency_ms, cell.reject_rate, cell.source, now))
                written += 1
        self.load()
        return written

    def load(self) -> None:
        self.cells = {}
        for r in self.conn.execute("SELECT * FROM cost_models"):
            self.cells[(r["symbol"], r["session"], int(r["hour"]))] = CostCell(
                int(r["n"]), float(r["spread_points"]), float(r["spread_p90"]), float(r["slippage_points"]),
                float(r["slippage_p90"]), float(r["latency_ms"]), float(r["reject_rate"]), r["source"])

    def cell(self, symbol: str, utc_ts: float, server_hour: int) -> Optional[CostCell]:
        return lookup_cell(self.cells, symbol, session_of(utc_ts), server_hour)

    def sim_lookup(self) -> Callable[[str, int], Optional[tuple[float, float, float]]]:
        """Plugs the calibrated model into ``SimCosts.lookup``."""
        by_hour: dict[tuple[str, int], CostCell] = {}
        for (symbol, _session, hour), cell in self.cells.items():
            best = by_hour.get((symbol, hour))
            if best is None or cell.n > best.n:
                by_hour[(symbol, hour)] = cell

        def lookup(symbol: str, hour: int) -> Optional[tuple[float, float, float]]:
            cell = by_hour.get((symbol, hour))
            return None if cell is None else (cell.slippage_points, cell.latency_ms, cell.reject_rate)

        return lookup

    def slippage_points(self, symbol: str, default: float) -> float:
        """Average calibrated slippage of a symbol, weighted by fills, for research and shadow books."""
        cells = [c for (s, _x, _h), c in self.cells.items() if s == symbol and c.n > 0]
        total = sum(c.n for c in cells)
        return sum(c.slippage_points * c.n for c in cells) / total if total else default

    # ----------------------------------------------------------------- reports

    def report(self, since: float = 0.0) -> dict[str, Any]:
        """Execution quality by symbol, session and hour (server-side aggregation for the panel)."""
        rows = self.conn.execute(
            "SELECT symbol, session, hour, COUNT(*) AS n, SUM(status IN ('FILLED', 'PARTIALLY_FILLED')) AS fills,"
            " SUM(requotes) AS requotes, SUM(rejects) AS rejects, AVG(slippage_points) AS slip, AVG(slippage_r) AS"
            " slip_r, AVG(latency_ms) AS latency, AVG(spread_submit) AS spread FROM tca WHERE ts >= ?"
            " GROUP BY symbol, session, hour ORDER BY symbol, hour", (since,)).fetchall()
        return {"cells": [dict(r) for r in rows], "orders": sum(int(r["n"]) for r in rows),
                "fills": sum(int(r["fills"] or 0) for r in rows)}
