"""Clocks. ``SystemClock`` for live trading, ``VirtualClock`` for replay, simulation and tests."""

from __future__ import annotations

import time


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class VirtualClock:
    """Callable like a plain time function, so it can be injected wherever one is expected."""

    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
