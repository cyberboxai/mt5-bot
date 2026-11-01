"""``config.recommended.json``: a starting configuration for the detected balance.

It is a proposal, written beside config.json and never applied by itself. Everything comes from
the live feasibility report, so it contains no money band.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .persistence import atomic_write_text

RECOMMENDED_NAME = "config.recommended.json"
FALLBACK_TIMEFRAMES = ("M15", "H1")


def recommendation(base: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``base`` with the universe the current balance can trade, and a note per decision."""
    rows: Sequence[Mapping[str, Any]] = report.get("rows") or []
    feasible = [r for r in rows if r.get("feasible")]
    symbols = list(dict.fromkeys(r["symbol"] for r in sorted(feasible, key=lambda r: r["min_lot_risk"])))
    frames = sorted({r["timeframe"] for r in feasible}, key=lambda tf: ("M5", "M15", "M30", "H1", "H4", "D1").index(tf))
    known = {r["symbol"] for r in rows}
    excluded = sorted(known - set(symbols))
    out: dict[str, Any] = json.loads(json.dumps(base))
    universe = dict(out.get("universe") or {})
    universe["candidates"] = symbols or list(universe.get("candidates") or [])
    universe["timeframes"] = frames or list(FALLBACK_TIMEFRAMES)
    universe["exclude"] = excluded
    out["universe"] = universe
    out["_recommended"] = {
        "equity": report.get("equity"), "currency": report.get("currency"), "floor": report.get("floor"),
        "mode": report.get("mode"), "excluded_because": {s: "lote mínimo acima do limite a este saldo" for s in excluded},
        "note": "Proposta para o saldo detectado. Reveja antes de usar; o bot nunca a aplica sozinho."}
    return out


def write_recommended(config_path: Path, recommended: Mapping[str, Any]) -> Path:
    target = Path(config_path).with_name(RECOMMENDED_NAME)
    atomic_write_text(target, json.dumps(recommended, indent=2, ensure_ascii=False) + "\n")
    return target
