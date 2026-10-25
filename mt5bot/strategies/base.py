"""Strategy interface. Strategies only propose a direction and a volatility estimate.

Sizing, SL/TP placement, filters and every safety rule live in ``mt5bot.risk``.
A strategy must be deterministic, use only closed bars and never look ahead:
``signals(bars[:k])`` must equal ``signals(bars)[:k]`` for every ``k``.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Sequence

import numpy as np

from .. import indicators, limits

Side = Literal["BUY", "SELL"]
ParamValue = Any  # int, float or str
Params = Mapping[str, ParamValue]

ATR_PERIOD = 14


@dataclass(frozen=True)
class Signal:
    """Entry signal. ``atr`` is in price units; SL/TP distances are ``atr * mult``."""

    side: Side
    atr: float
    sl_mult: Optional[float] = None
    tp_mult: Optional[float] = None

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Signal side must be BUY or SELL, got {self.side!r}")
        if not (isinstance(self.atr, (int, float)) and math.isfinite(self.atr) and self.atr > 0):
            raise ValueError(f"Signal atr must be a positive finite number, got {self.atr!r}")
        for name, low, high in (("sl_mult", limits.MIN_SL_ATR_MULT, limits.MAX_SL_ATR_MULT),
                                ("tp_mult", limits.MIN_TP_ATR_MULT, limits.MAX_TP_ATR_MULT)):
            value = getattr(self, name)
            if value is not None and not (isinstance(value, (int, float)) and low <= value <= high):
                raise ValueError(f"Signal {name} must be within [{low}, {high}], got {value!r}")


@dataclass(frozen=True)
class Param:
    """A bounded, typed, discrete parameter. The grid is the complete set of legal values."""

    name: str
    values: tuple[ParamValue, ...]

    @classmethod
    def int_range(cls, name: str, low: int, high: int, step: int = 1) -> "Param":
        return cls(name, tuple(range(low, high + 1, step)))

    @classmethod
    def float_range(cls, name: str, low: float, high: float, step: float) -> "Param":
        count = int(round((high - low) / step))
        return cls(name, tuple(round(low + i * step, 10) for i in range(count + 1)))

    @classmethod
    def choice(cls, name: str, *options: str) -> "Param":
        return cls(name, tuple(options))

    def __post_init__(self) -> None:
        if not self.values or len(set(self.values)) != len(self.values):
            raise ValueError(f"Param {self.name}: values must be non-empty and unique")

    def contains(self, value: ParamValue) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, float) or isinstance(self.values[0], float):
            return any(isinstance(v, (int, float)) and isinstance(value, (int, float))
                       and math.isclose(v, value, abs_tol=1e-9) for v in self.values)
        return value in self.values

    def index(self, value: ParamValue) -> int:
        for i, v in enumerate(self.values):
            if v == value or (isinstance(v, float) and isinstance(value, (int, float))
                              and math.isclose(v, value, abs_tol=1e-9)):
                return i
        raise ValueError(f"{value!r} not in {self.name}")

    def neighbors(self, value: ParamValue) -> list[ParamValue]:
        """Adjacent grid values (one step either side). Choice params have no order: all others."""
        if isinstance(self.values[0], str):
            return [v for v in self.values if v != value]
        i = self.index(value)
        return [self.values[j] for j in (i - 1, i + 1) if 0 <= j < len(self.values)]


@dataclass(frozen=True)
class SignalArrays:
    """Vectorised strategy output: side[i] in {+1, -1, 0} for the bar that closed at i."""

    side: np.ndarray
    atr: np.ndarray
    sl_mult: float
    tp_mult: float


def canonical_params(params: Params) -> dict[str, ParamValue]:
    return {k: params[k] for k in sorted(params)}


class Strategy(ABC):
    """Base class. Subclasses define ``name``, ``param_space`` and ``compute``."""

    name: str = ""
    description: str = ""
    param_space: tuple[Param, ...] = ()
    default_sl_mult: float = 2.0
    default_tp_mult: float = 3.0

    # ---- parameters

    def validate_params(self, params: Params) -> dict[str, ParamValue]:
        """Return canonical params or raise ValueError if any value is outside its bounds."""
        if not isinstance(params, Mapping):
            raise ValueError("params must be a mapping")
        expected = {p.name for p in self.param_space}
        if set(params) != expected:
            raise ValueError(f"{self.name}: params must be exactly {sorted(expected)}, got {sorted(params)}")
        for p in self.param_space:
            if not p.contains(params[p.name]):
                raise ValueError(f"{self.name}: {p.name}={params[p.name]!r} outside bounds {p.values}")
        if not self.is_valid(params):
            raise ValueError(f"{self.name}: parameter combination {dict(params)} violates constraints")
        return canonical_params(params)

    def is_valid(self, params: Params) -> bool:
        """Cross-parameter constraints (e.g. fast < slow). Default: none."""
        return True

    def default_params(self) -> dict[str, ParamValue]:
        params = {p.name: p.values[len(p.values) // 2] for p in self.param_space}
        if not self.is_valid(params):
            raise RuntimeError(f"{self.name}: default params violate constraints; override default_params")
        return canonical_params(params)

    def grid_size(self) -> int:
        size = 1
        for p in self.param_space:
            size *= len(p.values)
        return size

    def sample_params(self, rng: np.random.Generator, count: int) -> list[dict[str, ParamValue]]:
        """Up to ``count`` distinct valid parameter sets drawn uniformly from the grid."""
        seen: set[tuple] = set()
        out: list[dict[str, ParamValue]] = []
        attempts = 0
        while len(out) < count and attempts < count * 50:
            attempts += 1
            params = {p.name: p.values[int(rng.integers(len(p.values)))] for p in self.param_space}
            key = tuple(sorted(params.items()))
            if key in seen or not self.is_valid(params):
                continue
            seen.add(key)
            out.append(canonical_params(params))
        return out

    def neighbors(self, params: Params) -> list[dict[str, ParamValue]]:
        """Valid parameter sets that differ from ``params`` by one grid step in one dimension."""
        out = []
        for p in self.param_space:
            for value in p.neighbors(params[p.name]):
                candidate = dict(params)
                candidate[p.name] = value
                if self.is_valid(candidate):
                    out.append(canonical_params(candidate))
        return out

    # ---- signals

    @abstractmethod
    def warmup_bars(self, params: Params) -> int:
        """Bars needed before the first signal can be trusted."""

    @abstractmethod
    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        """Return int8 side array (+1 buy, -1 sell, 0 none), one entry per closed bar."""

    def sl_tp_mults(self, params: Params) -> tuple[float, float]:
        return (float(params.get("sl_mult", self.default_sl_mult)),
                float(params.get("tp_mult", self.default_tp_mult)))

    def signals(self, bars: np.ndarray, params: Params) -> SignalArrays:
        """Vectorised signals over all closed bars, with warm-up and invalid ATR masked out."""
        params = self.validate_params(params)
        n = len(bars)
        vol = indicators.atr(bars["high"], bars["low"], bars["close"], ATR_PERIOD)
        side = np.zeros(n, dtype=np.int8)
        warm = self.warmup_bars(params)
        if n > warm:
            raw = np.asarray(self.compute(bars, params), dtype=np.int8)
            if raw.shape != (n,):
                raise RuntimeError(f"{self.name}.compute returned shape {raw.shape}, expected {(n,)}")
            side = raw.copy()
            side[:warm] = 0
        ok = np.isfinite(vol) & (vol > 0)
        side[~ok] = 0
        sl, tp = self.sl_tp_mults(params)
        return SignalArrays(side=side, atr=np.where(ok, vol, np.nan), sl_mult=sl, tp_mult=tp)

    def signal(self, closed_bars: np.ndarray, params: Params) -> Optional[Signal]:
        """Signal for the most recent closed bar; same code path as the backtester."""
        if closed_bars is None or len(closed_bars) < 2:
            return None
        arrays = self.signals(closed_bars, params)
        side = int(arrays.side[-1])
        if side == 0:
            return None
        return Signal("BUY" if side > 0 else "SELL", float(arrays.atr[-1]), arrays.sl_mult, arrays.tp_mult)


def crosses_above(a: np.ndarray, b: Any) -> np.ndarray:
    """True at i when a[i-1] <= b[i-1] and a[i] > b[i] (NaN-safe: NaN gives False)."""
    a = np.asarray(a, dtype=float)
    b = np.broadcast_to(np.asarray(b, dtype=float), a.shape)
    out = np.zeros(a.shape, dtype=bool)
    with np.errstate(invalid="ignore"):
        out[1:] = (a[:-1] <= b[:-1]) & (a[1:] > b[1:])
    return out


def crosses_below(a: np.ndarray, b: Any) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    b = np.broadcast_to(np.asarray(b, dtype=float), a.shape)
    out = np.zeros(a.shape, dtype=bool)
    with np.errstate(invalid="ignore"):
        out[1:] = (a[:-1] >= b[:-1]) & (a[1:] < b[1:])
    return out


def sides_from(buy: np.ndarray, sell: np.ndarray) -> np.ndarray:
    side = np.zeros(len(buy), dtype=np.int8)
    side[buy & ~sell] = 1
    side[sell & ~buy] = -1
    return side


def describe(strategy: Strategy, params: Params) -> str:
    inner = ", ".join(f"{k}={v}" for k, v in canonical_params(params).items())
    return f"{strategy.name}({inner})"


def param_names(space: Sequence[Param]) -> list[str]:
    return [p.name for p in space]
