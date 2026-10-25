"""Short-TTL cache so an identical situation is not paid for twice. Bounded size (LRU)."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional


def normalise(value: Any, digits: int = 3) -> Any:
    """Round floats and sort keys so near-identical feature sets hash identically."""
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, digits)
    if isinstance(value, dict):
        return {str(k): normalise(v, digits) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalise(v, digits) for v in value]
    return value


def cache_key(*parts: Any) -> str:
    text = json.dumps(normalise(list(parts)), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TTLCache:
    def __init__(self, ttl_seconds: float, maxsize: int = 256, clock: Callable[[], float] = time.time) -> None:
        self.ttl = float(ttl_seconds)
        self.maxsize = maxsize
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        if self.ttl <= 0:
            return None
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            stored, value = item
            if self._clock() - stored > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)
