"""Data platform: captured ticks and bars, quality checks and immutable snapshots.

Files live in the account folder, partitioned so a day of ticks is one small file:

    data/ticks/<symbol>/<YYYY-MM-DD>.npy     appended in batches by the trading loop
    data/snapshots/<id>.npy                  immutable; ``id`` is a hash of the content

Every research result names the snapshot it ran on, so it can be reproduced exactly. Numpy files
are the default on purpose: no dependency, and memory-mapped reads for research.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

log = logging.getLogger("mt5_bot")

TICK_DTYPE = np.dtype([("time", "<i8"), ("bid", "<f8"), ("ask", "<f8")])
SPIKE_SIGMAS = 12.0
FLUSH_TICKS = 500


@dataclass
class QualityReport:
    rows: int = 0
    duplicate_times: int = 0
    non_monotonic: int = 0
    gaps: int = 0
    max_gap_bars: float = 0.0
    spikes: int = 0
    stale_runs: int = 0
    weekend_bars: int = 0
    bad_ohlc: int = 0
    tz_suspect: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.non_monotonic or self.duplicate_times or self.bad_ohlc or self.tz_suspect
                    or self.spikes > max(3, self.rows // 2000))


def check_bars(bars: np.ndarray, bar_seconds: int) -> QualityReport:
    """Gaps, spikes, stale feeds, duplicate timestamps and timezone consistency."""
    r = QualityReport(rows=len(bars))
    if len(bars) < 2:
        r.issues.append("too few bars")
        return r
    times = np.asarray(bars["time"], dtype=np.int64)
    diffs = np.diff(times)
    r.duplicate_times = int(np.sum(diffs == 0))
    r.non_monotonic = int(np.sum(diffs < 0))
    days = times // 86400
    weekdays = (days + 3) % 7
    weekday_gap = (diffs > bar_seconds) & (weekdays[1:] == weekdays[:-1])   # same day: not a weekend close
    r.gaps = int(np.sum(weekday_gap))
    r.max_gap_bars = float(diffs[weekday_gap].max() / bar_seconds) if r.gaps else 0.0
    closes = np.asarray(bars["close"], dtype=float)
    opens, highs, lows = (np.asarray(bars[k], dtype=float) for k in ("open", "high", "low"))
    r.bad_ohlc = int(np.sum((highs < np.maximum(opens, closes) - 1e-9) | (lows > np.minimum(opens, closes) + 1e-9)))
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(np.log(np.where(closes > 0, closes, np.nan)))
    finite = returns[np.isfinite(returns)]
    if finite.size > 20:
        scale = 1.4826 * float(np.median(np.abs(finite - np.median(finite)))) or float(finite.std()) or 1e-12
        r.spikes = int(np.sum(np.abs(finite) > SPIKE_SIGMAS * scale))
    unchanged = (highs == lows) & (highs == closes)
    runs = np.diff(np.flatnonzero(np.r_[True, ~unchanged, True])) - 1
    r.stale_runs = int(np.sum(runs >= 6))
    r.weekend_bars = int(np.sum(weekdays >= 5))
    # A broker's server week runs Monday to Friday. Many Saturday bars mean a timezone shift upstream.
    r.tz_suspect = r.weekend_bars > 0.02 * len(bars)
    for name in ("duplicate_times", "non_monotonic", "gaps", "spikes", "stale_runs", "bad_ohlc"):
        value = getattr(r, name)
        if value:
            r.issues.append(f"{name}={value}")
    if r.tz_suspect:
        r.issues.append(f"{r.weekend_bars} weekend bars: check the server timezone")
    return r


class DataStore:
    def __init__(self, root: Path, registry: Any = None, clock: Callable[[], float] = lambda: 0.0) -> None:
        self.root = Path(root)
        self.registry = registry
        self.clock = clock
        self._ticks: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        self._last: dict[str, tuple[int, float, float]] = {}

    # --------------------------------------------------------------------- ticks

    def capture_tick(self, symbol: str, time_s: int, bid: float, ask: float) -> None:
        row = (int(time_s), float(bid), float(ask))
        if self._last.get(symbol) == row:
            return
        self._last[symbol] = row
        self._ticks[symbol].append(row)
        if len(self._ticks[symbol]) >= FLUSH_TICKS:
            self.flush()

    def flush(self) -> int:
        """Batched writes: one append per symbol and day instead of one per tick."""
        written = 0
        for symbol, rows in list(self._ticks.items()):
            if not rows:
                continue
            arr = np.array(rows, dtype=TICK_DTYPE)
            days = arr["time"] // 86400
            for day in np.unique(days):
                part = arr[days == day]
                name = datetime.fromtimestamp(int(day) * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
                path = self.root / "data" / "ticks" / symbol / f"{name}.npy"
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    part = np.concatenate([np.load(path), part])
                tmp = path.with_suffix(".tmp.npy")
                np.save(tmp, part)
                os.replace(tmp, path)
            written += len(rows)
            self._ticks[symbol] = []
        return written

    def load_ticks(self, symbol: str) -> np.ndarray:
        files = sorted((self.root / "data" / "ticks" / symbol).glob("*.npy"))
        return np.concatenate([np.load(f) for f in files]) if files else np.zeros(0, dtype=TICK_DTYPE)

    def prune_ticks(self, keep_days: int, now_ts: float) -> int:
        """Retention: raw ticks are heavy, and bars and TCA keep what research needs."""
        cutoff = datetime.fromtimestamp(now_ts - keep_days * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        removed = 0
        for path in (self.root / "data" / "ticks").glob("*/*.npy"):
            if path.stem < cutoff:
                path.unlink()
                removed += 1
        return removed

    # ----------------------------------------------------------------- snapshots

    def snapshot(self, symbol: str, timeframe: str, bars: np.ndarray, bar_seconds: int) -> tuple[str, QualityReport]:
        """Store ``bars`` immutably. The same content always gets the same id."""
        arr = np.asarray(bars)
        digest = hashlib.sha256(arr.tobytes()).hexdigest()
        sid = f"{symbol}-{timeframe}-{digest[:16]}"
        path = self.root / "data" / "snapshots" / f"{sid}.npy"
        report = check_bars(arr, bar_seconds)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.npy")
            np.save(tmp, arr)
            os.replace(tmp, path)
            path.chmod(0o444)
        if self.registry is not None:
            self.registry.conn.execute(
                "INSERT OR IGNORE INTO data_snapshots (id, ts, symbol, timeframe, first_time, last_time, rows,"
                " content_hash, quality_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, float(self.clock()), symbol, timeframe, int(arr["time"][0]) if len(arr) else None,
                 int(arr["time"][-1]) if len(arr) else None, len(arr), digest, json.dumps(asdict(report), default=str)))
        return sid, report

    def load_snapshot(self, sid: str) -> Optional[np.ndarray]:
        path = self.root / "data" / "snapshots" / f"{sid}.npy"
        return np.load(path, mmap_mode="r") if path.exists() else None
