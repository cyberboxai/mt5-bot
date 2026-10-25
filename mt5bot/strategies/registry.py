"""Strategy registry. Strategies register themselves with ``@register`` at import time.

Only modules that live inside this package are imported (see ``__init__``). Nothing is
ever generated, evaluated or downloaded at runtime.
"""

from __future__ import annotations

from typing import TypeVar

from .base import Strategy

_REGISTRY: dict[str, Strategy] = {}
S = TypeVar("S", bound=type)


def register(cls: S) -> S:
    instance = cls()
    if not isinstance(instance, Strategy) or not instance.name:
        raise TypeError(f"{cls!r} must be a Strategy subclass with a name")
    if instance.name in _REGISTRY:
        raise ValueError(f"duplicate strategy name {instance.name!r}")
    _REGISTRY[instance.name] = instance
    return cls


def register_instance(instance: Strategy) -> Strategy:
    """For wrapped strategies built from other registered ones (e.g. volatility filters)."""
    if instance.name in _REGISTRY:
        raise ValueError(f"duplicate strategy name {instance.name!r}")
    _REGISTRY[instance.name] = instance
    return instance


def registered() -> dict[str, Strategy]:
    return dict(_REGISTRY)
