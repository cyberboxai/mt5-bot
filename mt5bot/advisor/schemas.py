"""Strict response schemas and a small validator. Anything that does not validate is
rejected as a whole and ignored: the pipeline then behaves as if the advisor were absent.

The schemas use a JSON-Schema subset. ``api_schema`` strips the bounds the structured
output feature may not support (numeric ranges, lengths, item counts); the local
validator enforces ALL of them on the way back, so the API constraint is a convenience
and this module is the actual defence.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Sequence

from .. import limits
from .types import (MANAGEMENT_ACTIONS, REASON_CODES, RISK_FLAGS, VERDICTS, Briefing, Hypothesis,
                    ManagementSuggestion, Reason, ResearchNote, WeightTilt)


class SchemaError(ValueError):
    """The advisor response does not match its schema."""


API_UNSUPPORTED = ("minimum", "maximum", "maxLength", "minLength", "maxItems", "minItems")

REASON_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "enum": list(REASON_CODES)},
        "text": {"type": "string", "maxLength": 240},
    },
    "required": ["code", "text"],
    "additionalProperties": False,
}

TRADE_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "size_multiplier": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasons": {"type": "array", "items": REASON_SCHEMA, "maxItems": 5},
        "risk_flags": {"type": "array", "items": {"type": "string", "enum": list(RISK_FLAGS)}, "maxItems": 8},
    },
    "required": ["verdict", "size_multiplier", "confidence", "reasons", "risk_flags"],
    "additionalProperties": False,
}

MANAGEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(MANAGEMENT_ACTIONS)},
        "new_stop_price": {"type": "number", "minimum": 0.0, "maximum": 1e9},
        "close_fraction": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasons": {"type": "array", "items": REASON_SCHEMA, "maxItems": 5},
    },
    "required": ["action", "new_stop_price", "close_fraction", "confidence", "reasons"],
    "additionalProperties": False,
}

PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {"explanation": {"type": "string", "maxLength": 1500}},
    "required": ["explanation"],
    "additionalProperties": False,
}


def briefing_schema(tiltable: Sequence[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "regime_view": {"type": "string", "maxLength": 400},
            "narrative": {"type": "string", "maxLength": 2000},
            "tilts": {
                "type": "array", "maxItems": len(tiltable),
                "items": {
                    "type": "object",
                    "properties": {"agent": {"type": "string", "enum": list(tiltable)},
                                   "factor": {"type": "number", "minimum": 1.0 - limits.MAX_WEIGHT_TILT,
                                              "maximum": 1.0 + limits.MAX_WEIGHT_TILT}},
                    "required": ["agent", "factor"],
                    "additionalProperties": False,
                },
            },
            "risk_flags": {"type": "array", "items": {"type": "string", "enum": list(RISK_FLAGS)}, "maxItems": 8},
        },
        "required": ["regime_view", "narrative", "tilts", "risk_flags"],
        "additionalProperties": False,
    }


def research_schema(strategy_names: Sequence[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "lesson": {"type": "string", "maxLength": 1500},
            "reasons": {"type": "array", "items": REASON_SCHEMA, "maxItems": 5},
            "hypotheses": {
                "type": "array", "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {"strategy": {"type": "string", "enum": list(strategy_names)},
                                   "param": {"type": "string", "maxLength": 40},
                                   "value": {"type": "string", "maxLength": 40},
                                   "rationale": {"type": "string", "maxLength": 400}},
                    "required": ["strategy", "param", "value", "rationale"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["lesson", "reasons", "hypotheses"],
        "additionalProperties": False,
    }


# ------------------------------------------------------------------------ validator


def validate(schema: dict, value: Any, path: str = "$") -> None:
    """Raise SchemaError on the first violation. Booleans are never numbers."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: expected object")
        props = schema.get("properties", {})
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            raise SchemaError(f"{path}: missing {missing}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise SchemaError(f"{path}: unexpected keys {extra}")
        for key, sub in props.items():
            if key in value:
                validate(sub, value[key], f"{path}.{key}")
        return
    if kind == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{path}: expected array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(f"{path}: fewer than {schema['minItems']} items")
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{i}]")
        return
    if kind == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path}: expected string")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path}: longer than {schema['maxLength']} characters")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SchemaError(f"{path}: expected finite number")
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: above {schema['maximum']}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise SchemaError(f"{path}: expected boolean")
    else:
        raise SchemaError(f"{path}: unsupported schema type {kind!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: value not allowed")


def api_schema(schema: Any) -> Any:
    """Copy of ``schema`` without the keywords structured outputs may reject."""
    if isinstance(schema, dict):
        return {k: api_schema(v) for k, v in schema.items() if k not in API_UNSUPPORTED}
    if isinstance(schema, list):
        return [api_schema(v) for v in schema]
    return schema


# ------------------------------------------------------------------------- parsers


def _reasons(items: Iterable[dict]) -> tuple[Reason, ...]:
    return tuple(Reason(r["code"], r["text"]) for r in items)


def parse_trade_review(data: dict) -> dict:
    """Validated fields of a pre-trade review (the caller wraps them in AdvisorOpinion)."""
    validate(TRADE_REVIEW_SCHEMA, data)
    return {"verdict": data["verdict"], "size_multiplier": float(data["size_multiplier"]),
            "confidence": float(data["confidence"]), "reasons": _reasons(data["reasons"]),
            "risk_flags": tuple(dict.fromkeys(data["risk_flags"]))}


def parse_management(data: dict) -> ManagementSuggestion:
    validate(MANAGEMENT_SCHEMA, data)
    return ManagementSuggestion(True, data["action"], float(data["new_stop_price"]), float(data["close_fraction"]),
                                float(data["confidence"]), _reasons(data["reasons"]))


def parse_briefing(data: dict, tiltable: Sequence[str], now: float, hours: float) -> Briefing:
    validate(briefing_schema(tiltable), data)
    seen: dict[str, WeightTilt] = {}
    for t in data["tilts"]:
        seen[t["agent"]] = WeightTilt(t["agent"], float(t["factor"]), now + hours * 3600.0)
    return Briefing(True, data["regime_view"], data["narrative"], tuple(seen.values()),
                    tuple(dict.fromkeys(data["risk_flags"])))


def parse_research(data: dict, strategy_names: Sequence[str], menu: Optional[Any] = None) -> ResearchNote:
    """``menu(strategy, param, value)`` returns the typed grid value, or None if it is not on the vetted menu."""
    validate(research_schema(strategy_names), data)
    hypotheses = []
    for h in data["hypotheses"]:
        typed = menu(h["strategy"], h["param"], h["value"]) if menu is not None else None
        if typed is None:
            continue  # off-menu proposals are dropped, never "fixed up"
        hypotheses.append(Hypothesis(h["strategy"], h["param"], typed, h["rationale"]))
    return ResearchNote(True, data["lesson"], _reasons(data["reasons"]), tuple(hypotheses))
