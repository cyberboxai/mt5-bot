"""Content hash of this source tree, computed once per process for reproducibility stamps."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .core.stamp import code_hash


@lru_cache(maxsize=1)
def source_hash() -> str:
    root = Path(__file__).resolve().parent
    return code_hash(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
