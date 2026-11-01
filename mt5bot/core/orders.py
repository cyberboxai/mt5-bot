"""Order lifecycle: return-code classification, state machine and idempotency keys.

Return codes are the official MQL5 list (ENUM trade server return codes, checked on
2026-11-01 at mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes). The rule
that drives everything: an answer that does not PROVE the outcome is UNCERTAIN, and an
uncertain order is never sent again until reconciliation proves it did not execute.

    INTENT -> SUBMITTED -> ACKED | UNCERTAIN -> FILLED | PARTIALLY_FILLED | REJECTED | CANCELLED
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Iterable, Optional

RETCODE_NAMES: dict[int, str] = {
    10004: "REQUOTE", 10006: "REJECT", 10007: "CANCEL", 10008: "PLACED", 10009: "DONE", 10010: "DONE_PARTIAL",
    10011: "ERROR", 10012: "TIMEOUT", 10013: "INVALID", 10014: "INVALID_VOLUME", 10015: "INVALID_PRICE",
    10016: "INVALID_STOPS", 10017: "TRADE_DISABLED", 10018: "MARKET_CLOSED", 10019: "NO_MONEY",
    10020: "PRICE_CHANGED", 10021: "PRICE_OFF", 10022: "INVALID_EXPIRATION", 10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS", 10025: "NO_CHANGES", 10026: "SERVER_DISABLES_AT", 10027: "CLIENT_DISABLES_AT",
    10028: "LOCKED", 10029: "FROZEN", 10030: "INVALID_FILL", 10031: "CONNECTION", 10032: "ONLY_REAL",
    10033: "LIMIT_ORDERS", 10034: "LIMIT_VOLUME", 10035: "INVALID_ORDER", 10036: "POSITION_CLOSED",
    10038: "INVALID_CLOSE_VOLUME", 10039: "CLOSE_ORDER_EXIST", 10040: "LIMIT_POSITIONS", 10041: "REJECT_CANCEL",
    10042: "LONG_ONLY", 10043: "SHORT_ONLY", 10044: "CLOSE_ONLY", 10045: "FIFO_CLOSE", 10046: "HEDGE_PROHIBITED",
}

RC_PLACED = 10008
RC_DONE = 10009
RC_DONE_PARTIAL = 10010
RC_NO_CHANGES = 10025
RC_MARKET_CLOSED = 10018
RC_POSITION_CLOSED = 10036

# Definite non-executions that may succeed moments later. Only these may ever be retried, and
# the retryable set in config is validated as a subset of this one.
TRANSIENT_RETCODES = frozenset({10004, 10020, 10021, 10023, 10024, 10029})
DEFAULT_RETRYABLE = frozenset({10004, 10020, 10021, 10024})
# The server may or may not have acted: only reconciliation can tell.
UNCERTAIN_RETCODES = frozenset({10011, 10012, 10028, 10031, 10039})
PERMANENT_RETCODES = frozenset({10006, 10007, 10013, 10014, 10015, 10016, 10017, 10018, 10019, 10022, 10026, 10027,
                                10030, 10032, 10033, 10034, 10035, 10038, 10040, 10041, 10042, 10043, 10044, 10045,
                                10046})


class Outcome(str, Enum):
    FILLED = "filled"          # 10009: completed
    PLACED = "placed"          # 10008: accepted; the fill still has to be confirmed
    PARTIAL = "partial"        # 10010: part of the volume completed
    NO_CHANGE = "no_change"    # 10025: a modification that was already in effect
    GONE = "gone"              # 10036: the position is already closed
    TRANSIENT = "transient"    # definite non-execution, retryable
    PERMANENT = "permanent"    # definite non-execution, never retried
    UNCERTAIN = "uncertain"    # unknown: reconcile before anything else


def classify(retcode: Optional[int], retryable: Iterable[int] = DEFAULT_RETRYABLE) -> Outcome:
    """Meaning of one broker answer. Unknown codes and ``None`` are UNCERTAIN, never failures."""
    if retcode is None:
        return Outcome.UNCERTAIN
    if retcode == RC_DONE:
        return Outcome.FILLED
    if retcode == RC_PLACED:
        return Outcome.PLACED
    if retcode == RC_DONE_PARTIAL:
        return Outcome.PARTIAL
    if retcode == RC_NO_CHANGES:
        return Outcome.NO_CHANGE
    if retcode == RC_POSITION_CLOSED:
        return Outcome.GONE
    if retcode in UNCERTAIN_RETCODES:
        return Outcome.UNCERTAIN
    if retcode in PERMANENT_RETCODES:
        return Outcome.PERMANENT
    if retcode in TRANSIENT_RETCODES:
        return Outcome.TRANSIENT if retcode in frozenset(retryable) else Outcome.PERMANENT
    return Outcome.UNCERTAIN


def retcode_name(retcode: Optional[int]) -> str:
    if retcode is None:
        return "NO_ANSWER"
    return RETCODE_NAMES.get(retcode, f"UNKNOWN_{retcode}")


def validate_retryable(codes: Iterable[int]) -> frozenset[int]:
    """Config check: a retryable code must be a documented, definite non-execution."""
    values = frozenset(int(c) for c in codes)
    bad = sorted(values - TRANSIENT_RETCODES)
    if bad:
        raise ValueError(f"retcodes {bad} may not be retried (allowed: {sorted(TRANSIENT_RETCODES)})")
    return values


# ------------------------------------------------------------------- state machine


class OrderState(str, Enum):
    INTENT = "INTENT"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    UNCERTAIN = "UNCERTAIN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


_S = OrderState
TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    _S.INTENT: frozenset({_S.SUBMITTED, _S.CANCELLED}),
    # SUBMITTED -> SUBMITTED is a retry after a DEFINITE transient rejection.
    _S.SUBMITTED: frozenset({_S.SUBMITTED, _S.ACKED, _S.UNCERTAIN, _S.PARTIALLY_FILLED, _S.FILLED, _S.REJECTED}),
    _S.ACKED: frozenset({_S.UNCERTAIN, _S.PARTIALLY_FILLED, _S.FILLED, _S.REJECTED, _S.CANCELLED}),
    _S.UNCERTAIN: frozenset({_S.PARTIALLY_FILLED, _S.FILLED, _S.REJECTED, _S.CANCELLED}),
    _S.PARTIALLY_FILLED: frozenset({_S.FILLED, _S.CANCELLED}),
    _S.FILLED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.CANCELLED: frozenset(),
}
TERMINAL = frozenset(state for state, nexts in TRANSITIONS.items() if not nexts)
# States in which the broker might hold (or come to hold) a position nobody has journaled.
UNRESOLVED = frozenset({_S.SUBMITTED, _S.ACKED, _S.UNCERTAIN})


class IllegalTransition(ValueError):
    """A state change the machine forbids. It means a bug, so it is never swallowed."""


def can_transition(current: OrderState, target: OrderState) -> bool:
    return target in TRANSITIONS[current]


def advance(current: OrderState, target: OrderState) -> OrderState:
    if not can_transition(current, target):
        raise IllegalTransition(f"{current.value} -> {target.value}")
    return target



# ---------------------------------------------------------------- idempotency keys

COMMENT_PREFIX = "cb4-"
CLIENT_ID_LENGTH = 12
_COMMENT = re.compile(rf"^{COMMENT_PREFIX}([0-9a-f]{{{CLIENT_ID_LENGTH}}})")


def client_id(*parts: object) -> str:
    """Deterministic key of one logical order: the same decision always maps to the same id, so a
    restart cannot turn one decision into two orders."""
    joined = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:CLIENT_ID_LENGTH]


def order_comment(cid: str) -> str:
    """16 characters: short enough that no broker truncates the id out of the comment."""
    return f"{COMMENT_PREFIX}{cid}"


def parse_comment(comment: Optional[str]) -> Optional[str]:
    match = _COMMENT.match(comment or "")
    return match.group(1) if match else None
