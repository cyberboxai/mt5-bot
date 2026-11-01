"""Confirmation features, measured instead of assumed.

Each shadow trade records, at its signal bar: alignment with the higher-timeframe trend, the
liquidity session, the spread as a share of the stop, the volatility percentile, and whether the
bar sits just after rollover. Nothing filters on them by default. ``uplift`` measures each value
against the rest, and only a difference that is both well sampled and clearly negative or
positive changes a decision, through ``adjustment`` in the expected value.

The trend proxy uses a long EMA of the arm's own bars, about the slope of the next timeframe up,
so it needs no extra data request on the trading path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Optional

import numpy as np

from . import indicators

TREND_EMA = 200
VOL_LOOKBACK = 480
MIN_SAMPLES = 100
MIN_CONFIDENCE = 0.95
_N = NormalDist()


def session_of(hour: int) -> str:
    """Rough liquidity session from the bar's hour. Broker server time, usually UTC+2 or UTC+3."""
    if 10 <= hour < 15:
        return "london"
    if 15 <= hour < 20:
        return "new_york"
    if 2 <= hour < 10:
        return "asia"
    return "illiquid"


def _band(value: float, edges: tuple[float, float], names: tuple[str, str, str]) -> str:
    return names[0] if value < edges[0] else names[1] if value < edges[1] else names[2]


def signal_features(bars: np.ndarray, index: int, side: int, atr: float, sl_mult: float, point: float) -> dict[str, Any]:
    """Features of the signal at ``index``. Uses bars up to and including it: never looks ahead."""
    close = float(bars["close"][index])
    ema = indicators.cached(bars, f"feat_ema{TREND_EMA}", lambda: indicators.ema(bars["close"], TREND_EMA))
    trend = float(ema[index])
    pct = indicators.cached(bars, f"feat_atrpct{VOL_LOOKBACK}", lambda: indicators.rolling_percentile_rank(
        indicators.bars_atr(bars, 14), VOL_LOOKBACK))
    hour = int((int(bars["time"][index]) % 86400) // 3600)
    stop = atr * sl_mult
    spread_share = float(bars["spread"][index]) * point / stop if stop > 0 else math.inf
    vol = float(pct[index])
    out: dict[str, Any] = {"session": session_of(hour), "rollover": hour < 1 or hour >= 23,
                           "spread_stop": _band(spread_share, (0.08, 0.16), ("tight", "normal", "wide"))}
    if math.isfinite(trend):
        out["htf"] = "with" if (close > trend) == (side > 0) else "against"
    if math.isfinite(vol):
        out["vol"] = _band(vol, (20.0, 80.0), ("low", "mid", "high"))
    return out


@dataclass(frozen=True)
class Uplift:
    feature: str
    value: str
    n: int
    mean_r: float
    rest_mean_r: float
    diff: float
    confidence: float    # probability that the difference has the sign it shows

    @property
    def validated(self) -> bool:
        return self.n >= MIN_SAMPLES and self.confidence >= MIN_CONFIDENCE

    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "value": self.value, "n": self.n, "mean_r": round(self.mean_r, 4),
                "rest_mean_r": round(self.rest_mean_r, 4), "diff": round(self.diff, 4),
                "confidence": round(self.confidence, 4), "validated": self.validated}


def uplift(rows: Iterable[tuple[Mapping[str, Any], float]]) -> list[Uplift]:
    """For every feature value: its mean R against all the other trades with that feature."""
    data: dict[str, dict[str, list[float]]] = {}
    for features, r in rows:
        for name, value in features.items():
            data.setdefault(name, {}).setdefault(str(value), []).append(float(r))
    out: list[Uplift] = []
    for name, values in sorted(data.items()):
        for value, rs in sorted(values.items()):
            rest = [x for other, items in values.items() if other != value for x in items]
            if len(rs) < 2 or len(rest) < 2:
                continue
            a, b = np.asarray(rs), np.asarray(rest)
            se = math.sqrt(a.var(ddof=1) / a.size + b.var(ddof=1) / b.size)
            diff = float(a.mean() - b.mean())
            confidence = _N.cdf(abs(diff) / se) if se > 0 else 0.5
            out.append(Uplift(name, value, int(a.size), float(a.mean()), float(b.mean()), diff, confidence))
    return out


def adjustment(features: Optional[Mapping[str, Any]], table: Iterable[Uplift]) -> float:
    """R to add to a signal's EV for its validated feature values. Half the measured difference:
    a measured effect shrinks out of sample, and several features overlap."""
    if not features:
        return 0.0
    total = 0.0
    for item in table:
        if item.validated and str(features.get(item.feature)) == item.value:
            total += 0.5 * item.diff
    return total
