"""``python mt5_bot.py --diagnose [--hours N]``: why is (or isn't) the bot trading?

With v3.1 data it prints the decision funnel from ``funnel_events`` (bars, signals, every
blocker with its reason code, the counterfactual value of each filter), the arms by tier, the
exploration budget, research status and the Flow Doctor's adjustments.

On an install that still has v3.0 data (no funnel table yet) it reconstructs the funnel from
what v3.0 recorded: research runs and the gate each candidate failed, champion and
challengers, shadow and live trades, and the known messages in ``bot.log``. Output is PT-PT.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from . import funnel as funnel_mod
from .db import DB_NAME, LEGACY_REGISTRY

GATE_PT = {
    "out-of-sample trades": "poucos trades fora da amostra",
    "expectancy": "expectativa fora da amostra <= 0 após custos",
    "bootstrap": "limite inferior bootstrap da média <= 0",
    "profit factor": "profit factor abaixo do mínimo",
    "max drawdown": "drawdown fora da amostra acima do máximo",
    "profitable in": "poucas janelas lucrativas",
    "deflated Sharpe": "Sharpe deflacionado abaixo do mínimo (penalização por testes múltiplos)",
    "most recent training window": "nada lucrativo na janela de treino mais recente",
    "neighbouring parameters": "parâmetros vizinhos fracos (pico isolado)",
    "composite score": "pontuação composta <= 0",
    "insufficient history": "histórico insuficiente para walk-forward",
}

LOG_PATTERNS = (
    ("OUTSIDE_HOURS", re.compile(r"outside trading window")),
    ("SPREAD_LIMIT", re.compile(r"spread [\d.]+ points > max")),
    ("MIN_LOT_EXCEEDS_RISK", re.compile(r"minimum volume .* would risk")),
    ("ONE_PER_SYMBOL", re.compile(r"already has a position on this symbol")),
    ("RISK_OPEN_RISK", re.compile(r"total open risk")),
    ("HALTED", re.compile(r"trading halted")),
    ("NEWS_BLACKOUT", re.compile(r"news blackout")),
    ("ORDER_CHECK_FAIL", re.compile(r"order_check rejected")),
    ("SIGNAL", re.compile(r" signal from ")),
    ("ORDER_OK", re.compile(r": opened (BUY|SELL) ")),
    ("OBSERVATION", re.compile(r"OBSERVATION: ")),
    ("NO_CANDIDATE", re.compile(r"NO TRADE: no candidate passed")),
    ("RESEARCH_FAILED", re.compile(r"(optimisation|research) run \d+ failed")),
    ("RESEARCH_STARTED", re.compile(r"(Optimisation|Research) process started")),
)
LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _gate_pt(reason: str) -> str:
    for needle, text in GATE_PT.items():
        if needle in reason:
            return text
    return reason[:80]


def scan_log(paths: Iterable[Path], since: float) -> Counter:
    """Count the known v3.0/v3.1 log messages after ``since`` (local timestamps in the log)."""
    counts: Counter = Counter()
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    m = LOG_TS.match(line)
                    if m:
                        try:
                            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                        except ValueError:
                            continue
                        if ts < since:
                            continue
                    for code, pattern in LOG_PATTERNS:
                        if pattern.search(line):
                            counts[code] += 1
        except OSError:
            continue
    return counts


def legacy_report(conn: sqlite3.Connection, since: float) -> list[str]:
    """Funnel reconstruction for a v3.0 database (no funnel_events)."""
    lines = ["Dados da versão 3.0 (sem funil instrumentado): reconstrução a partir do registo.", ""]
    runs = list(conn.execute("SELECT * FROM optimization_runs ORDER BY id DESC LIMIT 20")) \
        if _has(conn, "optimization_runs") else []
    if not runs:
        lines.append("  Nenhuma optimização registada: o bot nunca chegou a ter candidatos.")
    for run in runs[:5]:
        lines.append(f"  Optimização #{run['id']} {run['symbol']} ({run['trigger'][:60]}): {run['status']}, "
                     f"{run['trials']} ensaios. {run['notes'] or ''}")
    failed = [r for r in runs if r["status"] == "failed"]
    if failed and all(r["status"] == "failed" for r in runs[:1]):
        lines.append("  >> A última optimização FALHOU. Na versão 3.0 isto adiava a próxima tentativa uma semana "
                     "inteira (corrigido na 3.1: nova tentativa em 60 min).")
    gates: Counter = Counter()
    candidates = passed = 0
    if _has(conn, "candidate_metrics"):
        for row in conn.execute("SELECT passed, reasons_json FROM candidate_metrics"):
            candidates += 1
            passed += int(row["passed"])
            for reason in json.loads(row["reasons_json"] or "[]"):
                gates[_gate_pt(reason)] += 1
    lines.append("")
    lines.append(f"  Candidatos avaliados: {candidates}; aprovados em todos os portões: {passed}")
    for text, n in gates.most_common(10):
        lines.append(f"    {n:>4} x {text}")
    champs = list(conn.execute("SELECT * FROM champions")) if _has(conn, "champions") else []
    challengers = conn.execute("SELECT COUNT(*) FROM challengers").fetchone()[0] if _has(conn, "challengers") else 0
    lines.append("")
    lines.append(f"  Campeões promovidos: {sum(1 for c in champs if c['param_set_id'] is not None)}; "
                 f"desafiantes em sombra: {challengers}")
    if _has(conn, "trades"):
        shadow = conn.execute("SELECT COUNT(*) FROM trades WHERE mode='shadow'").fetchone()[0]
        live = conn.execute("SELECT COUNT(*) FROM trades WHERE mode='live'").fetchone()[0]
        lines.append(f"  Trades sombra: {shadow}; trades reais: {live}")
        if challengers and shadow:
            lines.append("  >> Mesmo com desafiantes, a promoção exigia 30 trades sombra ou 14 dias, mais 7 dias de "
                         "intervalo entre trocas: semanas sem negociar.")
    return lines


def diagnose(state_dir: Path, hours: float, log_files: Iterable[Path] = (), now: Optional[float] = None) -> str:
    unified, legacy = state_dir / DB_NAME, state_dir / LEGACY_REGISTRY
    path = unified if unified.exists() else legacy
    if not path.exists():
        return f"Não encontrei a base de dados em {state_dir}. O bot já arrancou nesta conta?"
    conn = _open(path)
    try:
        instrumented = _has(conn, "funnel_events")
        latest = conn.execute("SELECT MAX(ts) FROM funnel_events").fetchone()[0] if instrumented else None
        if now is None:
            # A simulation records virtual time: its window ends at its own last record, not at the PC clock.
            now = float(latest) + 1.0 if latest is not None and _is_simulation(conn) else time.time()
        since = now - hours * 3600
        extra: list[str] = []
        rep: dict[str, Any] = {"available": False}
        if latest is not None:  # v3.1 data: never fall back to the v3.0 reconstruction
            rep = funnel_mod.report(conn, since, now)
            if not rep.get("events"):
                rep = {"available": False, "quiet": True, "latest": float(latest)}
            extra.extend(_v31_extras(conn, since))
        else:
            extra.extend(legacy_report(conn, since))
        counts = scan_log(list(log_files), since)
        if counts:
            extra.append("")
            extra.append(f"Mensagens no registo (bot.log) nas últimas {hours:g} h:")
            labels = {"SIGNAL": "sinais", "ORDER_OK": "ordens abertas", "OUTSIDE_HOURS": "fora do horário",
                      "SPREAD_LIMIT": "spread acima do limite", "MIN_LOT_EXCEEDS_RISK": "lote mínimo acima do risco",
                      "ONE_PER_SYMBOL": "já com posição no símbolo", "RISK_OPEN_RISK": "risco aberto no limite",
                      "HALTED": "negociação suspensa", "NEWS_BLACKOUT": "janela de notícias",
                      "ORDER_CHECK_FAIL": "order_check recusou", "OBSERVATION": "linhas de estado 'OBSERVATION'",
                      "NO_CANDIDATE": "'nenhum candidato passou os portões'",
                      "RESEARCH_FAILED": "optimizações falhadas", "RESEARCH_STARTED": "optimizações lançadas"}
            for code, n in counts.most_common():
                extra.append(f"  {n:>6}  {labels.get(code, code)}")
        extra.append("")
        extra.extend(advice(rep, conn))
        return funnel_mod.format_report_pt(rep, hours, extra)
    finally:
        conn.close()


def _is_simulation(conn: sqlite3.Connection) -> bool:
    if not _has(conn, "kv"):
        return False
    row = conn.execute("SELECT value FROM kv WHERE key = 'simulation'").fetchone()
    return bool(row and row["value"] == "1")


def _v31_extras(conn: sqlite3.Connection, since: float) -> list[str]:
    lines: list[str] = []
    if _has(conn, "arms"):
        tiers = {r["tier"]: r["n"] for r in conn.execute(
            "SELECT tier, COUNT(*) AS n FROM arms WHERE active = 1 GROUP BY tier")}
        lines.append("Braços por nível: " + ", ".join(f"{t} {tiers.get(t, 0)}" for t in ("T0", "T1", "T2", "T3")))
        for r in conn.execute("SELECT ts, symbol, old_desc, new_desc, reason FROM promotions WHERE ts >= ? AND"
                              " action LIKE 'tier_%' ORDER BY id DESC LIMIT 8", (since,)):
            lines.append(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['ts']))} UTC {r['old_desc']} -> "
                         f"{r['new_desc']}: {r['reason'][:120]}")
    if _has(conn, "kv"):
        row = conn.execute("SELECT value FROM kv WHERE key = 'last_research_status'").fetchone()
        if row:
            lines.append(f"Investigação: {row['value']}")
    if _has(conn, "flow_adjustments"):
        rows = list(conn.execute("SELECT * FROM flow_adjustments ORDER BY id DESC LIMIT 5"))
        if rows:
            lines.append("Ajustes do Doutor de Fluxo:")
            for r in rows:
                lines.append(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['ts']))} UTC {r['direction']} "
                             f"{r['knob']} {r['old_value']:g} -> {r['new_value']:g} ({r['status']}): {r['reason'][:140]}")
    return lines


def advice(rep: dict[str, Any], conn: sqlite3.Connection) -> list[str]:
    """Plain-language next steps for the dominant blocker."""
    out = ["O que fazer:"]
    top = funnel_mod.dominant_blocker(rep) if rep.get("available") else None
    if rep.get("quiet"):
        last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(rep["latest"]))
        out.append(f"  Nenhuma decisão registada nesta janela; o último registo do funil é de {last} UTC. O bot está a "
                   "correr? Veja python mt5_bot.py --status e o fim do bot.log.")
        return out
    if not rep.get("available"):
        out.append("  Actualize para a versão 3.1 (docs/UPGRADE.md): o funil passa a ser registado e o bot começa a "
                   "explorar com micro-risco em vez de esperar por um campeão.")
        return out
    if rep.get("orders"):
        out.append(f"  O bot negociou {rep['orders']} vez(es) neste período. Veja os trades no painel.")
    if top is None:
        if not rep.get("signals"):
            out.append("  Sem sinais: mercado fechado ou sem dados. Verifique o terminal MT5 e o horário do mercado.")
        else:
            out.append("  Os sinais não foram bloqueados por nenhuma regra dominante.")
        return out
    code = top["reason"]
    tips = {
        "OUTSIDE_HOURS": "o horário (hora do SERVIDOR) está estreito; alargue trading_hours_server ou deixe o "
                         "Doutor de Fluxo alargá-lo dentro de 1h-23h.",
        "SPREAD_LIMIT": "o spread está caro face ao stop; prefira símbolos com spread menor ou períodos maiores.",
        "SPREAD_SPIKE": "picos de spread face ao habitual desta hora; normal perto do rollover e de notícias.",
        "MIN_LOT_EXCEEDS_RISK": "o lote mínimo da corretora arrisca demasiado para o saldo; veja o mapa de "
                                "viabilidade no painel e o capital mínimo por símbolo.",
        "EXPLORATION_BUDGET_SPENT": "o orçamento de exploração do dia/semana foi gasto; recomeça na próxima janela.",
        "THOMPSON_REJECT": "a evidência dos braços em sombra ainda é fraca; a exploração continua a amostrar.",
        "EXPLORATION_SLOTS_FULL": "as vagas de exploração estão ocupadas por trades abertos.",
        "ONE_PER_SYMBOL": "já existe uma posição no símbolo (regra: uma entrada por símbolo).",
        "HALTED_DAILY": "limite de perda diária atingido; retoma no próximo dia do servidor.",
        "HALTED_DRAWDOWN": "disjuntor de drawdown activo; reveja e use --resume.",
        "FLOOR_SOFT_STOP": "o capital tocou o piso de protecção de lucros; entradas param hoje.",
        "TEAM_LOW_CONFIDENCE": "a equipa não estava confiante; o Doutor de Fluxo pode baixar o mínimo até 0,50.",
        "TEAM_NO_AGREEMENT": "faltou concordância na equipa; o Doutor de Fluxo pode baixar o mínimo até 1.",
    }
    out.append(f"  Bloqueio principal: {code} ({funnel_mod.describe(code)}): {tips.get(code, 'veja o detalhe acima.')}")
    return out
