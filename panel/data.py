"""Read-only data layer of the panel: one method per API endpoint.

Sources: ``health.json`` (written by the bot), ``registry.sqlite`` (journal, champions,
runs, equity, cached bars) and ``office.sqlite`` (events, dossiers, agents, advisor).
Connections are opened per request with ``mode=rw`` + ``query_only`` so the panel can
never create or modify them. Every result size is bounded. Missing data gives empty,
honestly labelled results, never invented numbers.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np

from mt5bot import __version__, limits
from mt5bot.officedb import OFFICE_DB, OfficeStore
from mt5bot.team.agents.market import correlation_matrix, currency_exposure, session_view
from mt5bot.team.context import PositionView
from mt5bot.team.roster import BY_ID, DEPARTMENTS, ROSTER, VOTERS

from . import metrics
from .office import annotate, derive

DAY = 86400.0
PAUSE_MARKER = "PAUSE_ENTRIES"
MAX_LOG_LINES = 200


class StateLocator:
    """Finds the account state folder: explicit, from health.json, or the only folder."""

    def __init__(self, cfg: Any, project_dir: Path, state_dir: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.project_dir = Path(project_dir)
        self.explicit = Path(state_dir) if state_dir else None

    def resolve(self, name: str) -> Path:
        path = Path(name)
        return path if path.is_absolute() else self.project_dir / path

    @property
    def health_path(self) -> Path:
        return self.resolve(self.cfg.health_file)

    def get(self) -> Optional[Path]:
        if self.explicit is not None:
            return self.explicit if self.explicit.exists() else None
        try:
            recorded = json.loads(self.health_path.read_text(encoding="utf-8")).get("state_dir")
            if recorded:
                path = self.resolve(recorded)
                if path.exists():
                    return path
        except (OSError, ValueError):
            pass
        root = self.resolve(self.cfg.state_root)
        folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
        candidates = [p for p in folders if (p / "registry.sqlite").exists()]
        return candidates[0] if len(candidates) == 1 else None


def _f(x: Any, digits: int = 6) -> Any:
    if isinstance(x, float):
        return round(x, digits) if math.isfinite(x) else None
    return x


class PanelData:
    def __init__(self, cfg: Any, locator: StateLocator, clock: Any = time.time) -> None:
        self.cfg = cfg
        self.locator = locator
        self.clock = clock
        self._sim_cache: Optional[tuple[Optional[Path], float, bool]] = None

    # ---------------------------------------------------------------- access

    @contextmanager
    def registry(self) -> Iterator[Optional[sqlite3.Connection]]:
        state = self.locator.get()
        path = state / "registry.sqlite" if state else None
        if path is None or not path.exists():
            yield None
            return
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def office(self) -> Iterator[Optional[OfficeStore]]:
        state = self.locator.get()
        path = state / OFFICE_DB if state else None
        if path is None or not path.exists():
            yield None
            return
        store = OfficeStore(path, read_only=True)
        try:
            yield store
        finally:
            store.close()

    def health(self) -> tuple[dict, Optional[float]]:
        path = self.locator.health_path
        try:
            return json.loads(path.read_text(encoding="utf-8")), self.clock() - path.stat().st_mtime
        except (OSError, ValueError):
            return {}, None

    @staticmethod
    def _kv(conn: Optional[sqlite3.Connection], key: str) -> Optional[str]:
        if conn is None:
            return None
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _simulation(self) -> bool:
        """True when the state folder belongs to a simulation (cached for a few seconds)."""
        state, wall = self.locator.get(), self.clock()
        cached = self._sim_cache
        if cached is not None and cached[0] == state and wall - cached[1] < 10:
            return cached[2]
        with self.registry() as reg:
            flag = self._kv(reg, "simulation") == "1"
        self._sim_cache = (state, wall, flag)
        return flag

    def now(self, health: Optional[dict] = None) -> float:
        """Reference time for windows such as "today" or "last 24 h".

        The wall clock, except for a simulation: its data is stamped with the bot's virtual
        clock, so the windows follow the bot's last loop instead (otherwise a demo would show
        every window empty)."""
        wall = self.clock()
        if not self._simulation():
            return wall
        if health is None:
            health, _ = self.health()
        try:
            return datetime.strptime(str(health["last_loop_utc"]), "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc).timestamp()
        except (KeyError, ValueError):
            return wall

    def _symbols(self, health: dict) -> list[str]:
        return list((health.get("symbols") or {}).keys()) or list(self.cfg.symbols)

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        health, age = self.health()
        now = self.now(health)
        offset = float(health.get("server_utc_offset_hours") or 2.0)
        state = self.locator.get()
        with self.registry() as reg:
            simulation = self._kv(reg, "simulation") == "1"
        team = health.get("team") or {}
        advisor = health.get("advisor") or {}
        return {
            "product": "CYBERBOX Trading Office", "version": __version__, "now_utc": now,
            "server_time": now + offset * 3600, "server_utc_offset_hours": offset,
            "account_type": health.get("account_type") or "?", "simulation": simulation,
            "dry_run": bool(health.get("dry_run")), "halted": health.get("halted"),
            "entry_block": health.get("entry_block"),
            "paused": bool(state and (state / PAUSE_MARKER).exists()),
            "bot_alive": age is not None and age < 180, "health_age_s": None if age is None else round(age, 1),
            "has_data": state is not None,
            "equity": health.get("equity"), "balance": health.get("balance"), "peak_equity": health.get("peak_equity"),
            "drawdown_pct": health.get("drawdown_pct"), "open_positions": health.get("open_positions"),
            "team": {k: team.get(k) for k in ("configured_mode", "effective_mode", "mode_reason", "guardian_freeze",
                                               "benched_performance", "benched_errors")},
            "advisor": {"state": advisor.get("state", "DISABLED"), "reason": advisor.get("reason", ""),
                        "provider": advisor.get("provider", "claude")},
            "sessions": session_view(now), "symbols": self._symbols(health),
            "language": self.cfg.panel.language, "theme": self.cfg.panel.theme,
            "disclaimer": "Resultados passados não garantem resultados futuros. O bot pode decidir não negociar.",
        }

    # ---------------------------------------------------------------- office

    def office_state(self) -> dict[str, Any]:
        health, _age = self.health()
        advisor = health.get("advisor") or {}
        advisor_state = advisor.get("state", "DISABLED")
        now = self.now(health)
        with self.office() as office:
            if office is None:
                out = derive({}, [], now, advisor_state, advisor.get("reason", ""))
                out.update(last_event_id=0, screens={}, waiting=True, now=now)
                return out
            rows = office.agent_states()
            events = [e for e in office.latest_events(600) if now - e["ts"] <= 300]
            out = derive(rows, events, now, advisor_state, advisor.get("reason", ""))
            out["last_event_id"] = office.max_event_id()
            out["now"] = now
            screens: dict[str, Any] = {}
            for symbol in self._symbols(health):
                raw = office.get_kv(f"market_view:{symbol}")
                if raw:
                    view = json.loads(raw)
                    regime = (view.get("views") or {}).get("regime_detector", {}).get("details", {})
                    screens[symbol] = {"bid": view.get("bid"), "ask": view.get("ask"),
                                       "spread_points": _f(view.get("spread_points"), 2),
                                       "regime": regime.get("regime"), "probs": regime.get("probs"),
                                       "atr_pct": _f(regime.get("atr_pct"), 1), "ts": view.get("ts")}
            risk = (rows.get("risk_manager") or {}).get("stats", {}).get("agent", {}).get("gauges", {})
            news = (rows.get("news_sentinel") or {}).get("stats", {}).get("agent", {}).get("news", {})
            out["screens"] = {"symbols": screens, "risk": risk, "next_news": (news or {}).get("next"),
                              "calendar": (news or {}).get("calendar"), "equity": health.get("equity"),
                              "drawdown_pct": health.get("drawdown_pct")}
            out["recent"] = [annotate(e, advisor_state) for e in events[-40:]]
        with self.registry() as reg:
            out["screens"]["pnl_today"] = self._pnl_since(reg, _day_start(now), health.get("equity"))
        return out

    # ---------------------------------------------------------------- equity

    def _pnl_since(self, reg: Optional[sqlite3.Connection], since: float, equity_now: Any) -> Optional[float]:
        if reg is None or equity_now is None:
            return None
        row = reg.execute("SELECT equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts LIMIT 1", (since,)).fetchone()
        return _f(float(equity_now) - float(row["equity"]), 2) if row else None

    def equity(self, days: float = 30.0) -> dict[str, Any]:
        health, _ = self.health()
        now = self.now(health)
        with self.registry() as reg:
            if reg is None:
                return {"points": [], "summary": {}, "insufficient": True}
            rows = list(reg.execute("SELECT ts, equity, balance, peak FROM equity_snapshots WHERE ts >= ? ORDER BY ts",
                                    (now - days * DAY,)))
            points = [{"ts": r["ts"], "equity": r["equity"], "balance": r["balance"], "peak": r["peak"]}
                      for r in metrics.downsample(rows)]
            first = reg.execute("SELECT equity FROM equity_snapshots ORDER BY ts LIMIT 1").fetchone()
            eq = health.get("equity")
            summary = {"today": self._pnl_since(reg, _day_start(now), eq),
                       "week": self._pnl_since(reg, now - 7 * DAY, eq),
                       "month": self._pnl_since(reg, now - 30 * DAY, eq),
                       "all_time": _f(float(eq) - float(first["equity"]), 2) if first and eq is not None else None,
                       "drawdown_pct": health.get("drawdown_pct"), "peak_equity": health.get("peak_equity"),
                       "equity": eq, "balance": health.get("balance")}
        return {"points": points, "summary": summary, "insufficient": len(points) < 2}

    # ----------------------------------------------------------- performance

    def performance(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with self.registry() as reg:
            if reg is None:
                return {"insufficient": True}
            live = [dict(r) for r in reg.execute("SELECT r, pnl, open_ts, close_ts, symbol FROM trades WHERE mode = "
                                                 "'live' AND status = 'closed' ORDER BY close_ts")]
            shadow = [dict(r) for r in reg.execute("SELECT r, pnl, open_ts, close_ts FROM trades WHERE mode = "
                                                   "'shadow' AND status = 'closed' ORDER BY close_ts")]
            points = [(r["ts"], r["equity"]) for r in reg.execute("SELECT ts, equity FROM equity_snapshots ORDER BY ts")]
            out["live"] = metrics.trade_metrics(live)
            out["shadow"] = metrics.trade_metrics(shadow)
            out["equity"] = metrics.equity_metrics(points)
            out["buy_and_hold"] = self._buy_and_hold(reg, points)
        out["baseline_vs_team"] = self._baseline_vs_team()
        return out

    def _buy_and_hold(self, reg: sqlite3.Connection, points: list) -> dict[str, Any]:
        if len(points) < 2:
            return {"available": False, "note": "dados insuficientes"}
        symbol = self.cfg.symbols[0]
        rows = list(reg.execute("SELECT time, close FROM bars_cache WHERE symbol LIKE ? ORDER BY time DESC LIMIT 5000",
                                (symbol + "%",)))
        if len(rows) < 100:
            return {"available": False, "note": "sem histórico de preços em cache para uma comparação com significado"}
        rows.reverse()
        first, last = rows[0]["close"], rows[-1]["close"]
        curve = [{"time": r["time"], "pct": _f((r["close"] / first - 1) * 100, 4)} for r in metrics.downsample(rows, 600)]
        return {"available": True, "symbol": symbol, "return_pct": _f((last / first - 1) * 100, 4), "curve": curve,
                "note": "variação do preço do instrumento no histórico em cache (sem alavancagem nem custos)"}

    def _baseline_vs_team(self) -> dict[str, Any]:
        from mt5bot.team.ablation import replay_multiplier
        from mt5bot.team.ensemble import EnsembleSettings

        with self.office() as office:
            if office is None:
                return {"n": 0, "insufficient": True}
            rows = office.resolved_dossiers(limit=20000)
            report = office.latest_ablation()
        settings = EnsembleSettings.from_config(self.cfg.team)
        base, team = [], []
        for d in rows:
            r = float(d["r_base"])
            base.append(r)
            team.append(replay_multiplier(d, settings) * r)
        return {"n": len(rows), "insufficient": len(rows) < self.cfg.team.ablation_min_samples,
                "baseline_cum_r": [_f(x, 4) for x in np.cumsum(base)][-2000:],
                "team_cum_r": [_f(x, 4) for x in np.cumsum(team)][-2000:],
                "report": report["payload"] if report else None}

    # ---------------------------------------------------------------- trades

    def trades(self, mode: str = "live", symbol: Optional[str] = None, limit: int = 100, offset: int = 0,
               sort: str = "open_ts", order: str = "desc", status: Optional[str] = None) -> dict[str, Any]:
        columns = {"open_ts", "close_ts", "r", "pnl", "symbol", "volume", "side", "status"}
        sort = sort if sort in columns else "open_ts"
        order_sql = "ASC" if order == "asc" else "DESC"
        limit = max(1, min(int(limit), 500))
        with self.registry() as reg:
            if reg is None:
                return {"rows": [], "total": 0}
            where, args = ["1 = 1"], []
            if mode in ("live", "shadow"):
                where.append("mode = ?")
                args.append(mode)
            if symbol:
                where.append("symbol = ?")
                args.append(symbol)
            if status in ("open", "closed"):
                where.append("status = ?")
                args.append(status)
            clause = " AND ".join(where)
            total = reg.execute(f"SELECT COUNT(*) AS n FROM trades WHERE {clause}", args).fetchone()["n"]
            rows = [dict(r) for r in reg.execute(
                f"SELECT * FROM trades WHERE {clause} ORDER BY {sort} {order_sql} LIMIT ? OFFSET ?",  # fixed columns
                args + [limit, max(0, int(offset))])]
            specs = {r["symbol"]: float(r["commission_per_lot"]) for r in reg.execute(
                "SELECT symbol, commission_per_lot FROM symbol_specs")}
            out_rows = [self._trade_row(reg, r, specs) for r in rows]
        with self.office() as office:
            for row in out_rows:
                did = row.get("dossier_id")
                d = office.dossier(did) if office is not None and did else None
                if d is not None:
                    row.update(regime=d.get("regime"), session=d.get("session"), team_multiplier=d.get(
                        "team_multiplier"), votes=[{"agent": o.get("agent"), "stance": o.get("stance"),
                                                    "confidence": o.get("confidence")}
                                                   for o in d.get("opinions", []) if o.get("role") == "voter"])
        return {"rows": out_rows, "total": int(total), "limit": limit, "offset": offset}

    def _trade_row(self, reg: sqlite3.Connection, r: dict, specs: dict[str, float]) -> dict[str, Any]:
        extra = json.loads(r.get("extra_json") or "{}")
        row = {k: r.get(k) for k in ("id", "mode", "symbol", "side", "status", "open_ts", "close_ts", "entry", "exit",
                                     "sl", "tp", "volume", "risk_money", "pnl", "r", "exit_reason", "param_set_id")}
        row.update(strategy=extra.get("strategy"), slippage_points=_f(extra.get("slippage_points"), 2),
                   dossier_id=extra.get("dossier_id"), size_multiplier=extra.get("size_multiplier"),
                   commission_estimate=_f(specs.get(r["symbol"], 0.0) * float(r["volume"]), 2),
                   swap="n/d", recovered=bool(extra.get("recovered")))
        row.update(self._mae_mfe(reg, r))
        return row

    @staticmethod
    def _mae_mfe(reg: sqlite3.Connection, r: dict) -> dict[str, Any]:
        """Max adverse / favourable excursion in R from cached bars, when available."""
        start = r.get("open_bar_time")
        end = r.get("close_bar_time") or start
        risk_dist = abs(float(r["entry"]) - float(r["sl"])) if r.get("sl") else 0.0
        if not start or risk_dist <= 0:
            return {"mae_r": None, "mfe_r": None}
        bars = list(reg.execute("SELECT high, low FROM bars_cache WHERE symbol = ? AND time >= ? AND time <= ? "
                                "LIMIT 3000", (r["symbol"], int(start), int(end) + 1)))
        if not bars:
            return {"mae_r": None, "mfe_r": None}
        hi = max(b["high"] for b in bars)
        lo = min(b["low"] for b in bars)
        entry = float(r["entry"])
        if r["side"] == "BUY":
            mae, mfe = (entry - lo) / risk_dist, (hi - entry) / risk_dist
        else:
            mae, mfe = (hi - entry) / risk_dist, (entry - lo) / risk_dist
        return {"mae_r": _f(max(0.0, mae), 3), "mfe_r": _f(max(0.0, mfe), 3)}

    # ---------------------------------------------------------------- agents

    def _accuracy(self, dossiers: list[dict]) -> dict[str, dict]:
        acc: dict[str, dict] = {}
        for d in dossiers:
            if d.get("r_base") is None:
                continue
            won = float(d["r_base"]) > 0
            side = 1 if d.get("side") == "BUY" else -1
            for o in d.get("opinions", []):
                if o.get("role") != "voter" or o.get("stance") not in ("long", "short"):
                    continue
                agrees = (1 if o["stance"] == "long" else -1) == side
                entry = acc.setdefault(o["agent"], {"n": 0, "right": 0, "by_regime": {}})
                right = int(agrees == won)
                entry["n"] += 1
                entry["right"] += right
                reg = entry["by_regime"].setdefault(d.get("regime") or "unknown", {"n": 0, "right": 0})
                reg["n"] += 1
                reg["right"] += right
        for entry in acc.values():
            entry["accuracy"] = _f(entry["right"] / entry["n"], 4) if entry["n"] else None
            for reg in entry["by_regime"].values():
                reg["accuracy"] = _f(reg["right"] / reg["n"], 4) if reg["n"] else None
        return acc

    def agents(self) -> dict[str, Any]:
        with self.office() as office:
            rows = office.agent_states() if office is not None else {}
            calibration = office.load_calibration() if office is not None else {}
            dossiers = office.resolved_dossiers(limit=5000) if office is not None else []
            ablation = office.latest_ablation() if office is not None else None
        accuracy = self._accuracy(dossiers)
        values = ((ablation or {}).get("payload") or {}).get("agents", {})
        out = []
        for info in ROSTER:
            row = rows.get(info.id) or {}
            stats = row.get("stats") or {}
            sup = stats.get("supervisor") or {}
            cal = calibration.get(info.id)
            out.append({
                "id": info.id, "name": info.name, "role": info.role_pt, "role_en": info.role_en,
                "department": info.department, "room": info.room, "icon": info.icon,
                "state": row.get("state", "idle"), "task": row.get("task"), "last_message": row.get("last_message"),
                "benched": bool(row.get("benched")), "bench_reason": row.get("bench_reason"),
                "voter": info.id in VOTERS, "weights": stats.get("weights", {}),
                "weight": (stats.get("weights") or {}).get("all"),
                "accuracy": (accuracy.get(info.id) or {}).get("accuracy"), "votes": (accuracy.get(info.id) or {}).get("n", 0),
                "brier_raw": _f(cal["brier_raw"], 5) if cal and cal.get("brier_raw") is not None else None,
                "brier_cal": _f(cal["brier_cal"], 5) if cal and cal.get("brier_cal") is not None else None,
                "value_added": (values.get(info.id) or {}).get("value_added_sharpe"),
                "value_ci": (values.get(info.id) or {}).get("ci"),
                "calls": sup.get("calls"), "failures": sup.get("failures"), "slow_calls": sup.get("slow_calls"),
                "uptime": _f(1 - (sup.get("failures") or 0) / sup["calls"], 4) if sup.get("calls") else None,
                "can": info.can_decide, "cannot": info.cannot_decide,
            })
        return {"agents": out, "departments": [{"id": k, "pt": v[0], "en": v[1]} for k, v in DEPARTMENTS.items()]}

    def agent(self, agent_id: str) -> Optional[dict[str, Any]]:
        if agent_id not in BY_ID:
            return None
        summary = next(a for a in self.agents()["agents"] if a["id"] == agent_id)
        with self.office() as office:
            if office is None:
                return dict(summary, messages=[], weight_history=[], reliability=[], opinions=[], accuracy_by_regime={},
                            details={})
            messages = [e for e in reversed(office.latest_events(3000)) if e["agent"] == agent_id
                        and e["topic"] in ("agent.message", "agent.state", "agent.error", "agent.benched")][:40]
            history = office.weight_history(agent_id, limit=2000)
            calibration = office.load_calibration().get(agent_id, {})
            row = office.agent_states().get(agent_id) or {}
            recent = office.dossiers(limit=60)
        accuracy = self._accuracy(recent).get(agent_id, {})
        opinions = []
        for d in recent:
            for o in d.get("opinions", []):
                if o.get("agent") == agent_id:
                    opinions.append({"dossier_id": d["id"], "ts": d["ts"], "symbol": d["symbol"], "side": d["side"],
                                     "stance": o.get("stance"), "confidence": o.get("confidence"),
                                     "weight": o.get("weight"), "reasons": o.get("reasons", [])[:3],
                                     "outcome_r": d.get("r_base"), "action": d.get("final_action")})
        return dict(summary, messages=[{"ts": m["ts"], "topic": m["topic"], "text": m["payload"].get("text")
                                        or m["payload"].get("message") or m["payload"].get("error")
                                        or m["payload"].get("reason") or m["payload"].get("task")} for m in messages],
                    weight_history=[{"ts": h["ts"], "regime": h["regime"], "weight": _f(h["weight"], 4)}
                                    for h in metrics.downsample(history, 800)],
                    reliability=calibration.get("reliability", []), opinions=opinions[:30],
                    accuracy_by_regime=accuracy.get("by_regime", {}), details=(row.get("stats") or {}).get("agent", {}))

    # ------------------------------------------------------------- decisions

    def decisions(self, query: Optional[str] = None, symbol: Optional[str] = None, action: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
        with self.office() as office:
            if office is None:
                return {"rows": []}
            rows = office.dossiers(limit=max(1, min(int(limit), 500)), offset=offset, symbol=symbol or None,
                                   action=action or None, query=query or None)
        out = []
        for d in rows:
            gate = (d.get("risk") or {}).get("gate") or (d.get("risk") or {}).get("preview") or {}
            out.append({"id": d["id"], "ts": d["ts"], "symbol": d["symbol"], "side": d["side"], "mode": d["mode"],
                        "strategy": d["strategy"], "regime": d["regime"], "session": d["session"],
                        "action": d["final_action"], "team_multiplier": d["team_multiplier"],
                        "advisor_multiplier": d["advisor_multiplier"], "final_multiplier": d["final_multiplier"],
                        "advisor": (d.get("advisor") or {}).get("status"), "risk_allowed": gate.get("allowed"),
                        "risk_reason": gate.get("reason"), "r_base": d.get("r_base"),
                        "reasons": (d.get("ensemble") or {}).get("reasons", [])[:3]})
        return {"rows": out}

    def decision(self, dossier_id: str) -> Optional[dict[str, Any]]:
        with self.office() as office:
            return office.dossier(dossier_id) if office is not None else None

    # ------------------------------------------------------------ strategies

    def strategies(self) -> dict[str, Any]:
        health, _ = self.health()
        out: dict[str, Any] = {"symbols": {}, "runs": [], "promotions": [], "rejected": []}
        with self.registry() as reg:
            if reg is None:
                return out
            for symbol, info in (health.get("symbols") or {}).items():
                out["symbols"][symbol] = {"champion": info.get("champion"), "status_line": info.get("status_line"),
                                          "performance": info.get("champion_performance"),
                                          "challengers": info.get("challengers", [])}
            runs = [dict(r) for r in reg.execute("SELECT * FROM optimization_runs ORDER BY id DESC LIMIT 20")]
            out["runs"] = runs
            for run in runs[:6]:
                cands = reg.execute("SELECT c.*, p.strategy, p.params_json FROM candidate_metrics c JOIN param_sets p "
                                    "ON p.id = c.param_set_id WHERE run_id = ? ORDER BY rank", (run["id"],))
                for c in cands:
                    m = json.loads(c["metrics_json"])
                    entry = {"run_id": run["id"], "symbol": run["symbol"], "strategy": c["strategy"],
                             "params": json.loads(c["params_json"]), "passed": bool(c["passed"]), "score": c["score"],
                             "reasons": json.loads(c["reasons_json"]),
                             "metrics": {k: _f(m.get(k), 4) for k in ("trades", "expectancy_r", "profit_factor",
                                                                       "max_drawdown_r", "deflated_sharpe",
                                                                       "profitable_window_ratio", "neighbor_pass_ratio",
                                                                       "total_trials")},
                             "stability": _choice_heatmap(m.get("windows_chosen") or [])}
                    out.setdefault("candidates", []).append(entry)
                    if not entry["passed"]:
                        out["rejected"].append({"run_id": run["id"], "strategy": c["strategy"],
                                                "gate": (entry["reasons"] or ["?"])[0]})
            out["promotions"] = [dict(r) for r in reg.execute(
                "SELECT id, ts, symbol, action, old_desc, new_desc, reason FROM promotions ORDER BY id DESC LIMIT 60")]
            out["shadow_progress"] = [dict(r) for r in reg.execute(
                "SELECT param_set_id, symbol, COUNT(*) AS trades, ROUND(AVG(r), 4) AS mean_r FROM trades "
                "WHERE mode = 'shadow' AND status = 'closed' GROUP BY param_set_id, symbol ORDER BY trades DESC "
                "LIMIT 30")]
        with self.office() as office:
            out["hypotheses"] = office.hypotheses(limit=30) if office is not None else []
        return out

    # ------------------------------------------------------------------ risk

    def risk(self) -> dict[str, Any]:
        health, _ = self.health()
        equity = float(health.get("equity") or 0.0)
        out: dict[str, Any] = {"equity": health.get("equity"), "drawdown_pct": health.get("drawdown_pct"),
                               "halted": health.get("halted"), "entry_block": health.get("entry_block")}
        with self.registry() as reg:
            open_rows = [dict(r) for r in reg.execute("SELECT symbol, side, volume, entry, sl, tp, risk_money FROM trades "
                                                      "WHERE mode = 'live' AND status = 'open'")] if reg else []
            points = [(r["ts"], r["equity"]) for r in reg.execute(
                "SELECT ts, equity FROM equity_snapshots ORDER BY ts")] if reg else []
            closes: dict[str, np.ndarray] = {}
            for symbol in self._symbols(health):
                if reg is None:
                    break
                rows = list(reg.execute("SELECT close FROM bars_cache WHERE symbol = ? ORDER BY time DESC LIMIT 600",
                                        (symbol,)))
                if len(rows) > 30:
                    closes[symbol] = np.array([r["close"] for r in reversed(rows)], dtype=float)
        by_symbol = {}
        for r in open_rows:
            entry = by_symbol.setdefault(r["symbol"], {"positions": 0, "volume": 0.0, "risk_money": 0.0})
            entry["positions"] += 1
            entry["volume"] += float(r["volume"])
            entry["risk_money"] += float(r["risk_money"])
        for entry in by_symbol.values():
            entry["risk_pct"] = _f(entry["risk_money"] / equity * 100, 4) if equity > 0 else None
        views = [PositionView(r["symbol"], r["side"], r["volume"], r["entry"], r["sl"], r["tp"], 0,
                              float(r["risk_money"]) / equity * 100 if equity > 0 else 0.0, 0.0, 0) for r in open_rows]
        names, corr = correlation_matrix(closes, 500) if len(closes) >= 2 else (list(closes), np.eye(len(closes)))
        worst = sum(float(r["risk_money"]) for r in open_rows)
        is_real = health.get("account_type") == "REAL"
        cfg = self.cfg
        out.update(
            by_symbol=by_symbol, by_currency=currency_exposure(views),
            correlation={"symbols": names, "matrix": [[_f(float(x), 3) for x in row] for row in corr]},
            var=metrics.var_estimate(points),
            worst_case_open_risk={"money": _f(worst, 2), "pct": _f(worst / equity * 100, 4) if equity > 0 else None,
                                  "note": "se todas as posições abertas atingissem o stop loss"},
            limits=[
                {"name": "risk_per_trade_pct", "config": cfg.risk_per_trade_pct(is_real),
                 "ceiling": limits.MAX_RISK_PER_TRADE_PCT},
                {"name": "max_daily_loss_pct", "config": cfg.max_daily_loss_pct(is_real),
                 "ceiling": limits.MAX_DAILY_LOSS_PCT},
                {"name": "max_total_open_risk_pct", "config": cfg.max_total_open_risk_pct,
                 "ceiling": limits.MAX_TOTAL_OPEN_RISK_PCT,
                 "current": _f(worst / equity * 100, 4) if equity > 0 else None},
                {"name": "max_open_positions_total", "config": cfg.max_open_positions_total,
                 "ceiling": limits.MAX_OPEN_POSITIONS, "current": len(open_rows)},
                {"name": "max_drawdown_from_peak_pct", "config": cfg.max_drawdown_from_peak_pct,
                 "ceiling": limits.MAX_DRAWDOWN_FROM_PEAK_PCT, "current": health.get("drawdown_pct")},
                {"name": "max_symbol_exposure_x", "config": cfg.max_symbol_exposure_x,
                 "ceiling": limits.MAX_SYMBOL_EXPOSURE_X},
            ],
            breakers={"halted": health.get("halted"), "guardian_freeze": (health.get("team") or {}).get(
                "guardian_freeze"), "entry_block": health.get("entry_block"),
                "drawdown_distance_pct": _f(cfg.max_drawdown_from_peak_pct - float(health.get("drawdown_pct") or 0), 3)})
        with self.office() as office:
            rows = office.agent_states() if office is not None else {}
        out["gauges"] = (rows.get("risk_manager") or {}).get("stats", {}).get("agent", {}).get("gauges", {})
        return out

    # --------------------------------------------------------------- markets

    def markets(self) -> dict[str, Any]:
        health, _ = self.health()
        now = self.now(health)
        out: dict[str, Any] = {"sessions": session_view(now), "symbols": {}}
        with self.office() as office:
            rows = office.agent_states() if office is not None else {}
            for symbol in self._symbols(health):
                raw = office.get_kv(f"market_view:{symbol}") if office is not None else None
                view = json.loads(raw) if raw else {}
                regime = (view.get("views") or {}).get("regime_detector", {}).get("details", {})
                sessions = ((rows.get("session_analyst") or {}).get("stats", {}).get("agent", {}).get("sessions") or {})
                session = sessions.get(symbol, {}) if isinstance(sessions, dict) else {}
                out["symbols"][symbol] = {
                    "regime": regime.get("regime"), "probs": regime.get("probs"), "atr_pct": _f(regime.get("atr_pct"), 1),
                    "er": _f(regime.get("er"), 3), "adx": _f(regime.get("adx"), 2),
                    "spread_points": _f(view.get("spread_points"), 2), "spread_ratio": session.get("spread_ratio"),
                    "spread_by_hour": session.get("spread_by_hour", {}), "updated": view.get("ts")}
            news = (rows.get("news_sentinel") or {}).get("stats", {}).get("agent", {}).get("news", {})
        out["news"] = {"calendar": (news or {}).get("calendar", "sem calendário"), "upcoming": (news or {}).get("upcoming",
                                                                                                           [])}
        return out

    # --------------------------------------------------------------- advisor

    def advisor(self) -> dict[str, Any]:
        health, _ = self.health()
        snap = health.get("advisor") or {}
        now = self.now(health)
        claude = self.cfg.claude
        out: dict[str, Any] = {"state": snap.get("state", "DISABLED"), "reason": snap.get("reason", ""),
                               "provider": snap.get("provider", "claude"), "model_fast": claude.model_fast,
                               "model_deep": claude.model_deep, "enabled": claude.enabled,
                               "caps": {"max_calls_per_hour": claude.max_calls_per_hour,
                                        "max_daily_cost_usd": claude.max_daily_cost_usd,
                                        "latency_budget_ms": claude.latency_budget_ms},
                               "budget": snap.get("budget"), "next_probe_at": snap.get("next_probe_at")}
        with self.office() as office:
            if office is None:
                out.update(history=[], calls_today=0, cost_today_usd=0.0, calls=[], verdicts={}, errors={},
                           latency_ms={}, briefing=None, research=None)
                return out
            today = office.advisor_calls(5000, since_ts=_day_start(now))
            recent = office.advisor_calls(200)
            out["history"] = office.advisor_states(100)
            briefings = office.briefings("daily", 1)
            research = office.briefings("research", 3)
            ablation = office.latest_ablation()
        verdicts: dict[str, int] = {}
        errors: dict[str, int] = {}
        for c in recent:
            if c["outcome"] == "ok" and c["purpose"] == "trade_review":
                try:
                    verdict = json.loads(c["response"]).get("verdict", "?")
                except ValueError:
                    verdict = "?"
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
            if c["error_kind"]:
                errors[c["error_kind"]] = errors.get(c["error_kind"], 0) + 1
        lat = sorted(float(c["latency_ms"]) for c in recent if c["latency_ms"])
        out.update(
            calls_today=len(today), cost_today_usd=_f(sum(float(c["cost_usd"]) for c in today), 6),
            calls=[{k: c[k] for k in ("ts", "purpose", "model", "outcome", "error_kind", "latency_ms", "cost_usd",
                                      "input_tokens", "output_tokens", "cache_read_tokens")} for c in recent[:50]],
            verdicts=verdicts, errors=errors,
            latency_ms={"p50": lat[len(lat) // 2] if lat else None, "p95": lat[int(len(lat) * 0.95)] if lat else None},
            value_added=((ablation or {}).get("payload") or {}).get("advisor"),
            briefing=briefings[0] if briefings else None, research=research,
            label="Comentário de IA: gerado pelo conselheiro opcional; nunca negoceia e só pode reduzir o risco")
        return out

    # ---------------------------------------------------------------- health

    def health_view(self) -> dict[str, Any]:
        health, age = self.health()
        now = self.now(health)
        out: dict[str, Any] = {"bot_alive": age is not None and age < 180, "health_age_s": age,
                               "last_loop_utc": health.get("last_loop_utc"), "last_tick_server": health.get(
                                   "last_tick_server"), "optimizer": health.get("optimizer"),
                               "event_bus": (health.get("team") or {}).get("event_bus"), "version": health.get("version")}
        with self.office() as office:
            series = {}
            for name in ("cpu_pct", "rss_mb", "loop_ms", "mt5_latency_ms", "tick_age_s", "office_db_mb", "log_mb",
                         "disk_free_gb", "registry_db_mb", "panel_heartbeat_age_s"):
                points = office.metrics(name, since_ts=now - DAY) if office is not None else []
                series[name] = [{"ts": t, "value": _f(v, 3)} for t, v in metrics.downsample(points, 400)]
            alerts = [e for e in (office.latest_events(3000) if office is not None else [])
                      if e["severity"] in ("warning", "critical")][-100:]
            audit = office.control_audit(50) if office is not None else []
        out.update(series=series, alerts=list(reversed(alerts)), control_audit=audit,
                   log_tail=self._tail(self.locator.resolve(self.cfg.log_file), MAX_LOG_LINES),
                   supervisor_restarts=self._count(self.locator.resolve("supervisor.log"), "Starting bot (start #"))
        return out

    @staticmethod
    def _tail(path: Path, lines: int) -> list[str]:
        try:
            with open(path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 64_000))
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            return []
        return text.splitlines()[-lines:]

    @staticmethod
    def _count(path: Path, needle: str) -> Optional[int]:
        try:
            return path.read_text(encoding="utf-8", errors="replace").count(needle)
        except OSError:
            return None

    # ---------------------------------------------------------------- events

    def events(self, after: int = 0, limit: int = 200, agent: Optional[str] = None, symbol: Optional[str] = None,
               severity: Optional[str] = None, since_ts: Optional[float] = None,
               until_ts: Optional[float] = None) -> list[dict]:
        health, _ = self.health()
        advisor_state = (health.get("advisor") or {}).get("state", "DISABLED")
        with self.office() as office:
            if office is None:
                return []
            rows = office.events_after(after, limit, since_ts, until_ts, agent, symbol, severity)
        return [annotate(e, advisor_state) for e in rows]

    def replay(self, start: float, end: float) -> dict[str, Any]:
        end = min(end, start + DAY)
        events = self.events(0, 20000, since_ts=start, until_ts=end)
        return {"from": start, "to": end, "events": events, "truncated": len(events) >= 20000}

    def team(self) -> dict[str, Any]:
        health, _ = self.health()
        agents = self.agents()
        with self.office() as office:
            history = office.weight_history(limit=5000) if office is not None else []
            ablation = office.latest_ablation() if office is not None else None
        weights: dict[str, list] = {}
        for h in history:
            if h["regime"] == "all":
                weights.setdefault(h["agent"], []).append({"ts": h["ts"], "weight": _f(h["weight"], 4)})
        return {"leaderboard": sorted(agents["agents"], key=lambda a: -(a["value_added"] or -9e9)),
                "departments": agents["departments"],
                "weights_over_time": {k: metrics.downsample(v, 300) for k, v in weights.items()},
                "benched": [a for a in agents["agents"] if a["benched"]], "ablation": ablation,
                "mode": health.get("team")}


def _day_start(ts: float) -> float:
    return float(int(ts // DAY) * DAY)


def _choice_heatmap(windows: list) -> dict[str, dict[str, int]]:
    """How often each parameter value was chosen across walk-forward windows (stability view)."""
    out: dict[str, dict[str, int]] = {}
    for params in windows:
        if not isinstance(params, dict):
            continue
        for name, value in params.items():
            bucket = out.setdefault(str(name), {})
            bucket[str(value)] = bucket.get(str(value), 0) + 1
    return out


def utc_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
