"""Reproducibility stamps: which code, config, data and model produced a decision."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


def short_hash(data: bytes, length: int = 12) -> str:
    return hashlib.sha256(data).hexdigest()[:length]


def json_hash(value: Any, length: int = 12) -> str:
    return short_hash(json.dumps(value, sort_keys=True, default=str).encode("utf-8"), length)


def code_hash(paths: Iterable[Path]) -> str:
    """Content hash of the given source files (path relative names plus bytes), order independent."""
    digest = hashlib.sha256()
    for path in sorted(Path(p) for p in paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def make_stamp(code: str, config: str, data: Optional[str] = None, model: Optional[str] = None) -> dict[str, str]:
    stamp = {"code": code, "config": config}
    if data:
        stamp["data"] = data
    if model:
        stamp["model"] = model
    return stamp


def merge_stamp(base: Mapping[str, str], **extra: Optional[str]) -> dict[str, str]:
    out = dict(base)
    out.update({k: v for k, v in extra.items() if v})
    return out
