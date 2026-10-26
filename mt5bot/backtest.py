"""Event-driven backtester that reuses the live sizing and SL/TP code (``risk.plan_trade``).

Fill model (MT5 bars are BID prices; ``spread`` is per bar, in points):

* a signal on the bar that closed at index ``i`` is executed at the OPEN of bar ``i+1``;
* BUY fills at ask = open + spread, plus ``slippage_points`` against us;
  SELL fills at bid = open, minus slippage. This is the full spread on the ask side,
  which equals half a spread each way around the mid price;
* BUY exits trigger on bid (bar low/high); SELL exits trigger on ask (bar high/low + spread);
* if SL and TP are both inside one bar, the SL is assumed to have been hit first
  (pessimistic). A gap through the SL fills at the open, minus slippage;
* SL fills get slippage, TP fills do not (limit-like);
* commission is charged per lot round turn; negative swap is charged per rollover
  (triple on the symbol's 3-day swap weekday); positive swap is ignored;
* no entry when the entry bar spread exceeds ``max_spread_points``, or costs more than
  ``max_spread_to_stop`` of the stop distance (a unit-free filter, valid for FX, gold,
  indices and crypto alike), or its server hour is outside ``trading_hours``;
* positions still open at the end of a window are closed at the last bar close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

import numpy as np

from .config import in_trading_hours
from .risk import SymbolSpec, plan_trade
from .strategies.base import SignalArrays

DAY = 86400


@dataclass(frozen=True)
class CostModel:
    commission_per_lot: float = 0.0
    slippage_points: float = 0.0
    max_spread_points: float = 1e9
    trading_hours: tuple[int, int] = (0, 24)
    close_on_opposite: bool = True
    max_spread_to_stop: float = 1.0


@dataclass
class VirtualPosition:
    side: int
    entry_index: int
    entry_time: int
    entry_price: float
    sl: float
    tp: float
    volume: float
    risk_money: float


@dataclass(frozen=True)
class Trade:
    side: str
    entry_index: int
    exit_index: int
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    sl: float
    tp: float
    volume: float
    pnl: float
    risk_money: float
    r: float
    commission: float
    swap: float
    exit_reason: str


# --------------------------------------------------------------- shared primitives


def open_position(side: int, index: int, bar_time: int, bar_open: float, spread_points: float,
                  atr_value: float, sl_mult: float, tp_mult: float, spec: SymbolSpec, costs: CostModel,
                  equity: float, risk_pct: float) -> Optional[VirtualPosition]:
    """Open a virtual position at the bar open, or None if a filter or the risk plan rejects it."""
    if spread_points > costs.max_spread_points:
        return None
    if spread_points * spec.point > costs.max_spread_to_stop * atr_value * sl_mult:
        return None
    hour = datetime.fromtimestamp(int(bar_time), tz=timezone.utc).hour
    if not in_trading_hours(hour, costs.trading_hours):
        return None
    name = "BUY" if side > 0 else "SELL"
    quote = bar_open + spread_points * spec.point if side > 0 else bar_open
    result = plan_trade(name, atr_value, sl_mult, tp_mult, quote, spread_points, spec, equity, risk_pct,
                        costs.commission_per_lot, costs.slippage_points)
    if result.plan is None:
        return None
    plan = result.plan
    fill = quote + side * costs.slippage_points * spec.point
    return VirtualPosition(side, index, int(bar_time), fill, plan.sl, plan.tp, plan.volume, plan.planned_risk)


def exit_for_bar(pos: VirtualPosition, bar_open: float, bar_high: float, bar_low: float,
                 spread_points: float, point: float, slippage_points: float,
                 entry_bar: bool) -> Optional[tuple[float, str]]:
    """Exit price and reason if this bar touches SL or TP. Pessimistic when both are touched."""
    slip = slippage_points * point
    if pos.side > 0:
        o, h, lo = bar_open, bar_high, bar_low
        if not entry_bar:
            if o <= pos.sl:
                return o - slip, "sl_gap"
            if o >= pos.tp:
                return pos.tp, "tp"
        if lo <= pos.sl:
            return pos.sl - slip, "sl"
        if h >= pos.tp:
            return pos.tp, "tp"
        return None
    spr = spread_points * point
    o, h, lo = bar_open + spr, bar_high + spr, bar_low + spr
    if not entry_bar:
        if o >= pos.sl:
            return o + slip, "sl_gap"
        if o <= pos.tp:
            return pos.tp, "tp"
    if h >= pos.sl:
        return pos.sl + slip, "sl"
    if lo <= pos.tp:
        return pos.tp, "tp"
    return None


def market_exit_price(pos: VirtualPosition, price_bid: float, spread_points: float, point: float,
                      slippage_points: float) -> float:
    """Price of closing at market: BUY sells at bid, SELL buys at ask; slippage against us."""
    slip = slippage_points * point
    if pos.side > 0:
        return price_bid - slip
    return price_bid + spread_points * point + slip


def rollover_nights(entry_time: int, exit_time: int, triple_weekday_mql: int) -> int:
    """Swap nights charged between two server timestamps (weekday rollovers; triple day counts 3)."""
    start_day, end_day = int(entry_time) // DAY, int(exit_time) // DAY
    nights = 0
    for day in range(start_day, end_day):
        weekday = (day + 3) % 7  # 1970-01-01 was a Thursday; Monday = 0
        if weekday >= 5:
            continue
        mql_weekday = (weekday + 1) % 7  # MQL5: Sunday = 0
        nights += 3 if mql_weekday == triple_weekday_mql else 1
    return nights


def close_position(pos: VirtualPosition, exit_index: int, exit_time: int, exit_price: float, reason: str,
                   spec: SymbolSpec, costs: CostModel) -> Trade:
    gross = spec.money((exit_price - pos.entry_price) * pos.side, pos.volume)
    commission = costs.commission_per_lot * pos.volume
    side_name = "BUY" if pos.side > 0 else "SELL"
    swap = spec.swap_cost_per_lot_night(side_name) * pos.volume * rollover_nights(pos.entry_time, exit_time,
                                                                                  spec.swap_3day)
    pnl = gross - commission - swap
    r = pnl / pos.risk_money if pos.risk_money > 0 else 0.0
    return Trade(side_name, pos.entry_index, exit_index, pos.entry_time, int(exit_time), pos.entry_price,
                 exit_price, pos.sl, pos.tp, pos.volume, pnl, pos.risk_money, r, commission, swap, reason)


# ------------------------------------------------------------------------ simulate


class PreparedBars:
    """Bars converted once to Python lists: scalar access in the trade loop is ~10x faster."""

    def __init__(self, bars: np.ndarray) -> None:
        self.bars = bars
        self.n = len(bars)
        self.times = [int(t) for t in bars["time"].tolist()]
        self.opens = np.asarray(bars["open"], dtype=float).tolist()
        self.highs = np.asarray(bars["high"], dtype=float).tolist()
        self.lows = np.asarray(bars["low"], dtype=float).tolist()
        self.closes = np.asarray(bars["close"], dtype=float).tolist()
        self.spreads = np.asarray(bars["spread"], dtype=float).tolist()


def prepare(bars: "np.ndarray | PreparedBars") -> PreparedBars:
    return bars if isinstance(bars, PreparedBars) else PreparedBars(bars)


def simulate(bars: "np.ndarray | PreparedBars", sig: SignalArrays, spec: SymbolSpec, costs: CostModel,
             equity0: float, risk_pct: float, start: int = 0, end: Optional[int] = None,
             compound: bool = True) -> list[Trade]:
    """Run one strategy over bars[start:end]. Entries only inside the window."""
    pb = prepare(bars)
    n = pb.n
    end = n if end is None else min(end, n)
    start = max(start, 1)
    if end - start < 2:
        return []
    times, opens, highs, lows, closes, spreads = pb.times, pb.opens, pb.highs, pb.lows, pb.closes, pb.spreads
    signal_idx = np.flatnonzero(sig.side != 0)
    side_arr, atr_arr = sig.side.tolist(), sig.atr.tolist()
    point, slip = spec.point, costs.slippage_points

    trades: list[Trade] = []
    equity = equity0
    pos: Optional[VirtualPosition] = None
    pending: int = 0           # side to open at the next bar open
    pending_atr = 0.0
    pending_exit = False       # close at the next bar open (opposite signal)

    # First bar that may execute: a signal from bar start-1 executes at start.
    i = start - 1
    k = int(np.searchsorted(signal_idx, i))
    if k >= len(signal_idx) or signal_idx[k] + 1 >= end:
        return []
    i = int(signal_idx[k])
    pending, pending_atr = side_arr[i], atr_arr[i]
    i += 1

    while i < end:
        if pos is not None and pending_exit:
            price = market_exit_price(pos, opens[i], spreads[i], point, slip)
            trade = close_position(pos, i, times[i], price, "opposite", spec, costs)
            trades.append(trade)
            equity += trade.pnl if compound else 0.0
            pos, pending_exit = None, False
        if pos is None and pending:
            pos = open_position(pending, i, times[i], opens[i], spreads[i], pending_atr, sig.sl_mult,
                                sig.tp_mult, spec, costs, equity, risk_pct)
            pending = 0
        if pos is not None:
            hit = exit_for_bar(pos, opens[i], highs[i], lows[i], spreads[i], point, slip, pos.entry_index == i)
            if hit is not None:
                trade = close_position(pos, i, times[i], hit[0], hit[1], spec, costs)
                trades.append(trade)
                equity += trade.pnl if compound else 0.0
                pos = None
        if i + 1 < end:
            s = side_arr[i]
            if s != 0:
                if pos is None:
                    pending, pending_atr = s, atr_arr[i]
                elif costs.close_on_opposite and s == -pos.side:
                    pending_exit = True
                    pending, pending_atr = s, atr_arr[i]
        if pos is None and not pending:
            k = int(np.searchsorted(signal_idx, i + 1))
            if k >= len(signal_idx) or signal_idx[k] + 1 >= end:
                break
            i = int(signal_idx[k])
            pending, pending_atr = side_arr[i], atr_arr[i]
        i += 1

    if pos is not None:
        last = end - 1
        price = market_exit_price(pos, closes[last], spreads[last], point, slip)
        trades.append(close_position(pos, last, times[last], price, "window_end", spec, costs))
    return trades


# ------------------------------------------------------------------------- metrics


def r_series(trades: Sequence[Trade]) -> np.ndarray:
    return np.array([t.r for t in trades], dtype=float)


def max_drawdown(curve: np.ndarray) -> float:
    """Largest peak-to-trough drop of a cumulative curve (>= 0), peak includes the start at 0."""
    if curve.size == 0:
        return 0.0
    running = np.maximum.accumulate(np.concatenate([[0.0], curve]))
    return float(np.max(running[1:] - curve))


def longest_losing_streak(rs: Sequence[float]) -> int:
    best = current = 0
    for r in rs:
        current = current + 1 if r < 0 else 0
        best = max(best, current)
    return best


def profit_factor(rs: np.ndarray) -> float:
    gains = float(rs[rs > 0].sum())
    losses = float(-rs[rs < 0].sum())
    if losses == 0:
        return 99.0 if gains > 0 else 0.0
    return min(gains / losses, 99.0)


def compute_metrics(trades: Sequence[Trade], equity0: float, bar_times: np.ndarray, start: int, end: int,
                    bar_seconds: int) -> dict[str, float]:
    """Summary statistics for one run. R-based metrics are size independent."""
    rs = r_series(trades)
    n = len(trades)
    start = max(0, start)
    end = min(len(bar_times), end)
    span_days = max((int(bar_times[end - 1]) - int(bar_times[start]) + bar_seconds) / DAY, 1e-9) if end > start else 1e-9
    months = span_days / 30.4375
    pnl = np.array([t.pnl for t in trades], dtype=float)
    equity_curve = equity0 + np.cumsum(pnl) if n else np.array([])
    peak = np.maximum.accumulate(np.concatenate([[equity0], equity_curve])) if n else np.array([equity0])
    dd_pct = float(np.max((peak[1:] - equity_curve) / peak[1:]) * 100) if n else 0.0

    # Daily returns over server days that had bars in the window.
    days = np.unique(bar_times[start:end].astype(np.int64) // DAY) if end > start else np.array([], dtype=np.int64)
    daily = {int(d): 0.0 for d in days}
    for t in trades:
        d = int(t.exit_time) // DAY
        daily[d] = daily.get(d, 0.0) + t.pnl
    ordered = [daily[d] for d in sorted(daily)]
    eq = equity0
    rets = []
    for p in ordered:
        rets.append(p / eq if eq > 0 else 0.0)
        eq += p
    rets_arr = np.array(rets, dtype=float)
    sharpe = sortino = 0.0
    if rets_arr.size > 1 and rets_arr.std(ddof=1) > 0:
        sharpe = float(rets_arr.mean() / rets_arr.std(ddof=1) * math.sqrt(252))
        downside = rets_arr[rets_arr < 0]
        dstd = math.sqrt(float(np.mean(np.square(np.minimum(rets_arr, 0))))) if downside.size else 0.0
        sortino = float(rets_arr.mean() / dstd * math.sqrt(252)) if dstd > 0 else 0.0
    net = float(pnl.sum()) if n else 0.0
    years = span_days / 365.25
    ann_return_pct = ((1 + net / equity0) ** (1 / years) - 1) * 100 if years > 0 and equity0 + net > 0 else -100.0
    calmar = ann_return_pct / dd_pct if dd_pct > 0 else 0.0
    exposure_bars = sum(t.exit_index - t.entry_index + 1 for t in trades)
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    return {
        "net_profit": net,
        "trades": float(n),
        "win_rate": float(wins.size / n) if n else 0.0,
        "avg_win_r": float(wins.mean()) if wins.size else 0.0,
        "avg_loss_r": float(losses.mean()) if losses.size else 0.0,
        "expectancy_r": float(rs.mean()) if n else 0.0,
        "r_std": float(rs.std(ddof=1)) if n > 1 else 0.0,
        "profit_factor": profit_factor(rs) if n else 0.0,
        "max_drawdown_money": max_drawdown(np.cumsum(pnl)) if n else 0.0,
        "max_drawdown_pct": dd_pct,
        "max_drawdown_r": max_drawdown(np.cumsum(rs)) if n else 0.0,
        "sharpe_daily": sharpe,
        "sortino_daily": sortino,
        "calmar": float(calmar),
        "longest_losing_streak": float(longest_losing_streak(rs)),
        "exposure": float(exposure_bars / max(end - start, 1)),
        "months": float(months),
        "trades_per_month": float(n / months) if months > 0 else 0.0,
        "total_commission": float(sum(t.commission for t in trades)),
        "total_swap": float(sum(t.swap for t in trades)),
    }
