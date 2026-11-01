"""``SimBroker``: broker and market data for backtest, replay and ``--simulate``.

A bar feed drives it: ``sim.market.SyntheticMarket`` (generated) or ``SnapshotFeed`` (a stored
data snapshot, for exact replay). Each step closes one base bar, fills server-side stops, charges
swap at the server rollover, enforces the stop-out and opens the next forming bar. Bars of longer
timeframes are aggregated from the base bars, so a run only ever sees closed information.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from ..core import broker as b
from ..sim.market import RATES_DTYPE, SymbolModel
from .exchange import SimCosts, SimExchange

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
SERVER_OFFSET = 2 * 3600
SIM_LOGIN = 10_000_001
SIM_SERVER = "CYBERBOX-Simulation"
BUFFER_SLACK = 4096


class SnapshotFeed:
    """Replays stored bars exactly. ``history`` serves the warm-up, ``next_bars`` the rest."""

    def __init__(self, bars: Mapping[str, np.ndarray], models: Sequence[SymbolModel], bar_seconds: int) -> None:
        self.bars = dict(bars)
        self.symbols = tuple(models)
        self.bar_seconds = bar_seconds
        self.cursor = 0
        self.time = int(next(iter(self.bars.values()))["time"][0])

    @property
    def length(self) -> int:
        return min(len(a) for a in self.bars.values())

    def model(self, name: str) -> Optional[SymbolModel]:
        return next((s for s in self.symbols if s.name == name), None)

    def history(self, count: int) -> dict[str, np.ndarray]:
        self.cursor = count
        self._sync()
        return {s.name: np.asarray(self.bars[s.name][:count].copy(), dtype=RATES_DTYPE) for s in self.symbols}

    def _sync(self) -> None:
        first = self.bars[self.symbols[0].name]
        if self.cursor < len(first):
            self.time = int(first["time"][self.cursor])

    def next_bars(self) -> tuple[int, list[tuple[float, float, float, float, int, int]]]:
        if self.cursor >= self.length:
            raise StopIteration("snapshot exhausted")
        out = []
        t = 0
        for s in self.symbols:
            r = self.bars[s.name][self.cursor]
            t = int(r["time"])
            out.append((float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), int(r["spread"]),
                        int(r["tick_volume"])))
        self.cursor += 1
        self._sync()
        return t, out


class SimBroker(SimExchange):
    def __init__(self, feed: Any, warmup_bars: int = 1500, costs: SimCosts = SimCosts(), balance: float = 10_000.0,
                 seed: Optional[int] = None, leverage: int = 100, swap_long: float = -0.5, swap_short: float = -0.3,
                 stops_level: int = 0, volume_max: float = 50.0) -> None:
        rng_seed = int(feed.rng.integers(1 << 31)) if seed is None and hasattr(feed, "rng") else int(seed or 1)
        super().__init__(costs, balance, rng_seed, leverage, SIM_LOGIN, SIM_SERVER)
        self.feed = feed
        self.swap = (swap_long, swap_short)
        self.stops_level = stops_level
        self.volume_max = volume_max
        history = feed.history(warmup_bars)
        self._buf: dict[str, np.ndarray] = {}
        self._n = warmup_bars
        for name, rows in history.items():
            buf = np.zeros(len(rows) + BUFFER_SLACK, dtype=RATES_DTYPE)
            buf[:len(rows)] = rows
            self._buf[name] = buf
        self._open_forming_bars()

    # ---- SimExchange hooks

    def quote(self, symbol: str) -> Any:
        return self.tick(symbol)

    def contract(self, symbol: str) -> float:
        model = self.feed.model(symbol)
        return float(model.contract) if model else 100_000.0

    def point(self, symbol: str) -> float:
        model = self.feed.model(symbol)
        return float(model.point) if model else 0.00001

    def digits(self, symbol: str) -> int:
        model = self.feed.model(symbol)
        return int(model.digits) if model else 5

    def swap_points(self, symbol: str, is_buy: bool) -> float:
        return self.swap[0] if is_buy else self.swap[1]

    def server_time(self) -> int:
        return int(self._last(self.feed.symbols[0].name)["time"]) + 60

    # ---- clock

    def _last(self, name: str) -> Any:
        return self._buf[name][self._n]

    def utc_now(self) -> float:
        return float(self.server_time() - SERVER_OFFSET)

    def _open_forming_bars(self) -> None:
        t = self.feed.time
        for buf in self._buf.values():
            last = buf[self._n - 1]
            row = buf[self._n]
            row["time"] = t
            row["open"] = row["high"] = row["low"] = row["close"] = last["close"]
            row["spread"], row["tick_volume"] = last["spread"], 1

    def advance_bar(self) -> int:
        """Close the forming bar with the next feed bar, fill stops, open a new forming bar."""
        day_before = self.server_time() // 86400
        bar_time, bars = self.feed.next_bars()
        for sym, (o, h, lo, c, spr, vol) in zip(self.feed.symbols, bars):
            row = self._buf[sym.name][self._n]
            row["time"], row["open"], row["high"], row["low"], row["close"] = bar_time, o, h, lo, c
            row["spread"], row["tick_volume"] = spr, vol
            self.fill_stops(sym.name, o, h, lo, spr * sym.point)
        self._n += 1
        if self._n + 2 >= len(next(iter(self._buf.values()))):
            self._grow()
        self._open_forming_bars()
        day_after = self.server_time() // 86400
        if day_after != day_before and self.positions_book:
            weekday = (day_before + 3) % 7   # 1970-01-01 was a Thursday; Monday = 0
            if weekday < 5:
                self.charge_rollover(3 if (weekday + 1) % 7 == self.swap_triple_weekday() else 1)
        self.enforce_stop_out()
        return bar_time

    def _grow(self) -> None:
        """Bounded memory: keep the newest bars when the buffer fills."""
        keep = 12_000
        for name, buf in self._buf.items():
            kept = buf[max(0, self._n - keep):self._n + 1].copy()
            new = np.zeros(len(kept) + BUFFER_SLACK, dtype=RATES_DTYPE)
            new[:len(kept)] = kept
            self._buf[name] = new
        self._n = min(self._n, keep)

    # ---- MarketDataPort

    def select_symbol(self, symbol: str) -> bool:
        return symbol in self._buf

    def symbols(self, group: Optional[str] = None) -> Sequence[Any]:
        needle = (group or "").strip("*").upper()
        return tuple(self.symbol_info(n) for n in self._buf if needle in n.upper())

    def symbol_info(self, symbol: str) -> Any:
        model = self.feed.model(symbol)
        if model is None:
            return None
        price = float(self._last(symbol)["close"])
        return SimpleNamespace(
            name=symbol, point=model.point, digits=model.digits, trade_mode=b.SYMBOL_TRADE_MODE_FULL,
            trade_stops_level=self.stops_level, trade_freeze_level=0, volume_min=0.01, volume_max=self.volume_max,
            volume_step=0.01, filling_mode=2, trade_contract_size=model.contract,
            trade_tick_value=model.point * self.value_per_price(symbol, price), trade_tick_size=model.point,
            swap_long=self.swap[0], swap_short=self.swap[1], swap_mode=1, swap_rollover3days=3,
            currency_base=symbol[:3], currency_profit=symbol[3:6])

    def tick(self, symbol: str) -> Any:
        if symbol not in self._buf:
            return None
        last = self._last(symbol)
        bid = float(last["close"])
        ask = round(bid + int(last["spread"]) * self.point(symbol), self.digits(symbol))
        return SimpleNamespace(time=int(last["time"]) + 60, bid=bid, ask=ask)

    def rates(self, symbol: str, timeframe: str, start: int, count: int) -> Any:
        buf = self._buf.get(symbol)
        if buf is None:
            return None
        end = self._n + 1          # the forming bar counts as position 0
        seconds = TIMEFRAME_SECONDS.get(timeframe, self.feed.bar_seconds)
        if seconds <= self.feed.bar_seconds:
            stop = end - start
            return buf[max(0, stop - count):stop].copy()
        ratio = seconds // self.feed.bar_seconds
        base = buf[max(0, end - (count + start + 2) * ratio):end]
        out = self._aggregate(base, seconds)
        stop = len(out) - start
        return out[max(0, stop - count):stop]

    @staticmethod
    def _aggregate(base: np.ndarray, seconds: int) -> np.ndarray:
        times = np.asarray(base["time"], dtype=np.int64)
        bucket = times // seconds
        starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
        ends = np.r_[starts[1:], len(base)]
        out = np.zeros(len(starts), dtype=RATES_DTYPE)
        out["time"] = bucket[starts] * seconds
        out["open"] = base["open"][starts]
        out["close"] = base["close"][ends - 1]
        out["high"] = np.maximum.reduceat(np.asarray(base["high"]), starts)
        out["low"] = np.minimum.reduceat(np.asarray(base["low"]), starts)
        out["tick_volume"] = np.add.reduceat(np.asarray(base["tick_volume"], dtype=np.uint64), starts)
        out["spread"] = np.minimum.reduceat(np.asarray(base["spread"]), starts)
        return out
