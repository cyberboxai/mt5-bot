"""Daily database backups, verified by an actual restore.

The online backup API copies a consistent database while the bot writes. A backup only counts when
a test restore passes ``PRAGMA integrity_check`` and the ledger and journal row counts match: an
unverified copy is a hope, not a backup.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("mt5_bot")

VERIFY_TABLES = ("ledger", "trades", "orders", "arms")


@dataclass(frozen=True)
class BackupResult:
    path: Path
    verified: bool
    detail: str


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # table names are constants above


def verify_restore(path: Path, expected: dict[str, int]) -> tuple[bool, str]:
    """Restore into a scratch folder and prove the copy is whole. The original is never touched."""
    with tempfile.TemporaryDirectory(prefix="restore_") as tmp:
        restored = Path(tmp) / "restored.sqlite"
        shutil.copyfile(path, restored)
        conn = sqlite3.connect(restored)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                return False, f"integrity_check: {check}"
            for table, count in expected.items():
                actual = _count(conn, table)
                if actual < count:
                    return False, f"{table}: {actual} rows < {count}"
        except sqlite3.DatabaseError as exc:
            return False, f"not a readable database: {exc}"
        finally:
            conn.close()
    return True, "restore verified"


def backup_database(db: Path, out_dir: Path, now: float, keep_days: int) -> BackupResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = out_dir / f"mt5bot_{stamp}.sqlite"
    source = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    copy = sqlite3.connect(target)
    try:
        with copy:
            source.backup(copy, pages=2048, sleep=0.05)
        expected = {t: _count(source, t) for t in VERIFY_TABLES}
    finally:
        copy.close()
        source.close()
    ok, detail = verify_restore(target, expected)
    if not ok:
        target.rename(target.with_suffix(".corrupt"))
        return BackupResult(target, False, detail)
    prune(out_dir, now, keep_days)
    return BackupResult(target, True, detail)


def prune(out_dir: Path, now: float, keep_days: int) -> int:
    """Retention. The newest backup always survives, however old it is."""
    files = sorted(Path(out_dir).glob("mt5bot_*.sqlite"), key=lambda p: p.name)
    removed = 0
    for path in files[:-1]:
        if now - path.stat().st_mtime > keep_days * 86400:
            path.unlink()
            removed += 1
    return removed


class BackupSchedule:
    """One backup per server day, at or after the configured hour."""

    def __init__(self, state_dir: Path, db: Path, keep_days: int, hour: int, clock: Callable[[], float]) -> None:
        self.dir = Path(state_dir) / "backups"
        self.db = Path(db)
        self.keep_days = keep_days
        self.hour = hour
        self.clock = clock
        self.last_day = ""
        self.failed: str = ""

    def maybe_run(self, server_ts: float) -> None:
        day = datetime.fromtimestamp(server_ts, tz=timezone.utc)
        key = day.strftime("%Y-%m-%d")
        if key == self.last_day or day.hour < self.hour:
            return
        self.last_day = key
        try:
            result = backup_database(self.db, self.dir, float(self.clock()), self.keep_days)
            self.failed = "" if result.verified else result.detail
            log.log(logging.INFO if result.verified else logging.CRITICAL, "Backup %s: %s", result.path.name, result.detail)
        except (OSError, sqlite3.Error) as exc:
            self.failed = str(exc)
            log.critical("Backup failed: %s", exc)
