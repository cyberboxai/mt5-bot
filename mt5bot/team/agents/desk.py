"""Strategy Desk: trend, mean-reversion, breakout/volatility and multi-timeframe analysts.

Each analyst wraps a family of the strategy library. The analyst whose family owns the
proposing arm is the PROPOSER for that bar: it votes for the arm's signal.
The others vote with a deterministic, state-based bias of their family (for example the
trend analyst's EMA alignment), so a vote exists on every bar, not only on crossings.
Raw confidences are mapped to calibrated ones by the CIO from realised outcomes.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

import numpy as np

from ... import indicators
from ..agent import Agent, AgentState, Opinion
from ..context import DecisionContext, Proposal

PT_SIDE = {"long": "comprador", "short": "vendedor", "flat": "neutro"}
TRACK_REFRESH_SECONDS = 3600.0
HTF_FACTOR = {"M1": 5, "M5": 6, "M15": 4, "M30": 4, "H1": 4, "H4": 6}


def family_for(strategy: str, params: Optional[Mapping[str, Any]] = None) -> str:
    base = strategy[len("volfilter_"):] if strategy.startswith("volfilter_") else strategy
    if base in ("ema_cross", "momentum_pullback"):
        return "trend_analyst"
    if base in ("donchian", "session_breakout", "ny_breakout", "vol_contraction"):
        return "breakout_analyst"
    if base in ("rsi_reversion", "vwap_reversion", "range_fade"):
        return "mean_reversion_analyst"
    if base == "bollinger":
        return "breakout_analyst" if (params or {}).get("mode") == "squeeze" else "mean_reversion_analyst"
    return "trend_analyst"  # strategies added later default to the trend desk until mapped here


def _strength(z: float, scale: float = 1.5) -> float:
    return 0.5 + 0.4 * math.tanh(abs(z) / scale)


def _last(x: np.ndarray, default: float = float("nan")) -> float:
    value = float(x[-1]) if len(x) else default
    return value if math.isfinite(value) else default


class DeskAgent(Agent):
    """Base for the four analysts."""

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._track_at = -math.inf

    def bias(self, bars: np.ndarray, ctx: DecisionContext) -> tuple[int, float, list[tuple[str, str]], dict]:
        raise NotImplementedError

    def owns(self, proposal: Optional[Proposal]) -> bool:
        return proposal is not None and family_for(proposal.strategy, proposal.params) == self.id

    # Bars each family needs for its indicators to have forgotten their seed (EMA50 ~ 3 periods).
    TAIL = 400

    def analyze(self, ctx: DecisionContext) -> Opinion:
        direction, strength, reasons, details = self.bias(ctx.market.closed[-self.TAIL:], ctx)
        details["track_record"] = self.stats.get("track_record", {})
        proposal = ctx.proposal
        sym = ctx.market.symbol
        if self.owns(proposal):
            side = 1 if proposal.side == "BUY" else -1
            confidence = 0.70 if direction == side else 0.60 if direction == 0 else 0.55
            stance = "long" if side > 0 else "short"
            reasons = [("PROPOSER", f"arm {proposal.description} signals {proposal.side}")] + reasons
            details["proposer"] = True
            self.set_state(AgentState.DEBATING, task=f"{sym}: propõe {proposal.side}",
                           message=f"{sym}: proponho {proposal.side} ({proposal.strategy}), confiança {confidence:.2f}")
        else:
            stance = "long" if direction > 0 else "short" if direction < 0 else "flat"
            confidence = strength if direction else 0.5
            details["proposer"] = False
            self.set_state(AgentState.ANALYZING, task=f"{sym}: enviesamento {PT_SIDE[stance]}",
                           message=f"{sym}: enviesamento {PT_SIDE[stance]}, confiança {confidence:.2f}")
        return self.opinion(ctx, stance, confidence, reasons, features=tuple(details.get("features", ())),
                            details=details)

    # --------------------------------------------------------------- track record

    def tick(self, now: float) -> None:
        if now - self._track_at < TRACK_REFRESH_SECONDS or self.services.registry is None:
            return
        self._track_at = now
        self.stats["track_record"] = self._track_record()

    def _track_record(self) -> dict[str, Any]:
        registry = self.services.registry
        out: dict[str, Any] = {}
        modes: dict[str, list[float]] = {"live": [], "shadow": []}
        mode_cache: dict[int, Optional[str]] = {}
        for mode in ("live", "shadow"):
            for row in registry.trades(mode=mode, status="closed", limit=400):
                strategy = (row.extra or {}).get("strategy")
                if not strategy or row.r is None:
                    continue
                params: Optional[dict] = None
                if strategy.endswith("bollinger") and row.param_set_id is not None:
                    if row.param_set_id not in mode_cache:
                        try:
                            mode_cache[row.param_set_id] = registry.param_set(int(row.param_set_id))[1].get("mode")
                        except KeyError:
                            mode_cache[row.param_set_id] = None
                    params = {"mode": mode_cache[row.param_set_id]}
                if family_for(strategy, params) == self.id:
                    modes[mode].append(float(row.r))
        for mode, rs in modes.items():
            recent = rs[-50:]
            out[mode] = {"trades": len(recent), "win_rate": round(float(np.mean([r > 0 for r in recent])), 3)
                         if recent else None, "expectancy_r": round(float(np.mean(recent)), 4) if recent else None}
        return out


class TrendAnalyst(DeskAgent):
    id = "trend_analyst"

    def bias(self, bars: np.ndarray, ctx: DecisionContext) -> tuple[int, float, list[tuple[str, str]], dict]:
        close = np.asarray(bars["close"], dtype=float)
        atr = _last(indicators.atr(bars["high"], bars["low"], close, 14))
        fast, slow = _last(indicators.ema(close, 20)), _last(indicators.ema(close, 50))
        if not (math.isfinite(atr) and atr > 0 and math.isfinite(fast) and math.isfinite(slow)):
            return 0, 0.5, [("INSUFFICIENT_DATA", "not enough bars")], {"features": ("ema20", "ema50", "atr14")}
        z = (fast - slow) / atr
        direction = 1 if z > 0.15 else -1 if z < -0.15 else 0
        reason = ("TREND_ALIGNED" if direction else "TREND_FLAT", f"EMA20-EMA50 = {z:+.2f} ATR")
        return direction, _strength(z), [reason], {"z": round(z, 4), "features": ("ema20", "ema50", "atr14")}


class MeanReversionAnalyst(DeskAgent):
    id = "mean_reversion_analyst"

    def bias(self, bars: np.ndarray, ctx: DecisionContext) -> tuple[int, float, list[tuple[str, str]], dict]:
        close = np.asarray(bars["close"], dtype=float)
        rsi = _last(indicators.rsi(close, 14), 50.0)
        mid = _last(indicators.sma(close, 20))
        dev = _last(indicators.rolling_std(close, 20))
        pct_b = (close[-1] - (mid - 2 * dev)) / (4 * dev) if math.isfinite(mid) and dev and dev > 0 else 0.5
        features = ("rsi14", "bollinger20")
        if rsi >= 70 or pct_b >= 1.0:
            z = max((rsi - 70) / 10, (pct_b - 1.0) * 4, 0.2)
            return -1, _strength(z, 1.0), [("OVERBOUGHT", f"RSI {rsi:.0f}, %b {pct_b:.2f}: expects a pullback")], \
                {"rsi": round(rsi, 2), "pct_b": round(pct_b, 3), "features": features}
        if rsi <= 30 or pct_b <= 0.0:
            z = max((30 - rsi) / 10, -pct_b * 4, 0.2)
            return 1, _strength(z, 1.0), [("OVERSOLD", f"RSI {rsi:.0f}, %b {pct_b:.2f}: expects a bounce")], \
                {"rsi": round(rsi, 2), "pct_b": round(pct_b, 3), "features": features}
        return 0, 0.5, [("NEUTRAL", f"RSI {rsi:.0f}, %b {pct_b:.2f}")], \
            {"rsi": round(rsi, 2), "pct_b": round(pct_b, 3), "features": features}


class BreakoutAnalyst(DeskAgent):
    id = "breakout_analyst"

    def bias(self, bars: np.ndarray, ctx: DecisionContext) -> tuple[int, float, list[tuple[str, str]], dict]:
        close = np.asarray(bars["close"], dtype=float)
        upper = _last(indicators.rolling_max_prev(bars["high"], 20))
        lower = _last(indicators.rolling_min_prev(bars["low"], 20))
        atr_series = indicators.atr(bars["high"], bars["low"], close, 14)
        atr = _last(atr_series)
        atr_avg = _last(indicators.sma(np.nan_to_num(atr_series, nan=0.0), 50))
        features = ("donchian20", "atr14", "atr_expansion")
        if not (math.isfinite(upper) and math.isfinite(lower) and upper > lower):
            return 0, 0.5, [("INSUFFICIENT_DATA", "channel not defined")], {"features": features}
        position = (close[-1] - lower) / (upper - lower)
        expansion = atr / atr_avg if atr_avg and atr_avg > 0 and math.isfinite(atr) else 1.0
        details = {"channel_position": round(float(position), 3), "atr_expansion": round(float(expansion), 3),
                   "features": features}
        boost = 0.5 if expansion > 1.2 else 0.0
        if close[-1] > upper:
            return 1, _strength(1.0 + boost), [("BREAKOUT", f"close above the 20-bar high, ATR x{expansion:.2f}")], \
                details
        if close[-1] < lower:
            return -1, _strength(1.0 + boost), [("BREAKDOWN", f"close below the 20-bar low, ATR x{expansion:.2f}")], \
                details
        if position > 0.85:
            return 1, _strength(0.4 + boost), [("NEAR_HIGH", f"top of the channel ({position:.0%})")], details
        if position < 0.15:
            return -1, _strength(0.4 + boost), [("NEAR_LOW", f"bottom of the channel ({position:.0%})")], details
        return 0, 0.5, [("INSIDE_RANGE", f"inside the channel ({position:.0%})")], details


class MultiTimeframeAnalyst(DeskAgent):
    id = "mtf_analyst"
    TAIL = 2000  # the higher timeframe is built from these bars

    def owns(self, proposal: Optional[Proposal]) -> bool:
        return False  # confirms or questions; never proposes

    def bias(self, bars: np.ndarray, ctx: DecisionContext) -> tuple[int, float, list[tuple[str, str]], dict]:
        factor = HTF_FACTOR.get(ctx.market.timeframe, 4)
        close = np.asarray(bars["close"], dtype=float)
        htf = close[::-1][::factor][::-1]  # every factor-th close, aligned to the latest bar
        features = (f"ema20_x{factor}", f"ema50_x{factor}")
        if htf.size < 60:
            return 0, 0.5, [("INSUFFICIENT_DATA", f"{htf.size} higher-timeframe bars")], {"features": features}
        fast, slow = _last(indicators.ema(htf, 20)), _last(indicators.ema(htf, 50))
        scale = float(np.std(np.diff(htf[-100:]))) or 1e-12
        z = (fast - slow) / (scale * 3)
        direction = 1 if z > 0.2 else -1 if z < -0.2 else 0
        return direction, _strength(z), [("HTF_BIAS", f"higher timeframe x{factor}: {z:+.2f}")], \
            {"z": round(z, 4), "factor": factor, "features": features}
