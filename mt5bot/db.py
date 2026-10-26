"""One database per account, one schema, ordered migrations.

``state/<server>_<login>/mt5bot.sqlite`` holds everything the bot persists: the trade
journal (live and shadow), arms and their tiers, research results, the decision funnel, the
audit log, the team's events and decision dossiers, learned weights, the advisor audit log
and capital state. Earlier versions split this across ``registry.sqlite`` and
``office.sqlite``; ``upgrade.migrate_state_dir`` merges them (after a backup).

The schema version is ``PRAGMA user_version``. ``ensure_schema`` applies every pending
migration in order, each in its own transaction, re-checking the version inside the
transaction so two processes opening the database at once cannot apply one twice. Every
migration is idempotent (``IF NOT EXISTS`` and column checks), so a database created by an
older build with the same tables is upgraded safely.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Union

DB_NAME = "mt5bot.sqlite"
LEGACY_REGISTRY = "registry.sqlite"
LEGACY_OFFICE = "office.sqlite"

Conn = Union[sqlite3.Connection, Any]  # sqlite3.Connection or registry.SerializedConnection


def db_path(state_dir: Union[str, Path]) -> Path:
    return Path(state_dir) / DB_NAME


# --------------------------------------------------------------------- schema 1
# The v3.0 tables (journal, registry, team office), unchanged so legacy data merges as is.

SCHEMA_1 = """
CREATE TABLE IF NOT EXISTS strategies (name TEXT PRIMARY KEY, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS param_sets (
    id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, params_json TEXT NOT NULL, UNIQUE (strategy, params_json));
CREATE TABLE IF NOT EXISTS optimization_runs (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, trigger TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
    status TEXT NOT NULL, trials INTEGER NOT NULL DEFAULT 0, data_from INTEGER, data_to INTEGER, notes TEXT);
CREATE TABLE IF NOT EXISTS candidate_metrics (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES optimization_runs(id), symbol TEXT NOT NULL,
    param_set_id INTEGER NOT NULL REFERENCES param_sets(id), passed INTEGER NOT NULL, reasons_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL, score REAL NOT NULL, rank INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_candidates_run ON candidate_metrics (run_id);
CREATE TABLE IF NOT EXISTS promotions (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, old_param_set_id INTEGER,
    new_param_set_id INTEGER, new_candidate_id INTEGER, old_desc TEXT NOT NULL, new_desc TEXT NOT NULL,
    reason TEXT NOT NULL, metrics_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS champions (
    symbol TEXT PRIMARY KEY, param_set_id INTEGER, candidate_id INTEGER, since REAL NOT NULL, promotion_id INTEGER);
CREATE TABLE IF NOT EXISTS challengers (
    symbol TEXT NOT NULL, param_set_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, added REAL NOT NULL,
    PRIMARY KEY (symbol, param_set_id));
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, param_set_id INTEGER, side TEXT NOT NULL,
    status TEXT NOT NULL, open_ts REAL NOT NULL, close_ts REAL, open_bar_time INTEGER, close_bar_time INTEGER,
    entry REAL NOT NULL, exit REAL, sl REAL NOT NULL, tp REAL NOT NULL, volume REAL NOT NULL, risk_money REAL NOT NULL,
    pnl REAL, r REAL, ticket INTEGER, exit_reason TEXT, extra_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_trades_ps ON trades (param_set_id, mode, status);
CREATE INDEX IF NOT EXISTS ix_trades_ticket ON trades (ticket);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts REAL NOT NULL, equity REAL NOT NULL, balance REAL NOT NULL, peak REAL, open_positions INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bars_cache (
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, time INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
    low REAL NOT NULL, close REAL NOT NULL, tick_volume INTEGER NOT NULL, spread INTEGER NOT NULL,
    real_volume INTEGER NOT NULL, PRIMARY KEY (symbol, timeframe, time));
CREATE TABLE IF NOT EXISTS symbol_specs (
    symbol TEXT PRIMARY KEY, spec_json TEXT NOT NULL, commission_per_lot REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, topic TEXT NOT NULL, agent TEXT, symbol TEXT,
    severity TEXT NOT NULL DEFAULT 'info', correlation_id TEXT, payload_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS ix_events_agent ON events (agent, id);
CREATE TABLE IF NOT EXISTS dossiers (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, symbol TEXT NOT NULL, timeframe TEXT, bar_time INTEGER, side TEXT,
    mode TEXT NOT NULL, strategy TEXT, param_set_id INTEGER, regime TEXT, session TEXT, status TEXT NOT NULL,
    final_action TEXT, team_multiplier REAL, advisor_multiplier REAL, final_multiplier REAL,
    inputs_json TEXT NOT NULL DEFAULT '{}', opinions_json TEXT NOT NULL DEFAULT '[]',
    weights_json TEXT NOT NULL DEFAULT '{}', ensemble_json TEXT NOT NULL DEFAULT '{}',
    advisor_json TEXT NOT NULL DEFAULT '{}', risk_json TEXT NOT NULL DEFAULT '{}',
    execution_json TEXT NOT NULL DEFAULT '{}', outcome_json TEXT NOT NULL DEFAULT '{}', r_base REAL,
    resolved_ts REAL, explanation TEXT);
CREATE INDEX IF NOT EXISTS ix_dossiers_ts ON dossiers (ts);
CREATE INDEX IF NOT EXISTS ix_dossiers_status ON dossiers (status, symbol);
CREATE TABLE IF NOT EXISTS agent_state (
    agent TEXT PRIMARY KEY, state TEXT NOT NULL, task TEXT, last_message TEXT, updated_ts REAL NOT NULL,
    benched INTEGER NOT NULL DEFAULT 0, bench_reason TEXT, errors INTEGER NOT NULL DEFAULT 0,
    stats_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS agent_weights (
    agent TEXT NOT NULL, regime TEXT NOT NULL, log_weight REAL NOT NULL, n INTEGER NOT NULL, updated_ts REAL NOT NULL,
    PRIMARY KEY (agent, regime));
CREATE TABLE IF NOT EXISTS weights_history (ts REAL NOT NULL, agent TEXT NOT NULL, regime TEXT NOT NULL,
    weight REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_weights_history ON weights_history (agent, ts);
CREATE TABLE IF NOT EXISTS calibration (
    agent TEXT PRIMARY KEY, a REAL NOT NULL, b REAL NOT NULL, n INTEGER NOT NULL, brier_raw REAL, brier_cal REAL,
    reliability_json TEXT NOT NULL DEFAULT '[]', updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS advisor_calls (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, purpose TEXT NOT NULL, model TEXT NOT NULL, prompt_hash TEXT NOT NULL,
    redacted_prompt TEXT NOT NULL, response TEXT NOT NULL, valid INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, latency_ms REAL NOT NULL, outcome TEXT NOT NULL, error_kind TEXT, detail TEXT,
    state_after TEXT);
CREATE INDEX IF NOT EXISTS ix_advisor_calls_ts ON advisor_calls (ts);
CREATE TABLE IF NOT EXISTS advisor_states (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS advisor_tilts (
    agent TEXT PRIMARY KEY, factor REAL NOT NULL, expires_ts REAL NOT NULL, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS briefings (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
    payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, strategy TEXT NOT NULL, param TEXT NOT NULL,
    value_json TEXT NOT NULL, rationale TEXT NOT NULL, status TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS control_audit (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, action TEXT NOT NULL, client TEXT NOT NULL, ok INTEGER NOT NULL,
    detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ablation_reports (id INTEGER PRIMARY KEY, ts REAL NOT NULL, n INTEGER NOT NULL,
    payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (ts REAL NOT NULL, name TEXT NOT NULL, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_metrics ON metrics (name, ts);
"""

# --------------------------------------------------------------------- schema 2
# v3.1: arms and tiers, research priors, the decision funnel, the Flow Doctor's adjustments,
# cash flows, management-policy evidence; trade rows learn their arm, timeframe and tier.

SCHEMA_2 = """
CREATE TABLE IF NOT EXISTS arms (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, strategy TEXT NOT NULL,
    param_set_id INTEGER NOT NULL REFERENCES param_sets(id), source TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'T0',
    tier_since REAL NOT NULL, last_change REAL, pinned_until REAL, active INTEGER NOT NULL DEFAULT 1,
    prior_n INTEGER NOT NULL DEFAULT 0, prior_mean REAL, prior_var REAL, prior_ts REAL,
    posterior_json TEXT NOT NULL DEFAULT '{}', policy TEXT NOT NULL DEFAULT 'fixed', created REAL NOT NULL,
    UNIQUE (symbol, timeframe, param_set_id));
CREATE INDEX IF NOT EXISTS ix_arms_symbol ON arms (symbol, timeframe, active);
CREATE TABLE IF NOT EXISTS research_results (
    id INTEGER PRIMARY KEY, run_id INTEGER, ts REAL NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
    strategy TEXT NOT NULL, param_set_id INTEGER, kind TEXT NOT NULL, n_oos INTEGER NOT NULL, mean_r REAL,
    var_r REAL, metrics_json TEXT NOT NULL DEFAULT '{}', flags_json TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS ix_research_key ON research_results (symbol, timeframe, strategy, ts);
CREATE TABLE IF NOT EXISTS funnel_events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, symbol TEXT, timeframe TEXT, arm_id INTEGER, stage TEXT NOT NULL,
    outcome TEXT NOT NULL, reason TEXT NOT NULL, bar_time INTEGER, values_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_funnel_ts ON funnel_events (ts);
CREATE INDEX IF NOT EXISTS ix_funnel_reason ON funnel_events (reason, ts);
CREATE TABLE IF NOT EXISTS flow_adjustments (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, knob TEXT NOT NULL, old_value REAL, new_value REAL,
    direction TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', review_after REAL,
    status TEXT NOT NULL, closed_ts REAL, outcome_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS cash_flows (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, ticket INTEGER UNIQUE, amount REAL NOT NULL, kind TEXT NOT NULL,
    comment TEXT);
CREATE TABLE IF NOT EXISTS policy_outcomes (
    id INTEGER PRIMARY KEY, arm_id INTEGER NOT NULL, trade_id INTEGER NOT NULL, policy TEXT NOT NULL, r REAL NOT NULL,
    ts REAL NOT NULL, UNIQUE (trade_id, policy));
CREATE INDEX IF NOT EXISTS ix_policy_arm ON policy_outcomes (arm_id, policy);
"""

TRADE_COLUMNS_2 = (("arm_id", "INTEGER"), ("timeframe", "TEXT"), ("tier", "TEXT"), ("risk_pct", "REAL"))


def _statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


def _columns(conn: Conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # table names are code constants


def _migration_1(conn: Conn) -> None:
    for statement in _statements(SCHEMA_1):
        conn.execute(statement)


def _migration_2(conn: Conn) -> None:
    for statement in _statements(SCHEMA_2):
        conn.execute(statement)
    existing = _columns(conn, "trades")
    for name, kind in TRADE_COLUMNS_2:
        if name not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {kind}")  # constants above, never input
    conn.execute("CREATE INDEX IF NOT EXISTS ix_trades_arm ON trades (arm_id, mode, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_trades_mode_close ON trades (mode, status, close_ts)")


MIGRATIONS: tuple[tuple[int, str, Callable[[Conn], None]], ...] = (
    (1, "v3.0 journal, registry and team office tables", _migration_1),
    (2, "v3.1 arms, tiers, research priors, funnel, flow doctor, cash flows, policies", _migration_2),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def user_version(conn: Conn) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def ensure_schema(conn: Conn) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied (empty when current)."""
    applied: list[int] = []
    if user_version(conn) >= SCHEMA_VERSION:
        return applied
    for version, _title, apply in MIGRATIONS:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if user_version(conn) >= version:
                conn.execute("COMMIT")
                continue
            apply(conn)
            conn.execute(f"PRAGMA user_version = {int(version)}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        applied.append(version)
    return applied


def connect(path: Union[str, Path], read_only: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    """Plain connection for tools (diagnose, migration). Read-only never creates the file."""
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=timeout,
                               isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn
