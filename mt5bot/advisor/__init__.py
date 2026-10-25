"""Optional Claude advisor with guaranteed offline operation.

The advisor may only make the bot MORE conservative (reduce, veto, delay, tighten).
Without the ``anthropic`` package, without a key, without credit or without internet,
the bot trades exactly as with ``NullAdvisor``. See ``health`` for the state machine.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from .base import Advisor, AdvisorStore, NullAdvisor
from .types import AdvisorOpinion, AdvisorState, ErrorKind, unavailable


def build_advisor(cfg: Any, store: Optional[AdvisorStore] = None, env: Optional[Mapping[str, str]] = None,
                  notify: Callable[[str], None] = lambda text: None, no_claude: bool = False,
                  **kwargs: Any) -> Advisor:
    """``--no-claude`` gives a ``NullAdvisor``; otherwise a ``ClaudeAdvisor`` that reports
    DISABLED / NO_KEY on its own when it cannot be used (and never imports the SDK then)."""
    if no_claude:
        return NullAdvisor("advisor switched off with --no-claude")
    from .claude import ClaudeAdvisor  # noqa: PLC0415

    return ClaudeAdvisor(cfg.claude, store=store, env=env, notify=notify, **kwargs)


__all__ = ["Advisor", "AdvisorOpinion", "AdvisorState", "AdvisorStore", "ErrorKind", "NullAdvisor",
           "build_advisor", "unavailable"]
