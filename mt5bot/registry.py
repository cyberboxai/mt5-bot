"""Registry: strategies, parameter sets, research runs, the audit log, trades (live and
shadow), equity snapshots, the market data cache and key/value state.

It lives in the account's single database (``state/<server>_<login>/mt5bot.sqlite``, see
``db``), next to the team's office tables. WAL mode lets the research process and the
panel read while the live process writes. Every statement is parameterised.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import numpy as np

from . import strategies as strategy_lib
from .db import ensure_schema
from .strategies.base import canonical_params


RATES_DTYPE = np.dtype([
    ("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
    ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8"),
])


def _json(value: Any) -> str:
    def clean(v: Any) -> Any:
        if isinstance(v, float) and not math.isfinite(v):
            return None if math.isnan(v) else (1e308 if v > 0 else -1e308)
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        if isinstance(v, np.generic):
            return clean(v.item())
        return v

    return json.dumps(clean(value), sort_keys=True)


@dataclass(frozen=True)
class Candidate:
    """A strategy + parameter set backed by a validating optimisation result."""

    candidate_id: Optional[int]
    param_set_id: Optional[int]
    strategy: str
    params: dict
    metrics: dict
    score: float
    passed: bool

    @property
    def is_notrade(self) -> bool:
        return self.strategy == strategy_lib.NO_TRADE

    def describe(self) -> str:
        if self.is_notrade:
            return "NoTrade"
        return strategy_lib.describe(strategy_lib.get(self.strategy), self.params)


NOTRADE_CANDIDATE = Candidate(None, None, strategy_lib.NO_TRADE, {}, {}, 0.0, True)


@dataclass(frozen=True)
class TradeRow:
    id: int
    mode: str
    symbol: str
    param_set_id: Optional[int]
    side: str
    status: str
    open_ts: float
    close_ts: Optional[float]
    entry: float
    exit: Optional[float]
    sl: float
    tp: float
    volume: float
    risk_money: float
    pnl: Optional[float]
    r: Optional[float]
    ticket: Optional[int]
    exit_reason: Optional[str]
    open_bar_time: Optional[int]
    close_bar_time: Optional[int]
    extra: dict
    arm_id: Optional[int] = None
    timeframe: Optional[str] = None
    tier: Optional[str] = None
    risk_pct: Optional[float] = None


class _Rows:
    """A fully fetched result, so the connection lock is never held while a caller iterates."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._rows = cursor.fetchall() if cursor.description is not None else []
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self._next = 0

    def fetchone(self) -> Any:
        if self._next >= len(self._rows):
            return None
        row = self._rows[self._next]
        self._next += 1
        return row

    def fetchall(self) -> list:
        rows, self._next = self._rows[self._next:], len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class SerializedConnection:
    """One sqlite3 connection shared by several threads (the trading loop and the team's
    supervised agents): every statement runs under a re-entrant lock."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, args: Sequence[Any] = ()) -> _Rows:
        with self._lock:
            return _Rows(self._conn.execute(sql, args))

    def executemany(self, sql: str, rows: Any) -> _Rows:
        with self._lock:
            return _Rows(self._conn.executemany(sql, rows))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Registry:
    # Production: WAL journal. The test suite sets this to False because switching a
    # database to WAL costs a slow fsync on some disks; a dedicated test covers WAL.
    durable = True
    # Simulation only (a separate process with synthetic data): WAL without fsync per commit.
    fast_writes = False

    def __init__(self, path: Path, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        raw = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self.raw = raw  # shared with the office tables' store in the same process (see OfficeStore)
        self.lock = self._lock
        self.conn = SerializedConnection(raw, self._lock)
        if self.durable:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL: a power cut can lose the last commit but can never corrupt the database.
            self.conn.execute("PRAGMA synchronous=OFF" if self.fast_writes else "PRAGMA synchronous=NORMAL")
        else:
            self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            ensure_schema(self.conn)
        with self.tx():
            for name, strat in strategy_lib.library().items():
                self.conn.execute("INSERT OR REPLACE INTO strategies (name, description) VALUES (?, ?)",
                                  (name, strat.description))
            self.conn.execute("INSERT OR REPLACE INTO strategies (name, description) VALUES (?, ?)",
                              (strategy_lib.NO_TRADE, strategy_lib.NOTRADE.description))

    def close(self) -> None:
        self.conn.close()

    def now(self) -> float:
        return float(self._clock())

    @contextmanager
    def tx(self) -> Iterator[SerializedConnection]:
        with self._lock:  # the whole transaction is one critical section
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                # Also when COMMIT itself fails (e.g. SQLITE_BUSY): the shared connection must
                # never be left inside a transaction, or every later BEGIN would fail.
                if self.raw.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    # ---- kv

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    # ---- parameter sets

    def param_set_id(self, strategy: str, params: dict) -> int:
        canon = json.dumps(canonical_params(params), sort_keys=True)
        self.conn.execute("INSERT OR IGNORE INTO param_sets (strategy, params_json) VALUES (?, ?)", (strategy, canon))
        row = self.conn.execute("SELECT id FROM param_sets WHERE strategy = ? AND params_json = ?",
                                (strategy, canon)).fetchone()
        return int(row["id"])

    def param_set(self, ps_id: int) -> tuple[str, dict]:
        row = self.conn.execute("SELECT strategy, params_json FROM param_sets WHERE id = ?", (ps_id,)).fetchone()
        if row is None:
            raise KeyError(f"param set {ps_id} not found")
        return row["strategy"], json.loads(row["params_json"])

    # ---- optimisation runs

    def start_run(self, symbol: str, trigger: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO optimization_runs (symbol, trigger, started_at, status) VALUES (?, ?, ?, 'running')",
            (symbol, trigger, self.now()))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, result: Any) -> list[int]:
        """Store every candidate of an OptimizationResult. Returns candidate ids in rank order."""
        ids = []
        with self.tx():
            for rank, cand in enumerate(result.candidates):
                ps = self.param_set_id(cand.strategy, cand.params)
                cur = self.conn.execute(
                    "INSERT INTO candidate_metrics (run_id, symbol, param_set_id, passed, reasons_json, metrics_json,"
                    " score, rank) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, result.symbol, ps, int(cand.passed), _json(cand.reasons), _json(cand.metrics),
                     float(cand.score) if math.isfinite(cand.score) else -1e308, rank))
                ids.append(int(cur.lastrowid))
            self.conn.execute(
                "UPDATE optimization_runs SET finished_at = ?, status = 'done', trials = ?, data_from = ?, data_to = ?,"
                " notes = ? WHERE id = ?",
                (self.now(), result.trials, result.data_from, result.data_to,
                 f"{len(result.survivors)} survivor(s) in {result.seconds:.1f}s", run_id))
        return ids

    def fail_run(self, run_id: int, message: str) -> None:
        self.conn.execute("UPDATE optimization_runs SET finished_at = ?, status = 'failed', notes = ? WHERE id = ?",
                          (self.now(), message[:2000], run_id))

    def latest_run(self, symbol: str, status: str = "done") -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM optimization_runs WHERE symbol = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (symbol, status)).fetchone()

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM optimization_runs ORDER BY id DESC LIMIT ?", (limit,)))

    def _candidate_from_row(self, row: sqlite3.Row) -> Candidate:
        strategy, params = self.param_set(int(row["param_set_id"]))
        return Candidate(int(row["id"]), int(row["param_set_id"]), strategy, params,
                         json.loads(row["metrics_json"]), float(row["score"]), bool(row["passed"]))

    def candidate(self, candidate_id: int) -> Candidate:
        row = self.conn.execute("SELECT * FROM candidate_metrics WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(f"candidate {candidate_id} not found")
        return self._candidate_from_row(row)

    def run_candidates(self, run_id: int) -> list[Candidate]:
        rows = self.conn.execute("SELECT * FROM candidate_metrics WHERE run_id = ? ORDER BY rank", (run_id,))
        return [self._candidate_from_row(r) for r in rows]

    def run_reasons(self, run_id: int) -> list[tuple[str, bool, float, list[str]]]:
        rows = self.conn.execute(
            "SELECT c.*, p.strategy FROM candidate_metrics c JOIN param_sets p ON p.id = c.param_set_id "
            "WHERE run_id = ? ORDER BY rank", (run_id,))
        return [(r["strategy"], bool(r["passed"]), float(r["score"]), json.loads(r["reasons_json"])) for r in rows]

    # ---- champions and challengers

    def champion(self, symbol: str) -> Candidate:
        row = self.conn.execute("SELECT * FROM champions WHERE symbol = ?", (symbol,)).fetchone()
        if row is None or row["param_set_id"] is None:
            return NOTRADE_CANDIDATE
        if row["candidate_id"] is not None:
            return self.candidate(int(row["candidate_id"]))
        strategy, params = self.param_set(int(row["param_set_id"]))
        return Candidate(None, int(row["param_set_id"]), strategy, params, {}, 0.0, False)

    def champion_since(self, symbol: str) -> Optional[float]:
        row = self.conn.execute("SELECT since FROM champions WHERE symbol = ?", (symbol,)).fetchone()
        return float(row["since"]) if row else None

    def set_challengers(self, symbol: str, candidates: Sequence[Candidate]) -> None:
        with self.tx():
            existing = {int(r["param_set_id"]): float(r["added"]) for r in self.conn.execute(
                "SELECT param_set_id, added FROM challengers WHERE symbol = ?", (symbol,))}
            self.conn.execute("DELETE FROM challengers WHERE symbol = ?", (symbol,))
            for cand in candidates:
                self.conn.execute(
                    "INSERT INTO challengers (symbol, param_set_id, candidate_id, added) VALUES (?, ?, ?, ?)",
                    (symbol, cand.param_set_id, cand.candidate_id, existing.get(cand.param_set_id, self.now())))

    def challengers(self, symbol: str) -> list[tuple[Candidate, float]]:
        rows = self.conn.execute(
            "SELECT candidate_id, added FROM challengers WHERE symbol = ? ORDER BY added, param_set_id", (symbol,))
        return [(self.candidate(int(r["candidate_id"])), float(r["added"])) for r in rows]

    def record_switch(self, symbol: str, action: str, old: Candidate, new: Candidate, reason: str,
                      metrics: dict) -> int:
        """Change the champion and write the audit row in one transaction."""
        now = self.now()
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO promotions (ts, symbol, action, old_param_set_id, new_param_set_id, new_candidate_id,"
                " old_desc, new_desc, reason, metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, symbol, action, old.param_set_id, new.param_set_id, new.candidate_id, old.describe(),
                 new.describe(), reason, _json(metrics)))
            promo_id = int(cur.lastrowid)
            self.conn.execute(
                "INSERT OR REPLACE INTO champions (symbol, param_set_id, candidate_id, since, promotion_id)"
                " VALUES (?, ?, ?, ?, ?)", (symbol, new.param_set_id, new.candidate_id, now, promo_id))
        return promo_id

    def last_switch_ts(self, symbol: str) -> Optional[float]:
        row = self.conn.execute(
            "SELECT ts FROM promotions WHERE symbol = ? AND action IN ('promote', 'demote', 'rollback') "
            "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        return float(row["ts"]) if row else None

    def promotions(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM promotions ORDER BY id DESC LIMIT ?", (limit,)))

    def promotion(self, promo_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM promotions WHERE id = ?", (promo_id,)).fetchone()

    def log_event(self, symbol: str, action: str, reason: str, metrics: Optional[dict] = None,
                  desc: str = "") -> int:
        """Audit row that does not change the champion (e.g. optimisation finished, reopt trigger)."""
        champ = self.champion(symbol)
        cur = self.conn.execute(
            "INSERT INTO promotions (ts, symbol, action, old_param_set_id, new_param_set_id, new_candidate_id,"
            " old_desc, new_desc, reason, metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.now(), symbol, action, champ.param_set_id, champ.param_set_id, champ.candidate_id,
             champ.describe(), desc or champ.describe(), reason, _json(metrics or {})))
        return int(cur.lastrowid)

    # ---- trades

    def open_trade(self, mode: str, symbol: str, param_set_id: Optional[int], side: str, entry: float, sl: float,
                   tp: float, volume: float, risk_money: float, ticket: Optional[int] = None,
                   open_bar_time: Optional[int] = None, extra: Optional[dict] = None,
                   open_ts: Optional[float] = None, arm_id: Optional[int] = None, timeframe: Optional[str] = None,
                   tier: Optional[str] = None, risk_pct: Optional[float] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO trades (mode, symbol, param_set_id, side, status, open_ts, open_bar_time, entry, sl, tp, volume,"
            " risk_money, ticket, extra_json, arm_id, timeframe, tier, risk_pct)"
            " VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, symbol, param_set_id, side, self.now() if open_ts is None else open_ts, open_bar_time, entry, sl,
             tp, volume, risk_money, ticket, _json(extra or {}), arm_id, timeframe, tier, risk_pct))
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, r: float, reason: str,
                    close_bar_time: Optional[int] = None, close_ts: Optional[float] = None) -> None:
        self.conn.execute(
            "UPDATE trades SET status = 'closed', close_ts = ?, close_bar_time = ?, exit = ?, pnl = ?, r = ?,"
            " exit_reason = ? WHERE id = ?",
            (self.now() if close_ts is None else close_ts, close_bar_time, exit_price, pnl, r, reason, trade_id))

    def update_trade_sl(self, trade_id: int, sl: float, tp: float) -> None:
        self.conn.execute("UPDATE trades SET sl = ?, tp = ? WHERE id = ?", (sl, tp, trade_id))

    @staticmethod
    def _trade(row: sqlite3.Row) -> TradeRow:
        keys = row.keys()
        return TradeRow(int(row["id"]), row["mode"], row["symbol"], row["param_set_id"], row["side"], row["status"],
                        float(row["open_ts"]), row["close_ts"], float(row["entry"]), row["exit"], float(row["sl"]),
                        float(row["tp"]), float(row["volume"]), float(row["risk_money"]), row["pnl"], row["r"],
                        row["ticket"], row["exit_reason"], row["open_bar_time"], row["close_bar_time"],
                        json.loads(row["extra_json"] or "{}"), row["arm_id"] if "arm_id" in keys else None,
                        row["timeframe"] if "timeframe" in keys else None, row["tier"] if "tier" in keys else None,
                        row["risk_pct"] if "risk_pct" in keys else None)

    def trades(self, mode: Optional[str] = None, param_set_id: Optional[int] = None, status: Optional[str] = None,
               symbol: Optional[str] = None, since: Optional[float] = None,
               limit: Optional[int] = None, arm_id: Optional[int] = None, tier: Optional[str] = None,
               closed_since: Optional[float] = None) -> list[TradeRow]:
        """Matching trades in id order; ``limit`` keeps only the most recent ones."""
        sql = "SELECT * FROM trades WHERE 1 = 1"
        args: list[Any] = []
        if closed_since is not None:
            sql += " AND (status = 'open' OR close_ts >= ?)"
            args.append(closed_since)
        for column, value in (("mode", mode), ("param_set_id", param_set_id), ("status", status), ("symbol", symbol),
                              ("arm_id", arm_id), ("tier", tier)):
            if value is not None:
                sql += f" AND {column} = ?"  # column names come from this fixed tuple, never from input
                args.append(value)
        if since is not None:
            sql += " AND open_ts >= ?"
            args.append(since)
        if limit is not None:
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit))
            return [self._trade(r) for r in reversed(list(self.conn.execute(sql, args)))]
        sql += " ORDER BY id"
        return [self._trade(r) for r in self.conn.execute(sql, args)]

    # ---- management policies (paired replays of shadow trades, see mt5bot.management)

    def record_policy_outcomes(self, arm_id: int, trade_id: int, outcomes: dict[str, float]) -> None:
        ts = self.now()
        for policy, r in outcomes.items():
            self.conn.execute("INSERT OR IGNORE INTO policy_outcomes (arm_id, trade_id, policy, r, ts)"
                              " VALUES (?, ?, ?, ?, ?)", (int(arm_id), int(trade_id), policy, float(r), ts))

    def policy_outcomes(self, after_id: int = 0) -> list[tuple[int, int, int, str, float]]:
        """(row id, arm_id, trade_id, policy, r) rows with id > ``after_id``, in id order."""
        return [(int(r["id"]), int(r["arm_id"]), int(r["trade_id"]), r["policy"], float(r["r"]))
                for r in self.conn.execute("SELECT id, arm_id, trade_id, policy, r FROM policy_outcomes WHERE id > ?"
                                           " ORDER BY id", (int(after_id),))]

    def trade_by_ticket(self, ticket: int) -> Optional[TradeRow]:
        row = self.conn.execute("SELECT * FROM trades WHERE ticket = ? AND mode = 'live' ORDER BY id DESC LIMIT 1",
                                (ticket,)).fetchone()
        return self._trade(row) if row else None

    def first_trade_ts(self, param_set_id: int, mode: str) -> Optional[float]:
        row = self.conn.execute("SELECT MIN(open_ts) AS t FROM trades WHERE param_set_id = ? AND mode = ?",
                                (param_set_id, mode)).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    # ---- equity

    def snapshot_equity(self, equity: float, balance: float, peak: Optional[float], open_positions: int) -> None:
        self.conn.execute("INSERT INTO equity_snapshots (ts, equity, balance, peak, open_positions) VALUES (?, ?, ?, ?, ?)",
                          (self.now(), equity, balance, peak, open_positions))

    def equity_points(self, since: float) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM equity_snapshots WHERE ts >= ? ORDER BY ts", (since,)))

    # ---- market data cache

    def store_bars(self, symbol: str, timeframe: str, rates: np.ndarray) -> None:
        rows = [(symbol, timeframe, int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]),
                 float(r["close"]), int(r["tick_volume"]), int(r["spread"]), int(r["real_volume"])) for r in rates]
        with self.tx():
            self.conn.executemany(
                "INSERT OR REPLACE INTO bars_cache (symbol, timeframe, time, open, high, low, close, tick_volume,"
                " spread, real_volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def load_bars(self, symbol: str, timeframe: str, since: Optional[int] = None) -> np.ndarray:
        sql = ("SELECT time, open, high, low, close, tick_volume, spread, real_volume FROM bars_cache"
               " WHERE symbol = ? AND timeframe = ?")
        args: list[Any] = [symbol, timeframe]
        if since is not None:
            sql += " AND time >= ?"
            args.append(since)
        rows = self.conn.execute(sql + " ORDER BY time", args).fetchall()
        out = np.zeros(len(rows), dtype=RATES_DTYPE)
        for i, r in enumerate(rows):
            out[i] = tuple(r)
        return out

    def store_spec(self, symbol: str, spec_json: str, commission_per_lot: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO symbol_specs (symbol, spec_json, commission_per_lot, updated)"
                          " VALUES (?, ?, ?, ?)", (symbol, spec_json, commission_per_lot, self.now()))

    def load_spec(self, symbol: str) -> Optional[tuple[str, float]]:
        row = self.conn.execute("SELECT spec_json, commission_per_lot FROM symbol_specs WHERE symbol = ?",
                                (symbol,)).fetchone()
        return (row["spec_json"], float(row["commission_per_lot"])) if row else None
