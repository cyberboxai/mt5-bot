"""Observability: structured logs, Prometheus metrics, alert rules and clock discipline.

All optional and all read-only for trading: nothing here can change a decision, and a failure here
never reaches the loop.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import socket
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

log = logging.getLogger("mt5_bot")

_correlation: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("correlation", default=None)


@contextmanager
def correlated(correlation_id: Optional[str]) -> Iterator[None]:
    """Every log line inside carries this decision's or order's id."""
    token = _correlation.set(correlation_id)
    try:
        yield
    finally:
        _correlation.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": round(record.created, 3), "level": record.levelname, "logger": record.name,
            "message": record.getMessage(), "correlation_id": _correlation.get()}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


# ------------------------------------------------------------------------- metrics

METRICS: tuple[tuple[str, str, str], ...] = (
    ("mt5bot_loop_ms", "gauge", "Duration of the last trading loop"),
    ("mt5bot_tick_age_seconds", "gauge", "Age of the newest tick"),
    ("mt5bot_order_latency_ms", "gauge", "Mean submit-to-fill latency over the last orders"),
    ("mt5bot_slippage_points", "gauge", "Mean adverse slippage over the last orders"),
    ("mt5bot_reject_rate", "gauge", "Share of the last orders that did not fill"),
    ("mt5bot_funnel_events_total", "counter", "Funnel events by outcome in the last 24 hours"),
    ("mt5bot_arms", "gauge", "Active arms by tier"),
    ("mt5bot_margin_level_percent", "gauge", "Account margin level"),
    ("mt5bot_reality_gap_r", "gauge", "Live minus simulated R per trade"),
    ("mt5bot_advisor_state", "gauge", "1 for the advisor's current state"),
    ("mt5bot_unresolved_orders", "gauge", "Orders whose outcome is not settled"),
    ("mt5bot_drawdown_percent", "gauge", "Drawdown from the equity peak"),
)


def _sample(name: str, value: Any, labels: Optional[Mapping[str, str]] = None) -> Optional[str]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    tag = ""
    if labels:
        body = ",".join(f'{k}="{str(v).replace(chr(92), "").replace(chr(34), "")}"' for k, v in sorted(labels.items()))
        tag = "{" + body + "}"
    return f"{name}{tag} {value}"


def collect(conn: Any, health: Mapping[str, Any], now: float) -> dict[str, Any]:
    """Numbers for the metrics page, aggregated in SQL so a scrape stays cheap."""
    out: dict[str, Any] = {"health": health}
    row = conn.execute("SELECT AVG(latency_ms) AS latency, AVG(slippage_points) AS slip, COUNT(*) AS n,"
                       " SUM(status IN ('FILLED', 'PARTIALLY_FILLED')) AS fills FROM (SELECT * FROM tca ORDER BY id DESC"
                       " LIMIT 200)").fetchone()
    n = int(row["n"] or 0)
    out.update(latency=row["latency"], slippage=row["slip"], reject_rate=(1.0 - float(row["fills"] or 0) / n) if n else None)
    out["funnel"] = {r["outcome"]: int(r["n"]) for r in conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM funnel_events WHERE ts >= ? GROUP BY outcome", (now - 86400,))}
    gap = conn.execute("SELECT gap_r FROM reality_gap ORDER BY id DESC LIMIT 1").fetchone()
    out["gap"] = gap["gap_r"] if gap else None
    return out


def render_metrics(data: Mapping[str, Any]) -> str:
    """Prometheus text exposition format."""
    health: Mapping[str, Any] = data.get("health") or {}
    samples: dict[str, list[Optional[str]]] = {
        "mt5bot_loop_ms": [_sample("mt5bot_loop_ms", (health.get("team") or {}).get("loop_ms"))],
        "mt5bot_order_latency_ms": [_sample("mt5bot_order_latency_ms", data.get("latency"))],
        "mt5bot_slippage_points": [_sample("mt5bot_slippage_points", data.get("slippage"))],
        "mt5bot_reject_rate": [_sample("mt5bot_reject_rate", data.get("reject_rate"))],
        "mt5bot_margin_level_percent": [_sample("mt5bot_margin_level_percent", (health.get("tail") or {}).get("margin_level"))],
        "mt5bot_reality_gap_r": [_sample("mt5bot_reality_gap_r", data.get("gap"))],
        "mt5bot_unresolved_orders": [_sample("mt5bot_unresolved_orders", (health.get("execution") or {}).get("unresolved"))],
        "mt5bot_drawdown_percent": [_sample("mt5bot_drawdown_percent", health.get("drawdown_pct"))],
        "mt5bot_tick_age_seconds": [_sample("mt5bot_tick_age_seconds", health.get("tick_age_seconds"))],
        "mt5bot_funnel_events_total": [_sample("mt5bot_funnel_events_total", v, {"outcome": k})
                                       for k, v in sorted((data.get("funnel") or {}).items())],
        "mt5bot_arms": [_sample("mt5bot_arms", v, {"tier": k}) for k, v in sorted((health.get("tiers") or {}).items())],
    }
    state = (health.get("advisor") or {}).get("state")
    samples["mt5bot_advisor_state"] = [_sample("mt5bot_advisor_state", 1, {"state": str(state)})] if state else []
    lines: list[str] = []
    for name, kind, text in METRICS:
        rows = [s for s in samples.get(name, ()) if s]
        if rows:
            lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}", *rows]
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------- alerts


@dataclass(frozen=True)
class Alert:
    name: str
    severity: str     # info | warning | critical
    text: str


SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
DEDUPE_SECONDS = {"info": 6 * 3600.0, "warning": 3600.0, "critical": 600.0}


def evaluate_rules(h: Mapping[str, Any]) -> list[Alert]:
    """The alert rules. Each one has an entry with the same name in docs/RUNBOOK.md."""
    out: list[Alert] = []
    execution = h.get("execution") or {}
    tail = h.get("tail") or {}
    if execution.get("breaker") not in (None, "CLOSED"):
        out.append(Alert("broker_breaker_open", "critical", "Disjuntor do corretor aberto: novas entradas paradas"))
    if int(execution.get("unresolved") or 0) > 0:
        out.append(Alert("order_uncertain", "critical", f"{execution['unresolved']} ordem(ns) com resultado desconhecido"))
    if h.get("research_only"):
        out.append(Alert("research_only", "critical", "Conta real em modo só investigação (regra de paragem)"))
    if h.get("promotions_frozen"):
        out.append(Alert("reality_gap", "warning", f"Promoções congeladas: {h['promotions_frozen']}"))
    if float(tail.get("stress_factor") or 1.0) < 1.0:
        out.append(Alert("stress_breach", "warning", "Teste de stress excede os limites: risco novo reduzido"))
    if float(tail.get("ladder") or 1.0) < 1.0:
        out.append(Alert("derisk_ladder", "warning", f"Escada de redução de risco activa (x{tail['ladder']:.2f})"))
    drift = h.get("clock_drift_seconds")
    if isinstance(drift, (int, float)) and h.get("clock_drift_alert"):
        out.append(Alert("clock_drift", "warning", f"Relógio do PC desviado {drift:+.1f} s"))
    if h.get("backup_failed"):
        out.append(Alert("backup_failed", "critical", f"Cópia de segurança falhou: {h['backup_failed']}"))
    return out


class AlertEngine:
    """Delivers each alert through the notifier, once per de-duplication window and severity."""

    def __init__(self, send: Callable[..., Any], clock: Callable[[], float] = time.time) -> None:
        self.send = send
        self.clock = clock
        self._last: dict[str, float] = {}
        self.active: list[Alert] = []

    def process(self, alerts: Sequence[Alert]) -> list[Alert]:
        now = float(self.clock())
        self.active = list(alerts)
        sent = []
        for alert in alerts:
            if now - self._last.get(alert.name, -math.inf) >= DEDUPE_SECONDS.get(alert.severity, 3600.0):
                self._last[alert.name] = now
                try:
                    self.send(f"[{alert.severity}] {alert.text}", key=f"alert-{alert.name}")
                except Exception:  # noqa: BLE001 - delivery failure must never reach the loop
                    log.debug("alert not delivered", exc_info=True)
                sent.append(alert)
        return sent


# ------------------------------------------------------------------ clock discipline

NTP_EPOCH_OFFSET = 2_208_988_800


def broker_drift(pc_utc: float, tick_server_time: float, server_offset: float) -> float:
    """PC clock minus the broker's, from a live tick. Only meaningful while ticks are fresh."""
    return float(pc_utc - (tick_server_time - server_offset))


def ntp_offset(server: str, timeout: float = 2.0, clock: Callable[[], float] = time.time) -> Optional[float]:
    """Offset of the PC clock from an NTP server (SNTP, one query). None when it does not answer."""
    packet = b"\x1b" + 47 * b"\0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sent = clock()
            sock.sendto(packet, (server, 123))
            data, _addr = sock.recvfrom(256)
            received = clock()
    except OSError:
        return None
    if len(data) < 48:
        return None
    seconds, fraction = struct.unpack("!II", data[40:48])
    server_time = seconds - NTP_EPOCH_OFFSET + fraction / 2 ** 32
    return ((sent + received) / 2.0) - server_time
