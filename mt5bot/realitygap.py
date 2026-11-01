"""Reality gap monitor: live results against what the simulator said for the same signals.

Every live trade has a twin: its arm's shadow trade of the same signal bar, run through the same
fill and cost rules as research. The gap between them is a first-class metric. When live results
fall behind the simulation beyond a threshold, tier promotions freeze, the cost model is recalibrated
and the owner is told: the simulation is flattering this broker, so its evidence cannot be trusted.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import numpy as np

OK = "ok"
INSUFFICIENT = "insufficient"
DIVERGED = "diverged"


@dataclass(frozen=True)
class GapSettings:
    min_pairs: int = 20
    max_gap_r: float = 0.15
    max_slippage_gap_points: float = 5.0
    min_fill_rate: float = 0.85
    window: int = 200


@dataclass(frozen=True)
class GapReport:
    n: int
    live_mean_r: Optional[float]
    sim_mean_r: Optional[float]
    gap_r: Optional[float]
    gap_se: Optional[float]
    slippage_gap_points: Optional[float]
    fill_rate: Optional[float]
    status: str
    reasons: tuple[str, ...] = ()


def evaluate_gap(pairs: Sequence[tuple[float, float]], slippage_gap: Optional[float], fill_rate: Optional[float],
                 s: GapSettings) -> GapReport:
    """``pairs``: (live R, simulated R) of the same signal, in time order."""
    recent = list(pairs)[-s.window:]
    n = len(recent)
    reasons: list[str] = []
    if fill_rate is not None and fill_rate < s.min_fill_rate:
        reasons.append(f"fill rate {fill_rate:.0%} < {s.min_fill_rate:.0%}")
    if slippage_gap is not None and slippage_gap > s.max_slippage_gap_points:
        reasons.append(f"slippage {slippage_gap:+.1f} points above the model")
    if n == 0:
        return GapReport(0, None, None, None, None, slippage_gap, fill_rate, DIVERGED if reasons else INSUFFICIENT,
                         tuple(reasons))
    live = np.array([p[0] for p in recent], dtype=float)
    sim = np.array([p[1] for p in recent], dtype=float)
    diff = live - sim
    gap = float(diff.mean())
    se = float(diff.std(ddof=1) / math.sqrt(n)) if n > 1 else None
    if n >= s.min_pairs and se is not None and gap < -s.max_gap_r and gap + 1.645 * se < 0:
        reasons.append(f"live {gap:+.3f}R per trade behind the simulation")
    status = DIVERGED if reasons else (OK if n >= s.min_pairs else INSUFFICIENT)
    return GapReport(n, float(live.mean()), float(sim.mean()), gap, se, slippage_gap, fill_rate, status, tuple(reasons))


def twin_pairs(conn: Any) -> list[tuple[float, float]]:
    """(live R, shadow R) of every closed live trade that has a shadow twin."""
    rows = conn.execute(
        "SELECT arm_id, r, extra_json, mode, open_bar_time, close_ts FROM trades WHERE arm_id IS NOT NULL AND"
        " status = 'closed' AND r IS NOT NULL ORDER BY close_ts").fetchall()
    shadow: dict[tuple[int, int], float] = {}
    live: list[tuple[tuple[int, int], float]] = []
    for r in rows:
        extra = json.loads(r["extra_json"] or "{}")
        key = (int(r["arm_id"]), int(extra.get("signal_bar") or r["open_bar_time"] or 0))
        if r["mode"] == "shadow":
            shadow[key] = float(r["r"])
        elif not extra.get("recovered"):
            live.append((key, float(r["r"])))
    return [(value, shadow[key]) for key, value in live if key in shadow]


def fill_and_slippage(conn: Any, modelled_slippage: float) -> tuple[Optional[float], Optional[float]]:
    row = conn.execute("SELECT COUNT(*) AS n, SUM(status IN ('FILLED', 'PARTIALLY_FILLED')) AS fills,"
                       " AVG(slippage_points) AS slip FROM tca").fetchone()
    n = int(row["n"] or 0)
    if n == 0:
        return None, None
    slip = row["slip"]
    return float(row["fills"] or 0) / n, None if slip is None else float(slip) - modelled_slippage


def record_gap(conn: Any, ts: float, report: GapReport) -> None:
    conn.execute("INSERT INTO reality_gap (ts, n, live_mean_r, sim_mean_r, gap_r, slippage_gap_points, fill_rate,"
                 " status, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (ts, report.n, report.live_mean_r, report.sim_mean_r, report.gap_r, report.slippage_gap_points,
                  report.fill_rate, report.status, json.dumps({"reasons": report.reasons, "gap_se": report.gap_se})))


def latest(conn: Any) -> Optional[Mapping[str, Any]]:
    row = conn.execute("SELECT * FROM reality_gap ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
