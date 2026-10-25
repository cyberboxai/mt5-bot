"""Market awareness: broker server clock, tick staleness, weekends and holidays.

MT5 reports tick and bar times as seconds in the trade server's own timezone (not
true UTC), so interpreting them as UTC yields the server's wall clock. The offset to
real UTC is detected from fresh ticks and rounded to a quarter hour.

The MT5 Python API does not expose the per-symbol trading session schedule (that is
MQL5's SymbolInfoSessionTrade), so "market open" is decided from tick freshness, the
symbol trade mode, the server weekday and the configured holiday list.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

QUARTER_HOUR = 900
MAX_OFFSET = 14 * 3600
# Fallback when no fresh tick has ever been seen: most MT5 brokers run on GMT+2/GMT+3.
DEFAULT_OFFSET_HOURS = 2


def server_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def fmt_server_time(timestamp: float) -> str:
    return server_datetime(timestamp).strftime("%Y-%m-%d %H:%M server")


class ServerClock:
    """Tracks the broker server clock and per-symbol tick freshness."""

    def __init__(self, utc_now: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 offset: Optional[int] = None) -> None:
        self._utc_now = utc_now
        self._monotonic = monotonic
        self.offset = offset
        self._last_tick: dict[str, tuple[int, float, float]] = {}
        self._last_change: dict[str, float] = {}
        self._started = monotonic()
        self.latest_tick_time = 0

    def observe(self, symbol: str, tick_time: int, bid: float, ask: float) -> bool:
        """Record a tick snapshot. Returns True if it changed since the previous snapshot."""
        key = (int(tick_time), float(bid), float(ask))
        previous = self._last_tick.get(symbol)
        self._last_tick[symbol] = key
        self.latest_tick_time = max(self.latest_tick_time, int(tick_time))
        if previous is None or previous == key:
            return False
        self._last_change[symbol] = self._monotonic()
        if previous[0] != key[0]:
            raw = int(tick_time) - self._utc_now()
            offset = int(round(raw / QUARTER_HOUR)) * QUARTER_HOUR
            if abs(offset) <= MAX_OFFSET and abs(raw - offset) < 180:
                self.offset = offset
        return True

    def now(self) -> float:
        """Server timestamp now. Without a known offset, uses the default broker offset."""
        offset = self.offset if self.offset is not None else DEFAULT_OFFSET_HOURS * 3600
        return self._utc_now() + offset

    def seconds_since_change(self, symbol: str) -> Optional[float]:
        changed = self._last_change.get(symbol)
        return None if changed is None else self._monotonic() - changed

    def is_fresh(self, symbol: str, stale_seconds: float) -> bool:
        """Ticks are moving for this symbol (or we started too recently to tell)."""
        since = self.seconds_since_change(symbol)
        if since is None:
            if self.offset is not None and symbol in self._last_tick:
                return self.now() - self._last_tick[symbol][0] < stale_seconds
            return self._monotonic() - self._started < stale_seconds
        return since < stale_seconds

    def all_stale_for(self, symbols: Iterable[str]) -> float:
        """Seconds since ANY of the symbols last ticked (or since start)."""
        changes = [self._last_change[s] for s in symbols if s in self._last_change]
        reference = max(changes) if changes else self._started
        return self._monotonic() - reference


def is_weekend(server_ts: float) -> bool:
    return server_datetime(server_ts).weekday() >= 5


def is_holiday(server_ts: float, holidays: Iterable[str]) -> bool:
    return server_datetime(server_ts).date().isoformat() in set(holidays)


def market_expected_open(server_ts: float, holidays: Iterable[str]) -> bool:
    """Should a weekday FX market be ticking now? Conservative: excludes rollover edges."""
    dt = server_datetime(server_ts)
    if dt.weekday() >= 5 or is_holiday(server_ts, holidays):
        return False
    if dt.weekday() == 0 and dt.hour < 1:
        return False
    if dt.weekday() == 4 and dt.hour >= 22:
        return False
    return 1 <= dt.hour < 23
