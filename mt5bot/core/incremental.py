"""O(1) per-bar indicator updates. Each class reproduces one batch function of ``indicators``.

``update`` takes the next value and returns the indicator at that index (NaN until defined). The
recursions and windows are the batch definitions, so a test can compare them directly: an
incremental value that differs from its batch twin is a bug, not an approximation.
"""

from __future__ import annotations

import bisect
import math
from collections import deque

NAN = math.nan


class Ema:
    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.keep = 1.0 - self.alpha
        self.n = 0
        self.total = 0.0
        self.value = NAN

    def update(self, x: float) -> float:
        self.n += 1
        if self.n < self.period:
            self.total += x
            return NAN
        if self.n == self.period:
            self.value = (self.total + x) / self.period
        else:
            self.value = self.alpha * x + self.keep * self.value
        return self.value


class Sma:
    """Same running cumulative sum as the batch version."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.sums: deque[float] = deque([0.0], maxlen=period + 1)
        self.total = 0.0

    def update(self, x: float) -> float:
        self.total += x
        self.sums.append(self.total)
        if len(self.sums) <= self.period:
            return NAN
        return (self.total - self.sums[0]) / self.period


class RollingStd:
    """Population standard deviation over a ring buffer, re-summed each window to stop drift."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.ring: deque[float] = deque(maxlen=period)
        self.s1 = 0.0
        self.s2 = 0.0
        self.since = 0

    def update(self, x: float) -> float:
        if len(self.ring) == self.period:
            old = self.ring[0]
            self.s1 -= old
            self.s2 -= old * old
        self.ring.append(x)
        self.s1 += x
        self.s2 += x * x
        self.since += 1
        if self.since >= self.period:
            self.since = 0
            self.s1 = math.fsum(self.ring)
            self.s2 = math.fsum(v * v for v in self.ring)
        if len(self.ring) < self.period or self.period < 2:
            return NAN
        mean = self.s1 / self.period
        return math.sqrt(max(self.s2 / self.period - mean * mean, 0.0))


class WilderAtr:
    """Takes (high, low, close). First defined value at index ``period``."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.n = 0
        self.prev_close = NAN
        self.total = 0.0
        self.value = NAN

    def update(self, high: float, low: float, close: float) -> float:
        self.n += 1
        if self.n == 1:
            self.prev_close = close
            return NAN
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        filled = self.n - 1
        if filled < self.period:
            self.total += tr
            return NAN
        if filled == self.period:
            self.value = (self.total + tr) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class WilderRsi:
    def __init__(self, period: int) -> None:
        self.period = period
        self.n = 0
        self.prev = NAN
        self.gain = 0.0
        self.loss = 0.0

    @staticmethod
    def _value(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    def update(self, close: float) -> float:
        self.n += 1
        if self.n == 1:
            self.prev = close
            return NAN
        change = close - self.prev
        self.prev = close
        g, lo = max(change, 0.0), max(-change, 0.0)
        steps = self.n - 1
        if steps < self.period:
            self.gain += g
            self.loss += lo
            return NAN
        if steps == self.period:
            self.gain = (self.gain + g) / self.period
            self.loss = (self.loss + lo) / self.period
        else:
            self.gain = (self.gain * (self.period - 1) + g) / self.period
            self.loss = (self.loss * (self.period - 1) + lo) / self.period
        return self._value(self.gain, self.loss)


class _Extreme:
    """Max or min of the ``period`` values BEFORE the current one: a monotonic ring, amortised O(1)."""

    sign = 1.0

    def __init__(self, period: int) -> None:
        self.period = period
        self.index = -1
        self.ring: deque[tuple[int, float]] = deque()

    def update(self, x: float) -> float:
        self.index += 1
        result = NAN
        if self.index >= self.period:
            while self.ring and self.ring[0][0] < self.index - self.period:
                self.ring.popleft()
            result = self.ring[0][1]
        while self.ring and self.sign * self.ring[-1][1] <= self.sign * x:
            self.ring.pop()
        self.ring.append((self.index, x))
        return result


class RollingMaxPrev(_Extreme):
    sign = 1.0


class RollingMinPrev(_Extreme):
    sign = -1.0


class PercentileRank:
    """Percent of the last ``lookback`` values (inclusive) at or below the current one."""

    def __init__(self, lookback: int) -> None:
        self.lookback = lookback
        self.ring: deque[float] = deque(maxlen=lookback)
        self.ordered: list[float] = []
        self.nans = 0

    def update(self, x: float) -> float:
        if len(self.ring) == self.lookback:
            old = self.ring[0]
            if old != old:
                self.nans -= 1
            else:
                del self.ordered[bisect.bisect_left(self.ordered, old)]
        self.ring.append(x)
        if x != x:
            self.nans += 1
        else:
            bisect.insort(self.ordered, x)
        if len(self.ring) < self.lookback or self.nans:
            return NAN
        return bisect.bisect_right(self.ordered, x) / self.lookback * 100.0


class RollingMedian:
    def __init__(self, window: int) -> None:
        self.window = window
        self.ring: deque[float] = deque(maxlen=window)
        self.ordered: list[float] = []

    def update(self, x: float) -> float:
        if len(self.ring) == self.window:
            del self.ordered[bisect.bisect_left(self.ordered, self.ring[0])]
        self.ring.append(x)
        bisect.insort(self.ordered, x)
        if len(self.ring) < self.window:
            return NAN
        middle = self.window // 2
        if self.window % 2:
            return self.ordered[middle]
        return (self.ordered[middle - 1] + self.ordered[middle]) / 2.0


class EfficiencyRatio:
    """Kaufman efficiency ratio, over the same running path sum as the batch version."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.closes: deque[float] = deque(maxlen=period + 1)
        self.sums: deque[float] = deque([0.0], maxlen=period + 1)
        self.path = 0.0
        self.n = 0

    def update(self, close: float) -> float:
        if self.n > 0:
            self.path += abs(close - self.closes[-1])
            self.sums.append(self.path)
        self.closes.append(close)
        self.n += 1
        if self.n <= self.period:
            return NAN
        path = self.path - self.sums[0]
        return abs(close - self.closes[0]) / path if path > 0 else 0.0


class WilderAdx:
    """Average Directional Index. First defined value at index 2 * period - 1. Takes (high, low, close)."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.prev: tuple[float, float, float] = (NAN, NAN, NAN)
        self.count = 0
        self.tr = 0.0
        self.plus = 0.0
        self.minus = 0.0
        self.dx_seen = 0
        self.dx_total = 0.0
        self.value = NAN

    def update(self, high: float, low: float, close: float) -> float:
        prev_high, prev_low, prev_close = self.prev
        self.prev = (high, low, close)
        if prev_close != prev_close:
            return NAN
        up, down = high - prev_high, prev_low - low
        plus_dm = up if up > down and up > 0 else 0.0
        minus_dm = down if down > up and down > 0 else 0.0
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        self.count += 1
        p = self.period
        if self.count <= p:
            self.tr += tr
            self.plus += plus_dm
            self.minus += minus_dm
            if self.count < p:
                return NAN
        else:
            self.tr = self.tr - self.tr / p + tr
            self.plus = self.plus - self.plus / p + plus_dm
            self.minus = self.minus - self.minus / p + minus_dm
        dx = 0.0
        if self.tr > 0:
            plus_di, minus_di = 100.0 * self.plus / self.tr, 100.0 * self.minus / self.tr
            denominator = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / denominator if denominator > 0 else 0.0
        self.dx_seen += 1
        if self.dx_seen < p:
            self.dx_total += dx
            return NAN
        if self.dx_seen == p:
            self.value = (self.dx_total + dx) / p
        else:
            self.value = (self.value * (p - 1) + dx) / p
        return self.value
