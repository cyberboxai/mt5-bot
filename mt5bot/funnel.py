"""The decision funnel: every gate between market data and an order emits a reason code.

Rows go to the ``funnel_events`` table (timestamp, symbol, timeframe, arm, stage, outcome,
reason code, key values). They feed ``--diagnose`` (top blockers in PT-PT), the panel's
"Porque nao estou a negociar?" tab and the Flow Doctor. Writes are buffered and flushed in
one transaction from the trading loop's housekeeping, so instrumentation never slows it.

Counterfactuals: a blocked signal is still paper traded by its arm's shadow book. Joining a
``block`` row with the arm's shadow trade of the next bar gives the R the filter avoided (or
cost), so each filter's net value can be measured instead of assumed.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Iterable, Mapping, Optional

log = logging.getLogger("mt5_bot")

# Stages, in pipeline order.
STAGES = ("data", "scan", "signal", "tier", "exploration", "budget", "sizing", "affordability", "team", "risk",
          "execution", "management")
OUTCOMES = ("pass", "block", "info")

# code: (stage, PT-PT description, Flow Doctor knob that may relax it, or None)
REASONS: dict[str, tuple[str, str, Optional[str]]] = {
    "BAR_EVALUATED": ("scan", "barra fechada avaliada", None),
    "SIGNAL": ("signal", "sinal gerado por um braço", None),
    "SELECTED": ("tier", "sinal escolhido para proposta de entrada", None),
    "EXPLORATION": ("exploration", "entrada de exploração (T1) escolhida por amostragem de Thompson", None),
    "NO_TICK": ("data", "sem cotação válida", None),
    "MARKET_CLOSED": ("data", "mercado fechado ou sem ticks recentes", None),
    "DATA_UNHEALTHY": ("data", "dados de mercado com problemas", None),
    "TRADE_MODE_RESTRICTED": ("data", "símbolo com negociação restrita pela corretora", None),
    "SYMBOL_QUARANTINED": ("execution", "símbolo em quarentena após falhas de execução seguidas", None),
    "OUTSIDE_HOURS": ("data", "fora do horário de negociação (hora do servidor)", "hours_extend"),
    "SPREAD_LIMIT": ("risk", "spread acima do limite", "spread_mult"),
    "SPREAD_SPIKE": ("risk", "pico de spread face ao habitual desta hora", "spread_mult"),
    "TIER_T0_SHADOW": ("tier", "braço em sombra (T0) e não escolhido para exploração", "t1_entry_p"),
    "THOMPSON_REJECT": ("exploration", "amostra de Thompson abaixo do limiar de exploração", None),
    "EXPLORATION_SLOTS_FULL": ("exploration", "vagas de exploração ocupadas", None),
    "EXPLORATION_BUDGET_SPENT": ("budget", "orçamento de exploração esgotado", None),
    "REGIME_INELIGIBLE": ("tier", "braço sem evidência positiva no regime actual", None),
    "MIN_LOT_EXCEEDS_RISK": ("affordability", "lote mínimo arrisca mais do que o permitido", "universe_extra"),
    "MIN_LOT_EXCEPTION": ("affordability", "lote mínimo aceite como excepção consciente", None),
    "SL_BELOW_BROKER_MIN": ("affordability", "stop abaixo da distância mínima da corretora", None),
    "SIZE_ZERO": ("sizing", "tamanho calculado é zero", None),
    "KELLY_ZERO": ("sizing", "Kelly fraccionado é zero (expectativa estimada não positiva)", None),
    "ANTI_MARTINGALE": ("sizing", "limite anti-martingala após uma perda", None),
    "FLOOR_SOFT_STOP": ("sizing", "capital no piso de protecção de lucros: entradas paradas hoje", None),
    "TEAM_NO_AGREEMENT": ("team", "concordância insuficiente na equipa", "min_agreement"),
    "TEAM_LOW_CONFIDENCE": ("team", "confiança agregada abaixo do mínimo", "min_aggregate_confidence"),
    "TEAM_DISAGREEMENT": ("team", "desacordo excessivo na equipa", None),
    "TEAM_VETO": ("team", "veto de um agente de controlo", None),
    "TEAM_SIZE_BELOW_MIN": ("team", "redução da equipa deixa o tamanho abaixo do lote mínimo", None),
    "TEAM_UNAVAILABLE": ("team", "equipa indisponível: decisão sem redução", None),
    "META_ABSTAIN": ("team", "meta-etiquetador neutro (sem dados)", None),
    "ADVISOR_VETO": ("team", "conselheiro Claude vetou", None),
    "ADVISOR_REDUCE": ("team", "conselheiro Claude reduziu o tamanho", None),
    "NEWS_BLACKOUT": ("risk", "janela de notícias", None),
    "ONE_PER_SYMBOL": ("risk", "já existe uma entrada neste símbolo", None),
    "RISK_MAX_POSITIONS": ("risk", "número máximo de posições abertas", None),
    "RISK_OPEN_RISK": ("risk", "risco aberto total no limite (calor da carteira)", None),
    "RISK_EXPOSURE": ("risk", "exposição nocional acima do limite", None),
    "RISK_PER_TRADE": ("risk", "risco por trade acima do permitido", None),
    "RISK_STOPS": ("risk", "SL/TP inválidos", None),
    "SLIPPAGE_PAUSE": ("risk", "pausa após derrapagem excessiva", None),
    "HALTED_DAILY": ("risk", "limite de perda diária atingido", None),
    "HALTED_DRAWDOWN": ("risk", "disjuntor de drawdown activo", None),
    "PAUSED": ("risk", "entradas em pausa (painel)", None),
    "GUARDIAN_FREEZE": ("risk", "Guardião congelou entradas", None),
    "FOREIGN_NETTING": ("risk", "conta netting com posição manual neste símbolo", None),
    "MARGIN_UNKNOWN": ("risk", "exposição não calculável (falha segura)", None),
    "ORDER_CHECK_FAIL": ("execution", "order_check recusou a ordem", None),
    "ORDER_SEND_FAIL": ("execution", "order_send falhou", None),
    "ORDER_OK": ("execution", "ordem executada com SL e TP", None),
    "DRY_RUN": ("execution", "modo de ensaio: ordem verificada mas não enviada", None),
    "OTHER": ("risk", "outro motivo (ver detalhe)", None),
}

BLOCKING_STAGES = ("data", "tier", "exploration", "budget", "sizing", "affordability", "team", "risk", "execution")


def describe(code: str) -> str:
    return REASONS.get(code, ("", code, None))[1]


def knob_for(code: str) -> Optional[str]:
    return REASONS.get(code, ("", "", None))[2]


def risk_reason_code(reason: str) -> str:
    """Map a risk-layer / execution message to a reason code."""
    text = reason.lower()
    table = (("trading halted: daily", "HALTED_DAILY"), ("daily loss limit", "HALTED_DAILY"),
             ("drawdown circuit breaker", "HALTED_DRAWDOWN"), ("paused from the panel", "PAUSED"),
             ("guardian", "GUARDIAN_FREEZE"), ("spread", "SPREAD_LIMIT"), ("slippage", "SLIPPAGE_PAUSE"),
             ("news blackout", "NEWS_BLACKOUT"), ("already has a position", "ONE_PER_SYMBOL"),
             ("netting account", "FOREIGN_NETTING"), ("bot positions open", "RISK_MAX_POSITIONS"),
             ("total open risk", "RISK_OPEN_RISK"), ("exposure", "RISK_EXPOSURE"),
             ("planned risk", "RISK_PER_TRADE"), ("sl/tp", "RISK_STOPS"), ("stop", "RISK_STOPS"),
             ("minimum volume", "MIN_LOT_EXCEEDS_RISK"), ("below broker minimum", "SL_BELOW_BROKER_MIN"),
             ("outside trading window", "OUTSIDE_HOURS"), ("order_check", "ORDER_CHECK_FAIL"),
             ("order_send", "ORDER_SEND_FAIL"), ("market reported closed", "MARKET_CLOSED"),
             ("trading mode is restricted", "TRADE_MODE_RESTRICTED"), ("no symbol info", "NO_TICK"),
             ("symbol quarantined", "SYMBOL_QUARANTINED"),
             ("order_calc_margin", "MARGIN_UNKNOWN"), ("halted", "HALTED_DAILY"))
    for needle, code in table:
        if needle in text:
            return code
    return "OTHER"


def team_reason_code(reason: str) -> str:
    text = reason.lower()
    if "veto" in text:
        return "TEAM_VETO"
    if "agreeing voter" in text or "min_agreement" in text:
        return "TEAM_NO_AGREEMENT"
    if "aggregate confidence" in text:
        return "TEAM_LOW_CONFIDENCE"
    if "disagreement" in text:
        return "TEAM_DISAGREEMENT"
    if "data unhealthy" in text or "data clerk" in text:
        return "DATA_UNHEALTHY"
    if "no edge in the" in text:
        return "REGIME_INELIGIBLE"
    if "below the minimum volume" in text:
        return "TEAM_SIZE_BELOW_MIN"
    return "TEAM_VETO"


@dataclass(frozen=True)
class FunnelEvent:
    ts: float
    stage: str
    outcome: str
    reason: str
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    arm_id: Optional[int] = None
    bar_time: Optional[int] = None
    values: Optional[Mapping[str, Any]] = None


def _clean(values: Optional[Mapping[str, Any]]) -> str:
    if not values:
        return "{}"
    out = {}
    for k, v in list(values.items())[:16]:
        if isinstance(v, float):
            out[str(k)] = round(v, 6) if math.isfinite(v) else None
        elif isinstance(v, (int, str, bool)) or v is None:
            out[str(k)] = v if not isinstance(v, str) else v[:200]
        else:
            out[str(k)] = str(v)[:200]
    return json.dumps(out, sort_keys=True)


class Funnel:
    """Buffered recorder plus in-memory counters (cheap reads for the Flow Doctor and health)."""

    def __init__(self, transaction: Optional[Callable[[], ContextManager[Any]]] = None,
                 clock: Callable[[], float] = time.time, max_buffer: int = 500) -> None:
        # ``transaction()`` opens a write transaction and yields its connection (``Registry.tx``):
        # the whole batch is written under the registry lock, never interleaved with another
        # thread's statements on the shared connection.
        self._transaction = transaction
        self.clock = clock
        self._buffer: list[FunnelEvent] = []
        self._lock = threading.Lock()
        self.max_buffer = max_buffer
        self.counts: Counter = Counter()
        self.last_signal_ts: Optional[float] = None
        self.last_order_ts: Optional[float] = None
        self.write_errors = 0

    def record(self, stage: str, outcome: str, reason: str, symbol: Optional[str] = None,
               timeframe: Optional[str] = None, arm_id: Optional[int] = None, bar_time: Optional[int] = None,
               values: Optional[Mapping[str, Any]] = None) -> None:
        if reason not in REASONS:
            values = dict(values or {}, original_reason=reason)
            reason = "OTHER"
        event = FunnelEvent(float(self.clock()), stage, outcome, reason, symbol, timeframe, arm_id, bar_time, values)
        with self._lock:
            self._buffer.append(event)
            self.counts[(outcome, reason)] += 1
            if reason == "SIGNAL":
                self.last_signal_ts = event.ts
            if reason in ("ORDER_OK", "DRY_RUN"):
                self.last_order_ts = event.ts
            full = len(self._buffer) >= self.max_buffer
        if full:
            self.flush()

    def flush(self) -> int:
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch or self._transaction is None:
            return 0
        rows = [(e.ts, e.symbol, e.timeframe, e.arm_id, e.stage, e.outcome, e.reason, e.bar_time, _clean(e.values))
                for e in batch]
        try:
            with self._transaction() as conn:
                conn.executemany("INSERT INTO funnel_events (ts, symbol, timeframe, arm_id, stage, outcome, reason,"
                                 " bar_time, values_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        except Exception:  # noqa: BLE001 - instrumentation must never break trading
            self.write_errors += 1
            log.debug("funnel flush failed", exc_info=True)
            return 0
        return len(rows)


# ------------------------------------------------------------------- reporting


def _rows(conn: Any, since: float, until: float) -> list[Any]:
    return list(conn.execute("SELECT ts, symbol, timeframe, arm_id, stage, outcome, reason, bar_time, values_json"
                             " FROM funnel_events WHERE ts >= ? AND ts < ? ORDER BY id", (since, until)))


def _has_table(conn: Any, name: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def counterfactuals(conn: Any, since: float, until: float) -> dict[str, dict[str, float]]:
    """Per blocking reason: virtual outcome of the blocked signals (from the arm's shadow trade)."""
    if not (_has_table(conn, "funnel_events") and _has_table(conn, "trades")):
        return {}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "arm_id" not in cols:
        return {}
    # The shadow trade of a signal opens on the first bar after the signal bar (weekend gaps allowed).
    sql = ("SELECT f.reason, (SELECT CASE WHEN t.status = 'closed' THEN t.r END FROM trades t"
           " WHERE t.arm_id = f.arm_id AND t.mode = 'shadow' AND t.open_bar_time > f.bar_time"
           " AND t.open_bar_time <= f.bar_time + 3 * 86400 ORDER BY t.open_bar_time LIMIT 1) AS r"
           " FROM funnel_events f WHERE f.outcome = 'block' AND f.arm_id IS NOT NULL AND f.bar_time IS NOT NULL"
           " AND f.ts >= ? AND f.ts < ?")
    out: dict[str, list[float]] = defaultdict(list)
    for reason, r in conn.execute(sql, (since, until)):
        if r is not None:
            out[reason].append(float(r))
    result = {}
    for reason, rs in out.items():
        total = sum(rs)
        result[reason] = {"n": len(rs), "virtual_total_r": round(total, 3), "virtual_mean_r": round(total / len(rs), 4),
                          "filter_value_r": round(-total, 3)}
    return result


def report(conn: Any, since: float, until: float) -> dict[str, Any]:
    """Aggregate funnel for [since, until): stages, top blockers, per strategy/symbol/timeframe."""
    if not _has_table(conn, "funnel_events"):
        return {"available": False, "since": since, "until": until}
    rows = _rows(conn, since, until)
    by_reason: Counter = Counter()
    by_stage: dict[str, Counter] = defaultdict(Counter)
    signals_by: dict[str, Counter] = {"strategy": Counter(), "symbol": Counter(), "timeframe": Counter()}
    blockers_by_symbol: dict[str, Counter] = defaultdict(Counter)
    bars = signals = orders = 0
    last_order = last_signal = None
    examples: dict[str, dict] = {}
    for ts, symbol, timeframe, arm_id, stage, outcome, reason, bar_time, values_json in rows:
        by_stage[stage][outcome] += 1
        if reason == "BAR_EVALUATED":
            bars += 1
            continue
        if reason == "SIGNAL":
            signals += 1
            last_signal = ts
            try:
                strategy = json.loads(values_json or "{}").get("strategy", "?")
            except ValueError:
                strategy = "?"
            signals_by["strategy"][strategy] += 1
            signals_by["symbol"][symbol or "?"] += 1
            signals_by["timeframe"][timeframe or "?"] += 1
            continue
        if reason in ("ORDER_OK", "DRY_RUN"):
            orders += 1
            last_order = ts
            continue
        if outcome == "block":
            by_reason[reason] += 1
            blockers_by_symbol[symbol or "?"][reason] += 1
            if reason not in examples:
                try:
                    examples[reason] = {"ts": ts, "symbol": symbol, "timeframe": timeframe,
                                        "values": json.loads(values_json or "{}")}
                except ValueError:
                    pass
    total_blocks = sum(by_reason.values())
    top = [{"reason": r, "count": n, "share": round(n / total_blocks, 4) if total_blocks else 0.0,
            "stage": REASONS.get(r, ("?",))[0], "description": describe(r), "knob": knob_for(r),
            "example": examples.get(r)} for r, n in by_reason.most_common(12)]
    return {"available": True, "since": since, "until": until, "events": len(rows), "bars_evaluated": bars,
            "signals": signals, "orders": orders, "blocked": total_blocks, "top_blockers": top,
            "by_stage": {s: dict(c) for s, c in by_stage.items()},
            "signals_by": {k: dict(v.most_common(20)) for k, v in signals_by.items()},
            "blockers_by_symbol": {s: dict(c.most_common(5)) for s, c in blockers_by_symbol.items()},
            "last_signal_ts": last_signal, "last_order_ts": last_order,
            "counterfactuals": counterfactuals(conn, since, until)}


def dominant_blocker(rep: Mapping[str, Any], relaxable_only: bool = False) -> Optional[dict]:
    for item in rep.get("top_blockers") or []:
        if not relaxable_only or item.get("knob"):
            return item
    return None


def format_report_pt(rep: Mapping[str, Any], hours: float, extra_lines: Iterable[str] = ()) -> str:
    """Plain-language PT-PT funnel for ``--diagnose``."""
    lines = [f"Funil de decisão das últimas {hours:g} h", ""]
    if rep.get("quiet"):
        lines.append("Nenhuma decisão registada nesta janela (o bot não avaliou barras neste período).")
        lines.extend(extra_lines)
        return "\n".join(lines)
    if not rep.get("available"):
        lines.append("Ainda não há registos do funil (a tabela funnel_events está vazia ou não existe).")
        lines.extend(extra_lines)
        return "\n".join(lines)
    lines += [f"  Barras avaliadas ............ {rep['bars_evaluated']}",
              f"  Sinais gerados .............. {rep['signals']}",
              f"  Sinais bloqueados ........... {rep['blocked']}",
              f"  Ordens executadas ........... {rep['orders']}", ""]
    if rep["signals_by"]["strategy"]:
        lines.append("Sinais por estratégia: " + ", ".join(f"{k} {v}" for k, v in rep["signals_by"]["strategy"].items()))
        lines.append("Sinais por símbolo: " + ", ".join(f"{k} {v}" for k, v in rep["signals_by"]["symbol"].items()))
        lines.append("Sinais por período: " + ", ".join(f"{k} {v}" for k, v in rep["signals_by"]["timeframe"].items()))
        lines.append("")
    if rep["top_blockers"]:
        lines.append("Principais bloqueios:")
        for i, b in enumerate(rep["top_blockers"], 1):
            relax = f" (o Doutor de Fluxo pode ajustar: {b['knob']})" if b.get("knob") else ""
            lines.append(f"  {i:>2}. {b['reason']:<26} {b['count']:>6}  {b['share']:>6.1%}  {b['description']}{relax}")
        lines.append("")
    cf = rep.get("counterfactuals") or {}
    if cf:
        lines.append("Valor dos filtros (resultado virtual dos sinais bloqueados, estimativa):")
        for reason, v in sorted(cf.items(), key=lambda kv: -kv[1]["n"]):
            verdict = "protegeu" if v["filter_value_r"] > 0 else "custou oportunidade"
            lines.append(f"  {reason:<26} n={v['n']:>4}  total virtual {v['virtual_total_r']:+8.2f}R  -> {verdict}")
        lines.append("")
    lines.extend(extra_lines)
    return "\n".join(lines)


def prune(conn: sqlite3.Connection, older_than: float) -> int:
    cur = conn.execute("DELETE FROM funnel_events WHERE ts < ?", (older_than,))
    return int(cur.rowcount or 0)
