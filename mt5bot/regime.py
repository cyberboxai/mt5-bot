"""Market regime of a bar series: trend, range, high volatility or illiquid.

Used by the Regime Detector agent (the office's live view) and by the arms, which tag every
shadow and live trade with the regime of its signal bar so that evidence can be read per
regime. Causal: only the bars passed in (closed bars) are used.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import indicators

REGIME_LABELS = ("trend", "range", "high_vol", "illiquid")
LABEL_PT = {"trend": "tendência", "range": "lateral", "high_vol": "alta volatilidade", "illiquid": "ilíquido"}

REGIME_TAIL = 700  # bars; Wilder smoothing has long forgotten its seed by then


def regime_features(bars: np.ndarray) -> dict[str, float]:
    bars = bars[-REGIME_TAIL:]
    close = np.asarray(bars["close"], dtype=float)
    high = np.asarray(bars["high"], dtype=float)
    low = np.asarray(bars["low"], dtype=float)
    er = indicators.efficiency_ratio(close[-60:], 20)
    ad = indicators.adx(high[-300:], low[-300:], close[-300:], 14)
    atr = indicators.atr(high, low, close, 14)
    window = atr[-480:]
    window = window[np.isfinite(window)]
    # Only the latest percentile is needed: rank the last ATR within the last 480 values.
    pct = np.array([float((window <= window[-1]).mean() * 100.0)]) if window.size >= 20 else np.array([50.0])
    vol = np.asarray(bars["tick_volume"], dtype=float)
    vol_ratio = float(np.median(vol[-4:]) / max(np.median(vol[-200:]), 1e-9)) if vol.size >= 20 else 1.0

    def last(x: np.ndarray, default: float) -> float:
        value = float(x[-1]) if x.size else default
        return value if math.isfinite(value) else default

    return {"er": last(er, 0.3), "adx": last(ad, 20.0), "atr_pct": last(pct, 50.0), "volume_ratio": vol_ratio,
            "atr": last(atr, 0.0)}


def regime_probabilities(f: dict[str, float], spread_ratio: float = 1.0) -> dict[str, float]:
    scores = {
        "trend": (f["er"] - 0.30) * 6.0 + (f["adx"] - 22.0) / 8.0,
        "range": (0.25 - f["er"]) * 6.0 + (20.0 - f["adx"]) / 8.0,
        "high_vol": (f["atr_pct"] - 85.0) / 6.0,
        "illiquid": (0.45 - f["volume_ratio"]) * 6.0 + (spread_ratio - 2.5) * 1.5,
    }
    top = max(scores.values())
    exp = {k: math.exp(v - top) for k, v in scores.items()}
    total = sum(exp.values())
    return {k: exp[k] / total for k in REGIME_LABELS}


def regime_label(bars: np.ndarray) -> str:
    """Most likely regime of the latest closed bar (spread is not part of the arms' label)."""
    probs = regime_probabilities(regime_features(bars))
    return max(probs, key=probs.get)


def _rolling_median(x: np.ndarray, window: int, chunk: int = 4096) -> np.ndarray:
    """Median of the last ``window`` values at every index (NaN before the first full window)."""
    out = np.full(x.shape, np.nan)
    if x.size < window:
        return out
    windows = sliding_window_view(x, window)
    hi, lo = window // 2, (window - 1) // 2
    for start in range(0, windows.shape[0], chunk):
        block = windows[start:start + chunk]
        if window <= 16:  # partitioning is much faster than np.median on tiny windows
            part = np.partition(block, (lo, hi), axis=1)
            values = (part[:, lo] + part[:, hi]) / 2.0
        else:
            values = np.median(block, axis=1)
        out[window - 1 + start:window - 1 + start + len(values)] = values
    return out


def regime_codes(bars: np.ndarray) -> np.ndarray:
    """Regime of every bar (index into ``REGIME_LABELS``), vectorised and memoised per bar
    series. Same features and scores as ``regime_features``/``regime_probabilities``; the
    ATR percentile and the volume ratio use full rolling windows."""

    def compute() -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        high = np.asarray(bars["high"], dtype=float)
        low = np.asarray(bars["low"], dtype=float)
        er = np.nan_to_num(indicators.efficiency_ratio(close, 20), nan=0.3)
        ad = np.nan_to_num(indicators.cached(bars, "adx14", lambda: indicators.adx(high, low, close, 14)), nan=20.0)
        pct = np.nan_to_num(indicators.rolling_percentile_rank(indicators.bars_atr(bars, 14), 480), nan=50.0)
        vol = np.asarray(bars["tick_volume"], dtype=float)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = _rolling_median(vol, 4) / np.maximum(_rolling_median(vol, 200), 1e-9)
        ratio = np.nan_to_num(ratio, nan=1.0)
        scores = np.vstack([(er - 0.30) * 6.0 + (ad - 22.0) / 8.0,
                            (0.25 - er) * 6.0 + (20.0 - ad) / 8.0,
                            (pct - 85.0) / 6.0,
                            (0.45 - ratio) * 6.0 + (1.0 - 2.5) * 1.5])
        return np.argmax(scores, axis=0).astype(np.int8)

    return indicators.cached(bars, "regime", compute)


def label_at(bars: np.ndarray, index: int = -1) -> str:
    """Regime label of bar ``index`` (default: the latest closed bar)."""
    if len(bars) == 0:
        return "range"
    return REGIME_LABELS[int(regime_codes(bars)[index])]
