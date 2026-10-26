"""Upgrade of an installed bot to v3.1: backup, in-place migration, rollback.

``migrate_installation`` (``python mt5_bot.py --migrate``, also run automatically on the first
start of the new version) does, in this order:

1. a timestamped backup in ``backups/upgrade_<UTC>/`` of config.json, health.json and the whole
   state folder. SQLite files are copied with the online backup API, so the copy is
   consistent even with a WAL journal; a ``manifest.json`` lists every file with its SHA-256;
2. for every account folder still in the v3.0 layout (``registry.sqlite`` + ``office.sqlite``):
   merge both into ``mt5bot.sqlite`` (a fresh copy; the originals are kept, renamed
   ``*.v30``), apply the schema migrations, and turn the v3.0 champion and challengers plus
   every strategy that has journal history into arms, linking their past shadow and live
   trades so all history becomes evidence;
3. ``config.migrated.json`` next to config.json when keys were mapped (config.json itself is
   never rewritten).

``rollback_upgrade`` (``--rollback-upgrade``) moves the current state and config into
``backups/rolled_back_<UTC>/`` (nothing is deleted) and restores the chosen backup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .db import DB_NAME, LEGACY_OFFICE, LEGACY_REGISTRY, ensure_schema, user_version

log = logging.getLogger("mt5_bot")

BACKUP_ROOT = "backups"
DURABLE = True  # tests switch the per-commit fsync off (see Registry.durable)
LEGACY_SUFFIX = ".v30"
OFFICE_TABLES = ("events", "dossiers", "agent_state", "agent_weights", "weights_history", "calibration",
                 "advisor_calls", "advisor_states", "advisor_tilts", "briefings", "hypotheses", "control_audit",
                 "ablation_reports", "metrics", "kv")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite database (includes its WAL)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # mode=rw never creates a missing file; read-only mode can fail on a WAL database whose
    # shared-memory file is absent, and the bot is stopped while it migrates anyway.
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=rw", uri=True, timeout=30)
    target = sqlite3.connect(str(dst))
    if not DURABLE:
        target.execute("PRAGMA synchronous=OFF")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _is_sqlite_sidecar(path: Path) -> bool:
    return path.name.endswith(("-wal", "-shm", ".tmp", "-journal"))


def needs_migration(state_dir: Path) -> bool:
    state_dir = Path(state_dir)
    return (state_dir / LEGACY_REGISTRY).exists() and not (state_dir / DB_NAME).exists()


def account_dirs(state_root: Path) -> list[Path]:
    root = Path(state_root)
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and ((p / LEGACY_REGISTRY).exists() or (p / DB_NAME).exists()))


def backup_installation(project_dir: Path, config_path: Path, state_root: Path, health_file: Optional[Path] = None,
                        reason: str = "upgrade") -> Path:
    """Copy config, health and every state file into a new timestamped backup folder."""
    project_dir = Path(project_dir)
    target = project_dir / BACKUP_ROOT / f"{reason}_{_stamp()}"
    while target.exists():  # two backups in one second: never overwrite one
        time.sleep(1.0)
        target = project_dir / BACKUP_ROOT / f"{reason}_{_stamp()}"
    target.mkdir(parents=True)
    files: list[dict[str, Any]] = []

    def record(src: Path, dst: Path) -> None:
        files.append({"source": str(src), "backup": str(dst.relative_to(target)), "bytes": dst.stat().st_size,
                      "sha256": _sha256(dst)})

    config_path = Path(config_path)
    if config_path.exists():
        dst = target / "config" / config_path.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_path, dst)
        record(config_path, dst)
    if health_file is not None and Path(health_file).exists():
        dst = target / "health" / Path(health_file).name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(health_file, dst)
        record(Path(health_file), dst)
    state_root = Path(state_root)
    if state_root.exists():
        for src in sorted(state_root.rglob("*")):
            if not src.is_file() or _is_sqlite_sidecar(src):
                continue
            dst = target / "state" / src.relative_to(state_root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.suffix == ".sqlite":
                _sqlite_copy(src, dst)
            else:
                shutil.copy2(src, dst)
            record(src, dst)
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason,
                "code_version": __version__, "config_path": str(config_path.resolve()),
                "state_root": str(state_root.resolve()),
                "health_file": str(Path(health_file).resolve()) if health_file else None, "files": files}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.warning("Backup written to %s (%d files)", target, len(files))
    return target


# ---------------------------------------------------------------- state layout


def _copy_table(conn: sqlite3.Connection, table: str) -> int:
    attached = {r[1] for r in conn.execute(f"PRAGMA legacy.table_info({table})")}  # constant table names
    if not attached:
        return 0
    main = {r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")}
    cols = [c for c in attached if c in main]
    if not cols:
        return 0
    names = ", ".join(cols)
    cur = conn.execute(f"INSERT OR IGNORE INTO main.{table} ({names}) SELECT {names} FROM legacy.{table}")
    return int(cur.rowcount or 0)


def migrate_state_dir(state_dir: Path, legacy_timeframe: str = "M15") -> dict[str, Any]:
    """Merge the v3.0 databases of one account into ``mt5bot.sqlite`` and create arms from its history."""
    state_dir = Path(state_dir)
    report: dict[str, Any] = {"state_dir": str(state_dir), "action": "none"}
    target = state_dir / DB_NAME
    if target.exists():
        conn = sqlite3.connect(str(target), timeout=30, isolation_level=None)
        try:
            applied = ensure_schema(conn)
            report.update(action="schema" if applied else "current", applied=applied, version=user_version(conn))
        finally:
            conn.close()
        return report
    registry = state_dir / LEGACY_REGISTRY
    if not registry.exists():
        return report
    tmp = state_dir / (DB_NAME + ".migrating")
    if tmp.exists():
        tmp.unlink()
    _sqlite_copy(registry, tmp)
    conn = sqlite3.connect(str(tmp), timeout=30, isolation_level=None)
    copied: dict[str, int] = {}
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        if not DURABLE:
            conn.execute("PRAGMA synchronous=OFF")
        ensure_schema(conn)
        office = state_dir / LEGACY_OFFICE
        if office.exists():
            office_copy = state_dir / (LEGACY_OFFICE + ".migrating")
            _sqlite_copy(office, office_copy)
            conn.execute("ATTACH DATABASE ? AS legacy", (str(office_copy),))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in OFFICE_TABLES:
                    copied[table] = _copy_table(conn, table)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            conn.execute("DETACH DATABASE legacy")
            office_copy.unlink()
        report["arms"] = legacy_arms(conn, legacy_timeframe)
        report["version"] = user_version(conn)
    finally:
        conn.close()
    os.replace(tmp, target)
    renamed = []
    for name in (LEGACY_REGISTRY, LEGACY_OFFICE):
        for suffix in ("", "-wal", "-shm"):
            path = state_dir / (name + suffix)
            if path.exists():
                path.rename(path.with_name(path.name + LEGACY_SUFFIX))
                renamed.append(path.name)
    report.update(action="merged", copied=copied, renamed=renamed)
    log.warning("State %s migrated to %s (office rows copied: %s)", state_dir.name, DB_NAME,
                sum(copied.values()) if copied else 0)
    return report


def legacy_arms(conn: sqlite3.Connection, timeframe: str) -> dict[str, int]:
    """Create an arm for every (symbol, parameter set) with v3.0 history or champion/challenger
    status, and link its journal rows. Tier T0: the evidence model decides from the data."""
    now = time.time()
    keys: set[tuple[str, int]] = set()
    for row in conn.execute("SELECT DISTINCT symbol, param_set_id FROM trades WHERE param_set_id IS NOT NULL"):
        keys.add((row[0], int(row[1])))
    for table in ("champions", "challengers"):
        try:
            for row in conn.execute(f"SELECT symbol, param_set_id FROM {table} WHERE param_set_id IS NOT NULL"):
                keys.add((row[0], int(row[1])))
        except sqlite3.OperationalError:
            continue
    created = linked = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for symbol, ps in sorted(keys):
            strat = conn.execute("SELECT strategy FROM param_sets WHERE id = ?", (ps,)).fetchone()
            if strat is None:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO arms (symbol, timeframe, strategy, param_set_id, source, tier,"
                               " tier_since, created) VALUES (?, ?, ?, ?, 'legacy', 'T0', ?, ?)",
                               (symbol, timeframe, strat[0], ps, now, now))
            created += int(cur.rowcount or 0)
            arm = conn.execute("SELECT id FROM arms WHERE symbol = ? AND timeframe = ? AND param_set_id = ?",
                               (symbol, timeframe, ps)).fetchone()
            cur = conn.execute("UPDATE trades SET arm_id = ?, timeframe = COALESCE(timeframe, ?) WHERE symbol = ? AND"
                               " param_set_id = ? AND arm_id IS NULL", (arm[0], timeframe, symbol, ps))
            linked += int(cur.rowcount or 0)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return {"created": created, "trades_linked": linked}


def migrate_installation(project_dir: Path, config_path: Path, state_root: Path, health_file: Optional[Path],
                         legacy_timeframe: str, force_backup: bool = False) -> dict[str, Any]:
    """Backup once, then migrate every account folder. Idempotent: a current install is a no-op."""
    dirs = account_dirs(state_root)
    pending = [d for d in dirs if needs_migration(d)]
    report: dict[str, Any] = {"accounts": [], "backup": None}
    if pending or force_backup:
        report["backup"] = str(backup_installation(project_dir, config_path, state_root, health_file))
    for d in dirs:
        report["accounts"].append(migrate_state_dir(d, legacy_timeframe))
    return report


def latest_backup(project_dir: Path, name: Optional[str] = None) -> Optional[Path]:
    root = Path(project_dir) / BACKUP_ROOT
    if name:
        path = root / name
        return path if (path / "manifest.json").exists() else None
    candidates = sorted(p for p in root.glob("upgrade_*") if (p / "manifest.json").exists()) if root.exists() else []
    return candidates[-1] if candidates else None


def rollback_upgrade(project_dir: Path, config_path: Path, state_root: Path, backup: Optional[str] = None,
                     health_file: Optional[Path] = None, running_check_seconds: float = 120.0,
                     force: bool = False) -> dict[str, Any]:
    """Restore a backup. The current state and config are moved aside, never deleted."""
    source = latest_backup(project_dir, backup)
    if source is None:
        raise ValueError("no upgrade backup found in the backups folder" + (f" named {backup}" if backup else ""))
    if health_file is not None and Path(health_file).exists() and not force:
        age = time.time() - Path(health_file).stat().st_mtime
        if age < running_check_seconds:
            raise ValueError(f"the bot looks like it is running (health.json updated {age:.0f} s ago). Stop it "
                             "first (create the KILL file or close its window), then repeat; or pass --force.")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    aside = Path(project_dir) / BACKUP_ROOT / f"rolled_back_{_stamp()}"
    aside.mkdir(parents=True)
    state_root, config_path = Path(state_root), Path(config_path)
    if state_root.exists():
        shutil.move(str(state_root), str(aside / "state"))
    if config_path.exists():
        shutil.copy2(config_path, aside / config_path.name)
    restored = 0
    if (source / "state").exists():
        shutil.copytree(source / "state", state_root)
        restored = sum(1 for p in state_root.rglob("*") if p.is_file())
    else:
        state_root.mkdir(parents=True, exist_ok=True)
    backed_config = source / "config" / config_path.name
    if backed_config.exists():
        shutil.copy2(backed_config, config_path)
    log.warning("Rollback: restored %s (%d state files); the replaced state is in %s", source.name, restored, aside)
    return {"restored_from": str(source), "moved_aside_to": str(aside), "state_files": restored,
            "backup_created_utc": manifest.get("created_utc"), "backup_code_version": manifest.get("code_version")}
