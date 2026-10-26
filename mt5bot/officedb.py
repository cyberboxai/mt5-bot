"""The team's office tables: the event stream the panel renders, decision dossiers, agent
state, learned weights and calibration, the advisor audit log and the panel's control-action
audit.

They live in the account's single database (``mt5bot.sqlite``, see ``db``). Retention here
only ever deletes from these tables (events, dossiers, advisor calls, metrics, weight
history); it never touches the trade journal. WAL mode lets the panel read while the bot
writes. Every statement is parameterised; growth is bounded by ``apply_retention``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .db import DB_NAME, ensure_schema

OFFICE_DB = DB_NAME  # the office tables share the account database
ADVISOR_RETENTION_DAYS = 90.0
METRICS_RETENTION_DAYS = 7.0
WEIGHT_HISTORY_RETENTION_DAYS = 180.0


DOSSIER_JSON_FIELDS = ("inputs", "opinions", "weights", "ensemble", "advisor", "risk", "execution", "outcome")
DOSSIER_COLUMNS = ("id", "ts", "symbol", "timeframe", "bar_time", "side", "mode", "strategy", "param_set_id", "regime",
                   "session", "status", "final_action", "team_multiplier", "advisor_multiplier", "final_multiplier",
                   "r_base", "resolved_ts", "explanation") + tuple(f"{f}_json" for f in DOSSIER_JSON_FIELDS)


def _default(v: Any) -> Any:
    if hasattr(v, "item") and callable(v.item):  # numpy scalars
        return v.item()
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    return str(v)


def to_json(value: Any) -> str:
    try:  # fast path: no NaN/inf anywhere
        return json.dumps(value, sort_keys=True, default=_default, allow_nan=False)
    except (ValueError, TypeError):
        pass

    def clean(v: Any) -> Any:
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        if isinstance(v, dict):
            return {str(k): clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple, set)):
            return [clean(x) for x in v]
        if hasattr(v, "item") and callable(v.item):  # numpy scalars
            return clean(v.item())
        return v

    return json.dumps(clean(value), sort_keys=True, default=str)


def _loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


class OfficeStore:
    """Thread-safe writer/reader. One connection guarded by a lock (writes are small)."""

    # Production: WAL. The test suite switches it off like Registry.durable (slow fsync on some disks).
    durable = True
    # Simulation only (separate process, synthetic data): WAL without an fsync per commit.
    fast_writes = False

    def __init__(self, path: Path, clock: Any = time.time, read_only: bool = False, shared: Any = None) -> None:
        """``shared``: a Registry on the same database. Its connection and lock are reused, so the
        trading loop and the event writer queue on one Python lock instead of contending for
        SQLite's file lock (busy waits) through two connections in one process."""
        self.path = Path(path)
        self._clock = clock
        self.read_only = read_only
        self._owns_conn = shared is None
        if shared is not None:
            self._lock = shared.lock
            self.conn = shared.raw
            return
        self._lock = threading.RLock()
        if read_only:
            # mode=rw never creates a missing database (the panel must not create files), and
            # query_only forbids writes; unlike mode=ro it also works on a WAL database whose
            # writer is not running.
            target, uri = self.path.resolve().as_uri() + "?mode=rw", True
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target, uri = str(self.path), False
        self.conn = sqlite3.connect(target, timeout=30, isolation_level=None, check_same_thread=False, uri=uri)
        self.conn.row_factory = sqlite3.Row
        if read_only:
            self.conn.execute("PRAGMA query_only=ON")
            return
        if self.durable:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=OFF" if self.fast_writes else "PRAGMA synchronous=NORMAL")
        else:
            self.conn.execute("PRAGMA synchronous=OFF")
        with self._lock:
            ensure_schema(self.conn)

    def close(self) -> None:
        if not self._owns_conn:
            return  # the Registry that owns the shared connection closes it
        with self._lock:
            self.conn.close()

    def now(self) -> float:
        return float(self._clock())

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:  # also when COMMIT itself failed
                    self.conn.execute("ROLLBACK")
                raise

    def _exec(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, args)

    def _all(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, args))

    def _one(self, sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------------- kv

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    # --------------------------------------------------------------- events

    def insert_events(self, rows: Iterable[Mapping[str, Any]]) -> list[int]:
        ids = []
        with self.tx():
            for r in rows:
                cur = self.conn.execute(
                    "INSERT INTO events (ts, topic, agent, symbol, severity, correlation_id, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (float(r["ts"]), str(r["topic"]), r.get("agent"), r.get("symbol"), r.get("severity", "info"),
                     r.get("correlation_id"), to_json(r.get("payload") or {})))
                ids.append(int(cur.lastrowid))
        return ids

    def events_after(self, after_id: int = 0, limit: int = 500, since_ts: Optional[float] = None,
                     until_ts: Optional[float] = None, agent: Optional[str] = None, symbol: Optional[str] = None,
                     severity: Optional[str] = None, topic_prefix: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE id > ?"
        args: list[Any] = [int(after_id)]
        for column, value in (("agent", agent), ("symbol", symbol), ("severity", severity)):
            if value:
                sql += f" AND {column} = ?"  # column names come from this fixed tuple, never from input
                args.append(value)
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(float(since_ts))
        if until_ts is not None:
            sql += " AND ts <= ?"
            args.append(float(until_ts))
        if topic_prefix:
            sql += " AND topic LIKE ? ESCAPE '\\'"
            args.append(topic_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql += " ORDER BY id LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        return [self._event(r) for r in self._all(sql, args)]

    def latest_events(self, limit: int = 100) -> list[dict]:
        rows = self._all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 5000)),))
        return [self._event(r) for r in reversed(rows)]

    def max_event_id(self) -> int:
        row = self._one("SELECT MAX(id) AS m FROM events")
        return int(row["m"] or 0) if row else 0

    @staticmethod
    def _event(r: sqlite3.Row) -> dict:
        return {"id": int(r["id"]), "ts": float(r["ts"]), "topic": r["topic"], "agent": r["agent"],
                "symbol": r["symbol"], "severity": r["severity"], "correlation_id": r["correlation_id"],
                "payload": _loads(r["payload_json"], {})}

    # -------------------------------------------------------------- dossiers

    def save_dossier(self, d: Mapping[str, Any]) -> None:
        values = []
        for column in DOSSIER_COLUMNS:
            if column.endswith("_json"):
                values.append(to_json(d.get(column[:-5], {} if column != "opinions_json" else [])))
            else:
                values.append(d.get(column))
        placeholders = ", ".join("?" for _ in DOSSIER_COLUMNS)
        self._exec(f"INSERT OR REPLACE INTO dossiers ({', '.join(DOSSIER_COLUMNS)}) VALUES ({placeholders})",
                   values)

    def update_dossier(self, dossier_id: str, **fields: Any) -> None:
        allowed = set(DOSSIER_COLUMNS) - {"id"}
        sets, args = [], []
        for key, value in fields.items():
            column = f"{key}_json" if key in DOSSIER_JSON_FIELDS else key
            if column not in allowed:
                raise KeyError(f"unknown dossier field {key!r}")
            sets.append(f"{column} = ?")  # validated against the fixed column list above
            args.append(to_json(value) if column.endswith("_json") else value)
        if not sets:
            return
        args.append(dossier_id)
        self._exec(f"UPDATE dossiers SET {', '.join(sets)} WHERE id = ?", args)

    @staticmethod
    def _dossier(r: sqlite3.Row) -> dict:
        out = {c: r[c] for c in DOSSIER_COLUMNS if not c.endswith("_json")}
        for f in DOSSIER_JSON_FIELDS:
            out[f] = _loads(r[f"{f}_json"], [] if f == "opinions" else {})
        return out

    def dossier(self, dossier_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM dossiers WHERE id = ?", (dossier_id,))
        return self._dossier(row) if row else None

    def dossiers(self, limit: int = 100, offset: int = 0, symbol: Optional[str] = None, status: Optional[str] = None,
                 since_ts: Optional[float] = None, action: Optional[str] = None, query: Optional[str] = None,
                 order: str = "desc") -> list[dict]:
        sql = "SELECT * FROM dossiers WHERE 1 = 1"
        args: list[Any] = []
        for column, value in (("symbol", symbol), ("status", status), ("final_action", action)):
            if value:
                sql += f" AND {column} = ?"  # fixed column names
                args.append(value)
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(float(since_ts))
        if query:
            sql += " AND (id LIKE ? OR strategy LIKE ? OR regime LIKE ? OR risk_json LIKE ?)"
            like = "%" + query.replace("%", "").replace("_", "")[:60] + "%"
            args.extend([like, like, like, like])
        sql += f" ORDER BY ts {'ASC' if order == 'asc' else 'DESC'} LIMIT ? OFFSET ?"
        args.extend([max(1, min(int(limit), 5000)), max(0, int(offset))])
        return [self._dossier(r) for r in self._all(sql, args)]

    def unresolved_dossiers(self, limit: int = 500) -> list[dict]:
        rows = self._all("SELECT * FROM dossiers WHERE status IN ('decided', 'executed', 'skipped') AND r_base IS NULL"
                         " AND side IS NOT NULL ORDER BY ts LIMIT ?", (limit,))
        return [self._dossier(r) for r in rows]

    def resolved_dossiers(self, since_ts: Optional[float] = None, limit: int = 100000) -> list[dict]:
        sql = "SELECT * FROM dossiers WHERE r_base IS NOT NULL"
        args: list[Any] = []
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(float(since_ts))
        sql += " ORDER BY ts LIMIT ?"
        args.append(limit)
        return [self._dossier(r) for r in self._all(sql, args)]

    # ---------------------------------------------------------- agent state

    def set_agent_state(self, agent: str, state: str, task: Optional[str] = None, last_message: Optional[str] = None,
                        benched: Optional[bool] = None, bench_reason: Optional[str] = None,
                        errors: Optional[int] = None, stats: Optional[Mapping[str, Any]] = None) -> None:
        row = self._one("SELECT * FROM agent_state WHERE agent = ?", (agent,))
        current = dict(row) if row else {"task": None, "last_message": None, "benched": 0, "bench_reason": None,
                                         "errors": 0, "stats_json": "{}"}
        self._exec(
            "INSERT OR REPLACE INTO agent_state (agent, state, task, last_message, updated_ts, benched, bench_reason,"
            " errors, stats_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent, state, task if task is not None else current["task"],
             last_message if last_message is not None else current["last_message"], self.now(),
             int(benched) if benched is not None else current["benched"],
             bench_reason if benched is not None else current["bench_reason"],
             errors if errors is not None else current["errors"],
             to_json(stats) if stats is not None else current["stats_json"]))

    def set_agent_states(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Upsert many agents' state in ONE transaction (one commit per housekeeping pass)."""
        now = self.now()
        with self.tx():
            for r in rows:
                self.conn.execute(
                    "INSERT OR REPLACE INTO agent_state (agent, state, task, last_message, updated_ts, benched,"
                    " bench_reason, errors, stats_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["agent"], r["state"], r.get("task"), r.get("last_message"), now, int(bool(r.get("benched"))),
                     r.get("bench_reason"), int(r.get("errors") or 0), to_json(r.get("stats") or {})))

    def agent_states(self) -> dict[str, dict]:
        out = {}
        for r in self._all("SELECT * FROM agent_state"):
            d = dict(r)
            d["stats"] = _loads(d.pop("stats_json"), {})
            d["benched"] = bool(d["benched"])
            out[r["agent"]] = d
        return out

    # ------------------------------------------------------ weights and calibration

    def load_weights(self) -> dict[tuple[str, str], tuple[float, int]]:
        return {(r["agent"], r["regime"]): (float(r["log_weight"]), int(r["n"]))
                for r in self._all("SELECT * FROM agent_weights")}

    def save_weights(self, weights: Mapping[tuple[str, str], tuple[float, int]], history: bool = True) -> None:
        now = self.now()
        with self.tx():
            for (agent, regime), (logw, n) in weights.items():
                self.conn.execute("INSERT OR REPLACE INTO agent_weights (agent, regime, log_weight, n, updated_ts)"
                                  " VALUES (?, ?, ?, ?, ?)", (agent, regime, float(logw), int(n), now))
                if history:
                    self.conn.execute("INSERT INTO weights_history (ts, agent, regime, weight) VALUES (?, ?, ?, ?)",
                                      (now, agent, regime, math.exp(float(logw))))

    def reset_weights(self) -> None:
        with self.tx():
            self.conn.execute("DELETE FROM agent_weights")
            self.conn.execute("DELETE FROM calibration")

    def weight_history(self, agent: Optional[str] = None, since_ts: Optional[float] = None,
                       limit: int = 5000) -> list[dict]:
        sql = "SELECT * FROM weights_history WHERE 1 = 1"
        args: list[Any] = []
        if agent:
            sql += " AND agent = ?"
            args.append(agent)
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(float(since_ts))
        sql += " ORDER BY ts LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._all(sql, args)]

    def load_calibration(self) -> dict[str, dict]:
        out = {}
        for r in self._all("SELECT * FROM calibration"):
            d = dict(r)
            d["reliability"] = _loads(d.pop("reliability_json"), [])
            out[r["agent"]] = d
        return out

    def save_calibration(self, agent: str, a: float, b: float, n: int, brier_raw: Optional[float],
                         brier_cal: Optional[float], reliability: Sequence[Any]) -> None:
        self._exec("INSERT OR REPLACE INTO calibration (agent, a, b, n, brier_raw, brier_cal, reliability_json,"
                   " updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (agent, float(a), float(b), int(n), brier_raw, brier_cal, to_json(list(reliability)), self.now()))

    # ---------------------------------------------------------------- advisor

    def log_advisor_call(self, row: Mapping[str, Any]) -> Optional[int]:
        cur = self._exec(
            "INSERT INTO advisor_calls (ts, purpose, model, prompt_hash, redacted_prompt, response, valid, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, latency_ms, outcome, error_kind, detail,"
            " state_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["ts"], row["purpose"], row["model"], row["prompt_hash"], row["redacted_prompt"], row["response"],
             int(row["valid"]), int(row["input_tokens"]), int(row["output_tokens"]), int(row["cache_read_tokens"]),
             int(row["cache_write_tokens"]), float(row["cost_usd"]), float(row["latency_ms"]), row["outcome"],
             row.get("error_kind"), row.get("detail"), row.get("state_after")))
        return int(cur.lastrowid)

    def log_advisor_state(self, ts: float, old: str, new: str, reason: str) -> None:
        self._exec("INSERT INTO advisor_states (ts, from_state, to_state, reason) VALUES (?, ?, ?, ?)",
                   (float(ts), old, new, reason[:500]))

    def advisor_usage_since(self, calls_since: float, cost_since: float) -> tuple[list[float], float]:
        calls = [float(r["ts"]) for r in self._all("SELECT ts FROM advisor_calls WHERE ts >= ?", (calls_since,))]
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM advisor_calls WHERE ts >= ?", (cost_since,))
        return calls, float(row["c"]) if row else 0.0

    def advisor_calls(self, limit: int = 100, since_ts: Optional[float] = None) -> list[dict]:
        sql = "SELECT * FROM advisor_calls"
        args: list[Any] = []
        if since_ts is not None:
            sql += " WHERE ts >= ?"
            args.append(float(since_ts))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        return [dict(r) for r in self._all(sql, args)]

    def advisor_states(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM advisor_states ORDER BY id DESC LIMIT ?", (limit,))]

    def set_tilts(self, tilts: Iterable[Any]) -> None:
        now = self.now()
        with self.tx():
            for t in tilts:
                self.conn.execute("INSERT OR REPLACE INTO advisor_tilts (agent, factor, expires_ts, created_ts)"
                                  " VALUES (?, ?, ?, ?)", (t.agent, float(t.factor), float(t.expires_ts), now))

    def active_tilts(self, now: Optional[float] = None) -> dict[str, float]:
        now = self.now() if now is None else now
        return {r["agent"]: float(r["factor"])
                for r in self._all("SELECT * FROM advisor_tilts WHERE expires_ts > ?", (now,))}

    def add_briefing(self, kind: str, payload: Mapping[str, Any]) -> int:
        cur = self._exec("INSERT INTO briefings (ts, kind, payload_json) VALUES (?, ?, ?)",
                         (self.now(), kind, to_json(payload)))
        return int(cur.lastrowid)

    def briefings(self, kind: Optional[str] = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM briefings"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": _loads(r["payload_json"], {})}
                for r in self._all(sql, args)]

    # ------------------------------------------------------------- hypotheses

    def add_hypothesis(self, source: str, strategy: str, param: str, value: Any, rationale: str) -> int:
        cur = self._exec("INSERT INTO hypotheses (ts, source, strategy, param, value_json, rationale, status)"
                         " VALUES (?, ?, ?, ?, ?, ?, 'queued')",
                         (self.now(), source, strategy, param, to_json(value), rationale[:400]))
        return int(cur.lastrowid)

    def hypotheses(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM hypotheses"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._all(sql, args):
            d = dict(r)
            d["value"] = _loads(d.pop("value_json"), None)
            out.append(d)
        return out

    def mark_hypothesis(self, hypothesis_id: int, status: str, note: str = "") -> None:
        self._exec("UPDATE hypotheses SET status = ?, note = ? WHERE id = ?", (status, note[:400], hypothesis_id))

    # ------------------------------------------------------------ control audit

    def add_control_audit(self, action: str, client: str, ok: bool, detail: str) -> int:
        cur = self._exec("INSERT INTO control_audit (ts, action, client, ok, detail) VALUES (?, ?, ?, ?, ?)",
                         (self.now(), action[:40], client[:80], int(ok), detail[:400]))
        return int(cur.lastrowid)

    def control_audit(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM control_audit ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------- ablation / metrics

    def add_ablation_report(self, n: int, payload: Mapping[str, Any]) -> int:
        cur = self._exec("INSERT INTO ablation_reports (ts, n, payload_json) VALUES (?, ?, ?)",
                         (self.now(), int(n), to_json(payload)))
        return int(cur.lastrowid)

    def latest_ablation(self) -> Optional[dict]:
        row = self._one("SELECT * FROM ablation_reports ORDER BY id DESC LIMIT 1")
        if row is None:
            return None
        return {"id": row["id"], "ts": row["ts"], "n": row["n"], "payload": _loads(row["payload_json"], {})}

    def add_metrics(self, values: Mapping[str, float]) -> None:
        now = self.now()
        with self.tx():
            for name, value in values.items():
                if isinstance(value, (int, float)) and math.isfinite(value):
                    self.conn.execute("INSERT INTO metrics (ts, name, value) VALUES (?, ?, ?)",
                                      (now, name, float(value)))

    def metrics(self, name: str, since_ts: Optional[float] = None, limit: int = 2000) -> list[tuple[float, float]]:
        sql = "SELECT ts, value FROM metrics WHERE name = ?"
        args: list[Any] = [name]
        if since_ts is not None:
            sql += " AND ts >= ?"
            args.append(float(since_ts))
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [(float(r["ts"]), float(r["value"])) for r in reversed(self._all(sql, args))]

    # -------------------------------------------------------------- retention

    def apply_retention(self, events_days: float, max_event_rows: int, dossier_days: float,
                        vacuum: bool = False) -> dict[str, int]:
        """Bounded growth: delete by age, cap the event table, optionally reclaim space."""
        now = self.now()
        deleted: dict[str, int] = {}
        with self.tx():
            cur = self.conn.execute("DELETE FROM events WHERE ts < ?", (now - events_days * 86400,))
            deleted["events_age"] = cur.rowcount
            row = self.conn.execute("SELECT MAX(id) AS m, COUNT(*) AS c FROM events").fetchone()
            if row and row["c"] and row["c"] > max_event_rows:
                cur = self.conn.execute("DELETE FROM events WHERE id <= ?", (int(row["m"]) - max_event_rows,))
                deleted["events_cap"] = cur.rowcount
            deleted["dossiers"] = self.conn.execute(
                "DELETE FROM dossiers WHERE ts < ?", (now - dossier_days * 86400,)).rowcount
            deleted["advisor_calls"] = self.conn.execute(
                "DELETE FROM advisor_calls WHERE ts < ?", (now - ADVISOR_RETENTION_DAYS * 86400,)).rowcount
            deleted["metrics"] = self.conn.execute(
                "DELETE FROM metrics WHERE ts < ?", (now - METRICS_RETENTION_DAYS * 86400,)).rowcount
            deleted["weights_history"] = self.conn.execute(
                "DELETE FROM weights_history WHERE ts < ?", (now - WEIGHT_HISTORY_RETENTION_DAYS * 86400,)).rowcount
            self.conn.execute("DELETE FROM advisor_tilts WHERE expires_ts < ?", (now - 86400,))
        if vacuum:
            with self._lock:
                if self.durable:
                    self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.execute("VACUUM")
        return deleted


def office_path(state_dir: Path) -> Path:
    return Path(state_dir) / OFFICE_DB
