"""Opportunity engine: ranks symbols and timeframes and publishes the affordability map.

A symbol is attractive when trading it is cheap relative to how much it moves (spread to
ATR), it is liquid now (tick volume, session), the account can afford its minimum lot at a
sensible stop, it does not duplicate the risk already open, and its arms have evidence.
Infeasible instruments are skipped with an explicit reason code instead of silently doing
nothing; the map tells the owner the equity needed to trade each one at each tier.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from . import indicators
from .capital import affordability

STOP_ATR = 1.5   # typical stop distance in ATRs used for the affordability map


@dataclass(frozen=True)
class SymbolScore:
    symbol: str
    total: float
    spread_to_atr: Optional[float]
    liquidity: float
    session: float
    affordable: bool
    correlation_penalty: float
    evidence: float
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for k, v in out.items():
            if isinstance(v, float):
                out[k] = round(v, 4) if math.isfinite(v) else None
        return out


def atr_and_spread(bars: np.ndarray) -> tuple[Optional[float], Optional[float], float]:
    """(ATR(14) in price, median spread in points over the last 200 bars, recent tick-volume ratio)."""
    if bars is None or len(bars) < 30:
        return None, None, 1.0
    atr = indicators.atr(bars["high"], bars["low"], bars["close"], 14)
    value = float(atr[-1]) if np.isfinite(atr[-1]) else None
    spread = float(np.median(np.asarray(bars["spread"][-200:], dtype=float)))
    vol = np.asarray(bars["tick_volume"], dtype=float)
    ratio = float(np.median(vol[-4:]) / max(np.median(vol[-200:]), 1e-9)) if vol.size >= 20 else 1.0
    return value, spread, ratio


def score_symbol(symbol: str, spread_points: Optional[float], atr_price: Optional[float], point: float,
                 volume_ratio: float, session_open: bool, affordable: bool, correlation: float,
                 best_p: Optional[float]) -> SymbolScore:
    notes: list[str] = []
    if atr_price is None or atr_price <= 0 or spread_points is None:
        return SymbolScore(symbol, 0.0, None, 0.0, 0.0, affordable, 0.0, 0.0, ("sem dados suficientes",))
    s2a = spread_points * point / atr_price
    cost = max(0.0, 1.0 - s2a / 0.25)            # 0 at a spread of 25% of ATR, 1 at no spread
    liquidity = min(1.0, max(0.0, volume_ratio))
    session = 1.0 if session_open else 0.4
    corr_pen = min(1.0, max(0.0, abs(correlation)))
    evidence = 0.5 if best_p is None else min(1.0, max(0.0, best_p))
    if not affordable:
        notes.append("lote mínimo acima do risco permitido")
    if s2a > 0.15:
        notes.append(f"spread caro: {s2a:.0%} do ATR")
    total = (0.35 * cost + 0.15 * liquidity + 0.15 * session + 0.2 * evidence + 0.15 * (1 - corr_pen))
    total *= 1.0 if affordable else 0.2
    return SymbolScore(symbol, round(total, 4), s2a, liquidity, session, affordable, corr_pen, evidence, tuple(notes))


@dataclass
class AffordabilityMap:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def add(self, row: Mapping[str, Any]) -> None:
        self.rows.append(dict(row))

    def feasible(self, symbol: str, timeframe: str, tier: str) -> Optional[bool]:
        for r in self.rows:
            if r["symbol"] == symbol and r["timeframe"] == timeframe and r["tier"] == tier:
                return bool(r["feasible"])
        return None

    def to_list(self) -> list[dict[str, Any]]:
        return list(self.rows)


def affordability_rows(symbol: str, timeframe: str, atr_price: Optional[float], value_per_price: float,
                       volume_min: float, commission_per_lot: float, equity: float, tier_risk: Mapping[str, float],
                       exception_caps: Mapping[str, float]) -> list[dict[str, Any]]:
    """One row per tier: is the minimum lot affordable at a typical stop (``STOP_ATR`` x ATR)?"""
    out = []
    if atr_price is None or atr_price <= 0:
        for tier in tier_risk:
            out.append({"symbol": symbol, "timeframe": timeframe, "tier": tier, "feasible": False,
                        "reason": "NO_DATA", "min_equity": None, "min_lot_risk_pct": None})
        return out
    stop = atr_price * STOP_ATR
    for tier, risk in tier_risk.items():
        a = affordability(symbol, timeframe, stop, value_per_price, volume_min, commission_per_lot, equity, risk,
                          exception_caps.get(tier, risk))
        row = a.to_dict()
        row["tier"] = tier
        out.append(row)
    return out


def rank(scores: Sequence[SymbolScore]) -> list[SymbolScore]:
    return sorted(scores, key=lambda s: (-s.total, s.symbol))
