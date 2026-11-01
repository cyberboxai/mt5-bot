"""Secret scanning: credentials must never appear in logs, the database or panel responses."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("telegram_token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b")),
    ("bearer_token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
SENSITIVE_ENV = ("MT5_PASSWORD", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "PANEL_TOKEN", "MT5BOT_PANEL_TOKEN")


def scan_text(text: str, literals: Sequence[str] = ()) -> list[str]:
    """Names of what was found. Never returns the secret itself."""
    found = [name for name, pattern in PATTERNS if pattern.search(text)]
    found += [f"literal_{i}" for i, value in enumerate(literals) if value and len(value) >= 6 and value in text]
    return found


def literals_from(env: Mapping[str, str]) -> list[str]:
    return [env[k] for k in SENSITIVE_ENV if env.get(k)]


def scan_files(paths: Iterable[Path], literals: Sequence[str] = ()) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = scan_text(text, literals)
        if hits:
            out[str(path)] = hits
    return out


def scan_database(db: Path, literals: Sequence[str] = ()) -> dict[str, list[str]]:
    """Every text column of every table."""
    conn = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)
    out: dict[str, list[str]] = {}
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")') if str(r[2]).upper() == "TEXT"]
            for column in columns:
                for (value,) in conn.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                    hits = scan_text(str(value), literals)
                    if hits:
                        out.setdefault(f"{table}.{column}", []).extend(hits)
    finally:
        conn.close()
    return out


def scan_project(root: Path, env: Optional[Mapping[str, str]] = None) -> dict[str, list[str]]:
    """Logs, reports, health files and databases of a state folder."""
    literals = literals_from(env or {})
    root = Path(root)
    files = [p for pattern in ("*.log", "*.json", "*.md", "*.txt") for p in root.rglob(pattern)]
    out = scan_files(files, literals)
    for db in root.rglob("*.sqlite"):
        out.update({f"{db.name}:{k}": v for k, v in scan_database(db, literals).items()})
    return out
