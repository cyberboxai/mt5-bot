"""Atomic file persistence and per-account state locations."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def atomic_write_text(path: Path, text: str, durable: bool = True) -> None:
    """Write ``text`` so readers see either the old file or the complete new one.

    The data is flushed and fsynced to a temp file in the same folder, then moved
    over the target with ``os.replace`` (atomic on NTFS and POSIX). A power cut at
    any point leaves the previous version intact. ``durable=False`` skips the fsync
    (simulation only: the replace is still atomic for readers).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        if durable:
            os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Any, durable: bool = True) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str), durable)


def account_key(server: str, login: int) -> str:
    """Folder name for one account: ``<server>_<login>`` with unsafe characters replaced."""
    return f"{_UNSAFE.sub('-', str(server)).strip('-') or 'server'}_{int(login)}"


def account_state_dir(state_root: str | os.PathLike[str], server: str, login: int) -> Path:
    path = Path(state_root) / account_key(server, login)
    path.mkdir(parents=True, exist_ok=True)
    return path
