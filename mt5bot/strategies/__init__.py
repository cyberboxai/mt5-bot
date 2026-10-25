"""Vetted strategy library.

Adding a strategy is a one-file change: create ``mt5bot/strategies/<name>.py`` with a
``Strategy`` subclass decorated with ``@register`` (see README, "Adicionar uma
estratégia"). Every module in this folder is imported once, in name order, at startup.
Only files that ship inside this package are loaded: nothing is generated, evaluated
or downloaded at runtime.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import Param, Params, Signal, SignalArrays, Strategy, canonical_params, describe
from .notrade import NO_TRADE, NOTRADE
from .registry import registered

_INTERNAL = {"base", "registry", "notrade", "regime_filter"}

for _module in sorted(m.name for m in pkgutil.iter_modules(__path__)):
    if _module not in _INTERNAL:
        importlib.import_module(f"{__name__}.{_module}")

from .regime_filter import register_filtered  # noqa: E402

register_filtered("ema_cross", "donchian")


def library() -> dict[str, Strategy]:
    """Tradable strategies by name (NoTrade is handled separately)."""
    return registered()


def library_names() -> tuple[str, ...]:
    return tuple(sorted(registered()))


def get(name: str) -> Strategy:
    if name == NO_TRADE:
        return NOTRADE
    try:
        return registered()[name]
    except KeyError as exc:
        raise KeyError(f"unknown strategy {name!r}; valid: {', '.join(library_names())}") from exc


__all__ = [
    "NO_TRADE", "NOTRADE", "Param", "Params", "Signal", "SignalArrays", "Strategy",
    "canonical_params", "describe", "get", "library", "library_names",
]
