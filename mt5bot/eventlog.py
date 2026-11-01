"""Append-only event log: the source of truth for decisions, orders, tiers and adaptations.

The ``ledger`` table rejects UPDATE and DELETE with database triggers, so history cannot be
rewritten even by a bug. ``orders`` and the other read models are projections: ``project_orders``
rebuilds the order table from the log, and tests assert it matches the live projection.

Order events are committed with ``PRAGMA synchronous=FULL`` before the order leaves the
process (write-ahead): after a power cut the log still shows an order that may have been sent.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

log = logging.getLogger("mt5_bot")


@dataclass(frozen=True)
class LedgerEvent:
    seq: int
    ts: float
    kind: str
    stream: str
    correlation_id: Optional[str]
    stamp: dict[str, Any]
    payload: dict[str, Any]


def canonical(value: Any) -> str:
    """Stable JSON: the bytes of a replayed decision log depend only on its content."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_safe(v) for v in items]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return _safe(value.item())
        except (TypeError, ValueError):
            return str(value)
    return value


class EventLog:
    """Writes through the registry's connection, so an event commits with its projection."""

    def __init__(self, registry: Any, clock: Callable[[], float] = time.time,
                 stamp: Optional[Callable[[], Mapping[str, Any]]] = None) -> None:
        self.registry = registry
        self.conn = registry.conn
        self.clock = clock
        self._stamp = stamp or (lambda: {})

    def append(self, kind: str, stream: str, payload: Optional[Mapping[str, Any]] = None,
               correlation_id: Optional[str] = None, stamp: Optional[Mapping[str, Any]] = None,
               durable: bool = False) -> int:
        """Append one event. Joins the caller's transaction when one is open."""
        row = (float(self.clock()), kind, stream, correlation_id, canonical(_safe(dict(stamp or self._stamp()))),
               canonical(_safe(dict(payload or {}))))
        sql = ("INSERT INTO ledger (ts, kind, stream, correlation_id, stamp_json, payload_json) "
               "VALUES (?, ?, ?, ?, ?, ?)")
        if durable and not self.registry.raw.in_transaction:
            with self.registry.durable_tx():
                return int(self.conn.execute(sql, row).lastrowid)
        return int(self.conn.execute(sql, row).lastrowid)

    @staticmethod
    def _event(row: Any) -> LedgerEvent:
        return LedgerEvent(int(row["seq"]), float(row["ts"]), row["kind"], row["stream"], row["correlation_id"],
                           json.loads(row["stamp_json"]), json.loads(row["payload_json"]))

    def stream(self, stream: str) -> list[LedgerEvent]:
        rows = self.conn.execute("SELECT * FROM ledger WHERE stream = ? ORDER BY seq", (stream,))
        return [self._event(r) for r in rows]

    def tail(self, kinds: Optional[Sequence[str]] = None, after: int = 0, limit: int = 1000) -> list[LedgerEvent]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            rows = self.conn.execute(f"SELECT * FROM ledger WHERE seq > ? AND kind IN ({marks}) ORDER BY seq LIMIT ?",
                                     (after, *kinds, limit))
        else:
            rows = self.conn.execute("SELECT * FROM ledger WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
        return [self._event(r) for r in rows]


def decision_log(conn: Any, kinds: Optional[Iterable[str]] = None) -> list[str]:
    """Canonical lines of the log, without sequence numbers: the golden replay compares these."""
    rows = conn.execute("SELECT ts, kind, stream, correlation_id, stamp_json, payload_json FROM ledger ORDER BY seq")
    wanted = set(kinds) if kinds else None
    out = []
    for r in rows:
        if wanted is None or r["kind"] in wanted:
            out.append(canonical([round(float(r["ts"]), 3), r["kind"], r["stream"], r["correlation_id"],
                                  json.loads(r["stamp_json"]), json.loads(r["payload_json"])]))
    return out
