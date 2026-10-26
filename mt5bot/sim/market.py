"""Synthetic multi-symbol FX market for ``--simulate`` and ``python -m panel --demo``.

Deterministic for a seed. Regimes switch by a sticky Markov chain (trend up, trend down,
range, high volatility); returns are correlated across symbols; spreads follow an
hour-of-day seasonality with rollover widening and rare spikes; weekends have no bars.
The data is SYNTHETIC: nothing learned on it says anything about real markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

RATES_DTYPE = np.dtype([
    ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
    ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8"),
])

REGIMES = ("trend_up", "trend_down", "range", "high_vol")
# Sticky transitions: a regime lasts on average ~1/(1-p) bars.
TRANSITION = np.array([
    [0.990, 0.002, 0.006, 0.002],
    [0.002, 0.990, 0.006, 0.002],
    [0.004, 0.004, 0.988, 0.004],
    [0.010, 0.010, 0.020, 0.960],
])


@dataclass(frozen=True)
class SymbolModel:
    name: str
    price: float
    point: float
    digits: int
    vol: float          # per-bar log-return volatility
    spread: int         # typical spread in points
    contract: float = 100_000.0


DEFAULT_SYMBOLS = (
    SymbolModel("EURUSD", 1.0850, 0.00001, 5, 0.00055, 8),
    SymbolModel("GBPUSD", 1.2700, 0.00001, 5, 0.00065, 12),
    SymbolModel("USDJPY", 150.00, 0.001, 3, 0.00060, 10),
)
# The demo office trades a wider universe, including gold (2 digits, 100 oz contract).
DEMO_SYMBOLS = DEFAULT_SYMBOLS + (
    SymbolModel("AUDUSD", 0.6600, 0.00001, 5, 0.00060, 10),
    SymbolModel("XAUUSD", 2350.0, 0.01, 2, 0.00090, 25, contract=100.0),
)
CORRELATION = np.array([[1.0, 0.70, -0.35], [0.70, 1.0, -0.30], [-0.35, -0.30, 1.0]])
VOL_REFERENCE_SECONDS = 900  # SymbolModel.vol is per M15 bar; other bar lengths scale by sqrt(time)


def spread_factor(server_hour: int) -> float:
    """Wider around the rollover (server midnight) and in the quiet Asian hours."""
    if server_hour in (23, 0):
        return 3.0
    if 1 <= server_hour < 8:
        return 1.4
    return 1.0


class SyntheticMarket:
    def __init__(self, seed: int = 7, symbols: tuple[SymbolModel, ...] = DEFAULT_SYMBOLS,
                 bar_seconds: int = 900, start_server_ts: int = 1_767_571_200, trend_strength: float = 0.18,
                 random_walk: bool = False) -> None:
        # 1_767_571_200 = Monday 2026-01-05 00:00 (server time)
        self.rng = np.random.default_rng(seed)
        self.symbols = symbols
        self.bar_seconds = bar_seconds
        self.trend_strength = 0.0 if random_walk else trend_strength
        self.random_walk = random_walk  # no drift and no mean reversion: no edge exists before costs
        self.vol_scale = float(np.sqrt(bar_seconds / VOL_REFERENCE_SECONDS))
        n = len(symbols)
        corr = np.eye(n)
        k = min(n, 3)
        corr[:k, :k] = CORRELATION[:k, :k]
        self._chol = np.linalg.cholesky(corr)
        self.regime = 2
        self.time = int(start_server_ts)
        self.close = np.array([s.price for s in symbols], dtype=float)
        self.anchor = self.close.copy()
        self.bars_made = 0

    @property
    def regime_name(self) -> str:
        return REGIMES[self.regime]

    def _advance_time(self) -> None:
        self.time += self.bar_seconds
        while True:
            weekday = ((self.time // 86400) + 3) % 7  # 1970-01-01 was a Thursday; Monday = 0
            if weekday < 5:
                return
            self.time += self.bar_seconds

    def next_bars(self) -> tuple[int, list[tuple[float, float, float, float, int, int]]]:
        """Advance one bar. Returns (server_time, [(open, high, low, close, spread, tick_volume) per symbol])."""
        self.regime = int(self.rng.choice(4, p=TRANSITION[self.regime]))
        bar_time = self.time
        hour = (bar_time % 86400) // 3600
        z = self._chol @ self.rng.standard_normal(len(self.symbols))
        out = []
        for i, sym in enumerate(self.symbols):
            vol = sym.vol * self.vol_scale * (2.5 if self.regime == 3 else 1.0) * (0.6 if 1 <= hour < 7 else 1.0)
            drift = 0.0
            if self.random_walk:
                drift = 0.0
            elif self.regime == 0:
                drift = self.trend_strength * vol
            elif self.regime == 1:
                drift = -self.trend_strength * vol
            elif self.regime == 2:
                drift = -0.02 * np.log(self.close[i] / self.anchor[i])  # mean reversion to the anchor
            if self.regime != 2:
                self.anchor[i] = self.anchor[i] * 0.995 + self.close[i] * 0.005
            sign = 1.0 if i < 2 else -1.0  # USDJPY moves against the USD-quote pairs on trend days
            ret = drift * (sign if self.regime in (0, 1) else 1.0) + vol * z[i]
            o = self.close[i]
            c = o * float(np.exp(ret))
            wiggle = abs(self.rng.normal(0, vol * 0.6)) * o
            h = max(o, c) + wiggle
            lo = min(o, c) - abs(self.rng.normal(0, vol * 0.6)) * o
            spread = int(round(sym.spread * spread_factor(hour) * (4.0 if self.rng.random() < 0.01 else 1.0)))
            tick_volume = int(max(1, self.rng.normal(120, 30) * (0.5 if 1 <= hour < 7 else 1.0)))
            out.append((round(o, sym.digits), round(h, sym.digits), round(lo, sym.digits), round(c, sym.digits),
                        spread, tick_volume))
            self.close[i] = c
        self._advance_time()
        self.bars_made += 1
        return bar_time, out

    def history(self, count: int) -> dict[str, np.ndarray]:
        """Generate ``count`` bars per symbol (used to warm up the terminal)."""
        arrays = {s.name: np.zeros(count, dtype=RATES_DTYPE) for s in self.symbols}
        for k in range(count):
            t, bars = self.next_bars()
            for sym, (o, h, lo, c, spr, vol) in zip(self.symbols, bars):
                row = arrays[sym.name][k]
                row["time"], row["open"], row["high"], row["low"], row["close"] = t, o, h, lo, c
                row["spread"], row["tick_volume"] = spr, vol
        return arrays

    def model(self, name: str) -> Optional[SymbolModel]:
        return next((s for s in self.symbols if s.name == name), None)
