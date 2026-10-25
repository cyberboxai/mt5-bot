"""Performance metrics for the dashboards. Pure functions over journal rows and equity
snapshots. With too little data a metric is None and flagged ``insufficient`` instead of
showing a misleading number. Nothing here projects or extrapolates returns."""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

import numpy as np

MIN_TRADES = 10
MIN_DAYS = 10
R_BINS = (-3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0)


def _r(x: Optional[float], digits: int = 4) -> Optional[float]:
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return round(float(x), digits)


def streaks(rs: Sequence[float]) -> tuple[int, int]:
    best_win = best_loss = win = loss = 0
    for r in rs:
        if r > 0:
            win, loss = win + 1, 0
        else:
            loss, win = loss + 1, 0
        best_win, best_loss = max(best_win, win), max(best_loss, loss)
    return best_win, best_loss


def drawdown(curve: np.ndarray) -> tuple[float, int]:
    """(max drawdown in curve units from the running peak, longest underwater length in samples)."""
    if curve.size == 0:
        return 0.0, 0
    peak = np.maximum.accumulate(curve)
    dd = peak - curve
    longest = current = 0
    for under in dd > 1e-12:
        current = current + 1 if under else 0
        longest = max(longest, current)
    return float(dd.max()), longest


def daily_equity(points: Iterable[tuple[float, float]]) -> "OrderedDict[str, float]":
    """Last equity per UTC day."""
    days: "OrderedDict[str, float]" = OrderedDict()
    for ts, equity in points:
        days[datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")] = float(equity)
    return days


def trade_metrics(trades: Sequence[dict]) -> dict[str, Any]:
    rs = np.array([t["r"] for t in trades if t.get("r") is not None], dtype=float)
    pnl = np.array([t.get("pnl") or 0.0 for t in trades if t.get("r") is not None], dtype=float)
    n = int(rs.size)
    out: dict[str, Any] = {"trades": n, "insufficient": n < MIN_TRADES, "min_trades": MIN_TRADES}
    if n == 0:
        out.update(net_profit=0.0, win_rate=None, avg_r=None, expectancy_r=None, profit_factor=None, best_r=None,
                   worst_r=None, best_pnl=None, worst_pnl=None, longest_win_streak=0, longest_loss_streak=0,
                   max_drawdown_r=0.0, histogram=[], rolling=[])
        return out
    gains, losses = rs[rs > 0].sum(), -rs[rs < 0].sum()
    win_streak, loss_streak = streaks(rs.tolist())
    dd_r, _ = drawdown(np.concatenate([[0.0], np.cumsum(rs)]))
    hist = np.histogram(np.clip(rs, R_BINS[0], R_BINS[-1]), bins=R_BINS)[0]
    rolling = []
    window = 20
    for i in range(window, n + 1):
        chunk = rs[i - window:i]
        rolling.append({"i": i, "expectancy_r": _r(chunk.mean()), "win_rate": _r((chunk > 0).mean())})
    out.update(
        net_profit=_r(pnl.sum(), 2), win_rate=_r((rs > 0).mean()), avg_r=_r(rs.mean()), expectancy_r=_r(rs.mean()),
        profit_factor=_r(min(gains / losses, 99.0)) if losses > 0 else (99.0 if gains > 0 else None),
        best_r=_r(rs.max()), worst_r=_r(rs.min()), best_pnl=_r(pnl.max(), 2), worst_pnl=_r(pnl.min(), 2),
        longest_win_streak=win_streak, longest_loss_streak=loss_streak, max_drawdown_r=_r(dd_r),
        histogram=[{"from": R_BINS[i], "to": R_BINS[i + 1], "count": int(c)} for i, c in enumerate(hist)],
        rolling=rolling[-500:], cumulative_r=[_r(x) for x in np.cumsum(rs)][-2000:])
    if n < MIN_TRADES:
        for key in ("win_rate", "expectancy_r", "profit_factor"):
            out[key + "_note"] = "insufficient data"
    return out


def equity_metrics(points: Sequence[tuple[float, float]]) -> dict[str, Any]:
    days = daily_equity(points)
    values = np.array(list(days.values()), dtype=float)
    out: dict[str, Any] = {"days": int(values.size), "insufficient": values.size < MIN_DAYS, "min_days": MIN_DAYS}
    if values.size < 2:
        out.update(sharpe=None, sortino=None, calmar=None, max_drawdown_pct=None, max_drawdown_days=None,
                   monthly=[], total_return_pct=None)
        return out
    rets = np.diff(values) / np.where(values[:-1] > 0, values[:-1], np.nan)
    rets = rets[np.isfinite(rets)]
    peak_dd = 0.0
    peak = values[0]
    for v in values:
        peak = max(peak, v)
        peak_dd = max(peak_dd, (peak - v) / peak * 100 if peak > 0 else 0.0)
    _, dd_days = drawdown(values)
    sharpe = sortino = calmar = None
    if rets.size >= MIN_DAYS and rets.std(ddof=1) > 0:
        sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(252)
        downside = np.sqrt(np.mean(np.square(np.minimum(rets, 0))))
        sortino = rets.mean() / downside * math.sqrt(252) if downside > 0 else None
        years = values.size / 252
        total = values[-1] / values[0] - 1 if values[0] > 0 else 0.0
        annual = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else None
        calmar = annual * 100 / peak_dd if annual is not None and peak_dd > 0 else None
    monthly: "OrderedDict[str, list[float]]" = OrderedDict()
    for day, value in days.items():
        monthly.setdefault(day[:7], []).append(value)
    months = []
    prev_last: Optional[float] = None
    for month, vals in monthly.items():
        start = prev_last if prev_last is not None else vals[0]
        months.append({"month": month, "return_pct": _r((vals[-1] / start - 1) * 100 if start else None, 3)})
        prev_last = vals[-1]
    out.update(sharpe=_r(sharpe), sortino=_r(sortino), calmar=_r(calmar), max_drawdown_pct=_r(peak_dd, 3),
               max_drawdown_days=dd_days, monthly=months,
               total_return_pct=_r((values[-1] / values[0] - 1) * 100 if values[0] else None, 3))
    return out


def downsample(rows: Sequence[Any], limit: int = 1500) -> list[Any]:
    if len(rows) <= limit:
        return list(rows)
    step = len(rows) / limit
    out = [rows[int(i * step)] for i in range(limit)]
    out[-1] = rows[-1]
    return out


def var_estimate(points: Sequence[tuple[float, float]], confidence: float = 0.95) -> dict[str, Any]:
    """Historical one-day VaR from daily equity changes. An ESTIMATE, labelled as such."""
    values = np.array(list(daily_equity(points).values()), dtype=float)
    if values.size < 20:
        return {"var_pct": None, "note": "dados insuficientes (precisa de 20+ dias)", "label": "estimativa"}
    rets = np.diff(values) / values[:-1]
    return {"var_pct": _r(-np.quantile(rets, 1 - confidence) * 100, 3), "confidence": confidence,
            "days": int(rets.size), "label": "estimativa: VaR histórico a 1 dia, não é um limite nem uma promessa"}
