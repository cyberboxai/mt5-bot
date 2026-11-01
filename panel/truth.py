"""Server-side aggregation for the views that matter most: what is really happening.

Every number here is read from the database with indexed queries, so the browser never receives raw
rows it has to summarise. Evidence is shown as intervals, never as bare point estimates.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Mapping, Optional

from mt5bot.core.costs import all_in_cost_per_lot, cost_to_edge_ratio
from mt5bot.core.governance import VERDICT_PT
from mt5bot.funnel import describe
from mt5bot.services import complexity_budget, service_activity

KIND_SERVICE = {"decision.proposed": "signals", "decision.sized": "allocation", "decision.team": "allocation",
                "decision.blocked": "risk"}
STAGE_SERVICE = {"data": "data", "scan": "data", "signal": "signals", "tier": "allocation", "exploration": "allocation",
                 "budget": "allocation", "sizing": "allocation", "team": "allocation", "risk": "risk",
                 "affordability": "risk", "execution": "execution", "management": "execution"}


def _f(x: Any, digits: int = 4) -> Optional[float]:
    if x is None:
        return None
    value = float(x)
    return round(value, digits) if math.isfinite(value) else None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []       # a database from before the upgrade: the view says "no data"


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> Optional[dict[str, Any]]:
    rows = _rows(conn, sql, args)
    return rows[0] if rows else None


def _kv(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = _one(conn, "SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else None


def reality_gap(conn: sqlite3.Connection) -> dict[str, Any]:
    latest = _one(conn, "SELECT * FROM reality_gap ORDER BY id DESC LIMIT 1")
    history = _rows(conn, "SELECT ts, gap_r, n, status FROM reality_gap WHERE gap_r IS NOT NULL ORDER BY id DESC LIMIT 200")
    if latest is None:
        return {"status": "insufficient", "n": 0, "history": []}
    payload = json.loads(latest.pop("payload_json") or "{}")
    gap, se = latest.get("gap_r"), payload.get("gap_se")
    latest["interval"] = [_f(gap - 1.645 * se), _f(gap + 1.645 * se)] if gap is not None and se is not None else None
    latest["reasons"] = payload.get("reasons") or []
    latest["history"] = list(reversed(history))
    return latest


def _costs_by_symbol(conn: sqlite3.Connection, cfg: Any) -> dict[str, float]:
    """All-in round-turn cost per lot, per symbol, from the calibrated model (or the latest bars)."""
    out: dict[str, float] = {}
    hours = cfg.costs.expected_hold_bars * cfg.bar_seconds / 86400.0
    for s in _rows(conn, "SELECT symbol, spec_json, commission_per_lot FROM symbol_specs"):
        spec = json.loads(s["spec_json"])
        model = _one(conn, "SELECT SUM(spread_points * n) / MAX(SUM(n), 1) AS spread, SUM(slippage_points * n) / "
                           "MAX(SUM(n), 1) AS slip FROM cost_models WHERE symbol = ?", (s["symbol"],)) or {}
        spread = model.get("spread") or 0.0
        if not spread:
            bar = _one(conn, "SELECT spread FROM bars_cache WHERE symbol = ? ORDER BY time DESC LIMIT 1", (s["symbol"],))
            spread = float(bar["spread"]) if bar else 0.0
        value = float(spec["tick_value"]) / float(spec["tick_size"])
        out[s["symbol"]] = all_in_cost_per_lot(float(spread), float(model.get("slip") or cfg.backtest_slippage_points),
                                               float(spec["point"]), value, float(s["commission_per_lot"]), 0.0, hours)
    return out


def arms_truth(conn: sqlite3.Connection, cfg: Any, limit: int = 200) -> list[dict[str, Any]]:
    """Per arm: evidence as an interval, cost-to-edge, deflated Sharpe, PBO and the verdict."""
    verdicts: dict[tuple, dict[str, Any]] = {}
    for v in _rows(conn, "SELECT symbol, timeframe, strategy, dsr, pbo, cost_to_edge, verdict, n_trials, sharpe,"
                         " min_track_record, track_record FROM candidate_verdicts ORDER BY id"):
        verdicts[(v["symbol"], v["timeframe"], v["strategy"])] = v
    costs = _costs_by_symbol(conn, cfg)
    risk = {r["arm_id"]: float(r["lot_risk"]) for r in _rows(
        conn, "SELECT arm_id, AVG(risk_money / volume) AS lot_risk FROM trades WHERE arm_id IS NOT NULL AND volume > 0"
              " GROUP BY arm_id")}
    out = []
    rows = _rows(conn, "SELECT id, symbol, timeframe, strategy, tier, active, posterior_json FROM arms WHERE active = 1"
                       " ORDER BY tier DESC, id LIMIT ?", (limit,))
    for arm in rows:
        post = json.loads(arm["posterior_json"] or "{}")
        v = verdicts.get((arm["symbol"], arm["timeframe"], arm["strategy"])) or {}
        lower = post.get("lo")
        ratio = None
        cost, loss = costs.get(arm["symbol"]), risk.get(arm["id"])
        if post.get("mean") is not None and post.get("sd") is not None and cost and loss:
            ratio = cost_to_edge_ratio(float(post["mean"]) - cfg.costs.edge_z * float(post["sd"]), loss, cost)
        out.append({"id": arm["id"], "arm": f"{arm['strategy']} {arm['symbol']} {arm['timeframe']}", "tier": arm["tier"],
                    "mean_r": _f(post.get("mean")), "lo_r": _f(lower), "hi_r": _f(post.get("hi")),
                    "p_positive": _f(post.get("p_positive")), "trades": post.get("n_obs"),
                    "cost_to_edge": _f(ratio, 3), "research_cost_to_edge": _f(v.get("cost_to_edge"), 3),
                    "dsr": _f(v.get("dsr")), "pbo": _f(v.get("pbo")), "trials": v.get("n_trials"),
                    "verdict": v.get("verdict", "pending")})
    order = {"T3": 0, "T2": 1, "T1": 2, "T0": 3}
    return sorted(out, key=lambda a: (order.get(a["tier"], 4), -(a["p_positive"] or 0.0)))


def execution_quality(conn: sqlite3.Connection, since: float) -> dict[str, Any]:
    sessions = _rows(conn, "SELECT session, COUNT(*) AS orders, SUM(status IN ('FILLED', 'PARTIALLY_FILLED')) AS fills,"
                           " SUM(requotes) AS requotes, SUM(rejects) AS rejects, AVG(slippage_points) AS slippage,"
                           " AVG(latency_ms) AS latency, AVG(spread_submit) AS spread FROM tca WHERE ts >= ?"
                           " GROUP BY session ORDER BY session", (since,))
    hours = _rows(conn, "SELECT hour, COUNT(*) AS orders, AVG(slippage_points) AS slippage, AVG(latency_ms) AS latency,"
                        " SUM(status NOT IN ('FILLED', 'PARTIALLY_FILLED')) AS failed FROM tca WHERE ts >= ?"
                        " GROUP BY hour ORDER BY hour", (since,))
    states = {r["state"]: r["n"] for r in _rows(conn, "SELECT state, COUNT(*) AS n FROM orders GROUP BY state")}
    return {"sessions": sessions, "hours": hours, "order_states": states,
            "unprotected_alerts": (_one(conn, "SELECT COUNT(*) AS n FROM ledger WHERE kind ="
                                              " 'order.note.protection_failed'") or {}).get("n", 0)}


def tail_view(conn: sqlite3.Connection, health: Mapping[str, Any]) -> dict[str, Any]:
    latest = _one(conn, "SELECT * FROM stress_reports ORDER BY id DESC LIMIT 1")
    stress = None
    if latest is not None:
        payload = json.loads(latest["payload_json"] or "{}")
        stress = {"ts": latest["ts"], "worst": latest["worst_scenario"], "worst_loss_pct": latest["worst_loss_pct"],
                  "worst_margin_level": latest["worst_margin_level"], "breached": bool(latest["breached"]),
                  "scenarios": payload.get("scenarios", []), "cvar_pct": payload.get("cvar_95_pct_estimate")}
    tail = health.get("tail") or {}
    return {"margin_level": tail.get("margin_level"), "ladder": tail.get("ladder", 1.0),
            "stress_factor": tail.get("stress_factor", 1.0), "portfolio": tail.get("portfolio") or {}, "stress": stress}


def truth(conn: sqlite3.Connection, cfg: Any, health: Mapping[str, Any], now: float) -> dict[str, Any]:
    return {"reality_gap": reality_gap(conn), "arms": arms_truth(conn, cfg),
            "execution": execution_quality(conn, now - 30 * 86400), "tail": tail_view(conn, health),
            "trials": (_one(conn, "SELECT COUNT(*) AS n FROM trial_ledger") or {}).get("n", 0),
            "promotions_frozen": health.get("promotions_frozen")}


def decisions(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """Latest decisions in the log."""
    return _rows(conn, "SELECT correlation_id AS id, MIN(ts) AS ts, COUNT(*) AS events FROM ledger WHERE correlation_id"
                       " IS NOT NULL GROUP BY correlation_id ORDER BY MAX(seq) DESC LIMIT ?", (limit,))


def replay_decision(conn: sqlite3.Connection, decision_id: str) -> Optional[dict[str, Any]]:
    """One decision, step by step from the event log: inputs, each service, risk, order, fill, outcome."""
    events = _rows(conn, "SELECT seq, ts, kind, stream, stamp_json, payload_json FROM ledger WHERE correlation_id = ?"
                         " ORDER BY seq", (decision_id,))
    if not events:
        return None
    parts = decision_id.rsplit(":", 2)
    symbol, timeframe = (parts[0], parts[1]) if len(parts) == 3 else (None, None)
    bar_time = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
    steps: list[dict[str, Any]] = []
    if bar_time is not None:
        for f in _rows(conn, "SELECT ts, stage, outcome, reason, values_json FROM funnel_events WHERE symbol = ? AND"
                             " timeframe = ? AND bar_time = ? ORDER BY id", (symbol, timeframe, bar_time)):
            steps.append({"ts": f["ts"], "order": 0, "service": STAGE_SERVICE.get(f["stage"], "data"),
                          "kind": f"funnel.{f['stage']}", "title": describe(f["reason"]), "outcome": f["outcome"],
                          "data": json.loads(f["values_json"] or "{}")})
    stamp: dict[str, Any] = {}
    for e in events:
        stamp.update(json.loads(e["stamp_json"] or "{}"))     # the proposal carries data and model; later events do not
        steps.append({"ts": e["ts"], "order": 1, "service": "execution" if e["kind"].startswith("order.")
                      else KIND_SERVICE.get(e["kind"], "ops"), "kind": e["kind"], "title": e["kind"],
                      "outcome": "info", "data": json.loads(e["payload_json"] or "{}")})
    steps.sort(key=lambda s: (s["ts"], s["order"]))
    orders = _rows(conn, "SELECT client_id, state, side, volume, filled_volume, fill_price, ticket, position_ticket,"
                         " retcode, protected, detail FROM orders WHERE decision_id = ? ORDER BY created_ts", (decision_id,))
    outcome: list[dict[str, Any]] = []
    tickets = [o["position_ticket"] or o["ticket"] for o in orders if o["position_ticket"] or o["ticket"]]
    for ticket in tickets:
        outcome += _rows(conn, "SELECT mode, side, entry, exit, volume, pnl, r, exit_reason, status FROM trades WHERE"
                               " ticket = ?", (ticket,))
    if bar_time is not None:
        outcome += _rows(conn, "SELECT mode, side, entry, exit, volume, pnl, r, exit_reason, status FROM trades WHERE"
                               " mode = 'shadow' AND symbol = ? AND timeframe = ? AND json_extract(extra_json,"
                               " '$.signal_bar') = ? LIMIT 20", (symbol, timeframe, bar_time))
    return {"id": decision_id, "symbol": symbol, "timeframe": timeframe, "bar_time": bar_time, "stamp": stamp,
            "steps": steps, "orders": orders, "outcome": outcome}


def project(conn: sqlite3.Connection, cfg: Any, now: float, ablation: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    raw = _kv(conn, "governance_verdict")
    verdict = json.loads(raw) if raw else {}
    first = _one(conn, "SELECT MIN(open_ts) AS t FROM trades WHERE mode = 'live' AND tier IN ('T2', 'T3')") or {}
    first_ts = first.get("t")
    review = None
    if first_ts:
        review = float(first_ts) + cfg.governance.horizon_days * 86400
    status = verdict.get("status", "evidence_insufficient")
    research_only = _kv(conn, "research_only")
    funnel = {r["stage"]: r["n"] for r in _rows(conn, "SELECT stage, COUNT(*) AS n FROM funnel_events WHERE ts >= ?"
                                                      " GROUP BY stage", (now - 86400,))}
    return {"verdict": status, "verdict_pt": VERDICT_PT.get(status, status), "progress": verdict.get("progress", 0.0),
            "n": verdict.get("n", 0), "mean": _f(verdict.get("mean")), "ci_low": _f(verdict.get("ci_low")),
            "ci_high": _f(verdict.get("ci_high")), "reason": verdict.get("reason", ""),
            "horizon_trades": cfg.governance.horizon_trades, "horizon_days": cfg.governance.horizon_days,
            "next_review": review, "research_only": json.loads(research_only) if research_only else None,
            "services": service_activity(funnel), "components": complexity_budget(ablation, first_ts, now)}
