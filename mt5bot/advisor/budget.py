"""Local spend control for the advisor: calls per rolling hour, cost per UTC day and per month.

A call RESERVES its worst-case cost (``max_tokens`` at the output price plus the estimated
input) before it is sent and SETTLES the real cost from the usage block afterwards, so
concurrent calls can never overshoot the daily cap. State is restored from the audit table
on start, so a restart does not reset the caps.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .types import CallResult

DAY = 86400.0
HOUR = 3600.0


def estimate_cost(price: Any, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """USD estimate from per-million-token prices (``price`` has input/output/cache_read/cache_write)."""
    return (input_tokens * price.input + output_tokens * price.output + cache_read * price.cache_read
            + cache_write * price.cache_write) / 1_000_000.0


class CallBudget:
    def __init__(self, max_calls_per_hour: int, max_daily_cost_usd: float, clock: Callable[[], float] = time.time,
                 restore: Optional[Callable[[float, float], tuple[list[float], float]]] = None,
                 max_monthly_cost_usd: float = math.inf) -> None:
        self.max_calls_per_hour = int(max_calls_per_hour)
        self.max_daily_cost_usd = float(max_daily_cost_usd)
        self.max_monthly_cost_usd = float(max_monthly_cost_usd)
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()
        self._day = self._day_key(clock())
        self._spent = 0.0
        self._reserved = 0.0
        self._month = self._month_key(clock())
        self._month_spent = 0.0
        if restore is not None:
            now = clock()
            try:
                calls, spent = restore(now - HOUR, self._day * DAY)
                self._calls.extend(sorted(t for t in calls if t >= now - HOUR))
                self._spent = max(0.0, float(spent))
                self._month_spent = max(0.0, float(restore(now, self._month_start(now))[1]))
            except Exception:  # noqa: BLE001 - an unreadable history must not stop the bot; caps start empty
                pass

    @staticmethod
    def _day_key(ts: float) -> int:
        return int(ts // DAY)

    @staticmethod
    def _month_key(ts: float) -> tuple[int, int]:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        return d.year, d.month

    @staticmethod
    def _month_start(ts: float) -> float:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()

    @staticmethod
    def _next_month(ts: float) -> float:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()

    def update_limits(self, max_calls_per_hour: int, max_daily_cost_usd: float,
                      max_monthly_cost_usd: Optional[float] = None) -> None:
        with self._lock:
            self.max_calls_per_hour = int(max_calls_per_hour)
            self.max_daily_cost_usd = float(max_daily_cost_usd)
            if max_monthly_cost_usd is not None:
                self.max_monthly_cost_usd = float(max_monthly_cost_usd)

    def _roll(self, now: float) -> None:
        while self._calls and self._calls[0] <= now - HOUR:
            self._calls.popleft()
        day = self._day_key(now)
        if day != self._day:
            self._day, self._spent, self._reserved = day, 0.0, 0.0
        month = self._month_key(now)
        if month != self._month:
            self._month, self._month_spent = month, 0.0

    def reserve(self, worst_case_usd: float) -> tuple[bool, str, float]:
        """Try to reserve one call. Returns (allowed, reason, resume_at)."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            if len(self._calls) >= self.max_calls_per_hour:
                resume = self._calls[0] + HOUR
                return False, f"max_calls_per_hour ({self.max_calls_per_hour}) reached", resume
            if self._spent + self._reserved + worst_case_usd > self.max_daily_cost_usd:
                resume = (self._day + 1) * DAY
                return False, (f"max_daily_cost_usd ({self.max_daily_cost_usd:.2f}) would be exceeded "
                               f"(spent {self._spent:.4f})"), resume
            if self._month_spent + self._reserved + worst_case_usd > self.max_monthly_cost_usd:
                return False, (f"max_monthly_cost_usd ({self.max_monthly_cost_usd:.2f}) would be exceeded "
                               f"(spent {self._month_spent:.4f})"), self._next_month(now)
            self._calls.append(now)
            self._reserved += worst_case_usd
            return True, "ok", now

    def settle(self, reserved_usd: float, result: Optional[CallResult]) -> None:
        with self._lock:
            self._roll(self._clock())
            self._reserved = max(0.0, self._reserved - reserved_usd)
            if result is not None:
                self._spent += max(0.0, result.cost_usd)
                self._month_spent += max(0.0, result.cost_usd)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll(self._clock())
            return {"calls_last_hour": len(self._calls), "max_calls_per_hour": self.max_calls_per_hour,
                    "spent_today_usd": round(self._spent, 6), "reserved_usd": round(self._reserved, 6),
                    "max_daily_cost_usd": self.max_daily_cost_usd,
                    "spent_month_usd": round(self._month_spent, 6), "max_monthly_cost_usd": self.max_monthly_cost_usd}
