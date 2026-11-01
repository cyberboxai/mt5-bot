"""Governance reports: the stop rule's evidence and the weekly board report (PT-PT).

``core.governance`` decides; this module reads the journal and writes what the owner reads. Going
research-only needs no human, because a system with no edge should stop by itself. Coming back
always needs one: ``--reenable-real`` after reading the report.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .core.governance import VERDICT_PT, GovernanceRules, Verdict, evaluate
from .persistence import atomic_write_text

log = logging.getLogger("mt5_bot")

RESEARCH_ONLY_KEY = "research_only"
VERDICT_KEY = "governance_verdict"
_SQL_TRADES = ("SELECT pnl, open_ts FROM trades WHERE mode = 'live' AND status = 'closed' AND pnl IS NOT NULL AND tier"
               " IN ('T2', 'T3') ORDER BY close_ts")


def evaluate_account(conn: Any, rules: GovernanceRules, now: float) -> Verdict:
    rows = conn.execute(_SQL_TRADES).fetchall()
    return evaluate([float(r["pnl"]) for r in rows], float(rows[0]["open_ts"]) if rows else None, now, rules)


def research_only_state(registry: Any) -> Optional[dict[str, Any]]:
    raw = registry.get_kv(RESEARCH_ONLY_KEY)
    if not raw:
        return None
    state = json.loads(raw)
    return state if state.get("active") else None


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def write_stop_report(out_dir: Path, verdict: Verdict, now: float, account: str) -> Path:
    interval = f"[{verdict.ci_low:+.2f}; {verdict.ci_high:+.2f}]" if verdict.ci_low is not None else "n/d"
    lines = [
        f"# Relatório da regra de paragem, {_day(now)}", "",
        f"Conta: {account}", "",
        "A conta real passou para o modo **só investigação**: não são enviadas novas ordens reais e as posições",
        "abertas são geridas até ao fim com os seus SL e TP. A investigação e a conta demo continuam.", "",
        "## Evidência", "",
        f"- Veredicto: **{VERDICT_PT[verdict.status]}**",
        f"- Trades reais em T2 ou acima: {verdict.n}",
        f"- Expectativa líquida por trade, depois de todos os custos: {verdict.mean:+.2f}",
        f"- Intervalo de confiança: {interval}",
        f"- Explicação técnica: {verdict.reason}", "",
        "## O que isto significa", "",
        "Todo o intervalo de confiança está abaixo de zero: com os dados disponíveis, o sistema não mostra vantagem",
        "depois de custos nesta conta. Não significa que nunca a terá, nem o resultado seguinte é previsível.", "",
        "## Para voltar a negociar em real", "",
        "Só depois de ler este relatório e por decisão explícita do proprietário:", "",
        "    python mt5_bot.py --reenable-real", "",
    ]
    path = Path(out_dir) / f"paragem_{_day(now)}.md"
    atomic_write_text(path, "\n".join(lines))
    return path


def board_data(conn: Any, since: float, until: float) -> dict[str, Any]:
    trades = conn.execute("SELECT pnl, r, tier FROM trades WHERE mode = 'live' AND status = 'closed' AND close_ts >= ?"
                          " AND close_ts < ?", (since, until)).fetchall()
    flows = conn.execute("SELECT COALESCE(SUM(amount), 0) AS a FROM cash_flows WHERE ts >= ? AND ts < ?",
                         (since, until)).fetchone()["a"]
    equity = conn.execute("SELECT equity FROM equity_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1",
                          (until,)).fetchone()
    tiers = {r["tier"]: r["n"] for r in conn.execute("SELECT tier, COUNT(*) AS n FROM arms WHERE active = 1 GROUP BY tier")}
    changes = conn.execute("SELECT action, COUNT(*) AS n FROM promotions WHERE ts >= ? AND ts < ? GROUP BY action",
                           (since, until)).fetchall()
    incidents = conn.execute("SELECT COUNT(*) AS n FROM ledger WHERE ts >= ? AND ts < ? AND kind IN ('order.uncertain',"
                             " 'breaker.open', 'reconcile.adopted', 'order.note.protection_failed')",
                             (since, until)).fetchone()["n"]
    gap = conn.execute("SELECT * FROM reality_gap WHERE ts < ? ORDER BY id DESC LIMIT 1", (until,)).fetchone()
    spend = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM advisor_calls WHERE ts >= ? AND ts < ?",
                         (since, until)).fetchone()["c"]
    pnl = [float(r["pnl"]) for r in trades if r["pnl"] is not None]
    return {"trades": len(trades), "net": sum(pnl), "cash_flows": float(flows),
            "equity": float(equity["equity"]) if equity else None, "tiers": tiers,
            "changes": {r["action"]: r["n"] for r in changes}, "incidents": int(incidents),
            "gap": dict(gap) if gap else None, "advisor_usd": float(spend)}


def write_board_report(conn: Any, out_dir: Path, since: float, until: float, verdict: Verdict, account: str) -> Path:
    d = board_data(conn, since, until)
    gap = d["gap"]
    gap_text = "sem pares suficientes" if not gap or gap["gap_r"] is None else \
        f"{gap['gap_r']:+.3f}R por trade em {gap['n']} pares ({gap['status']})"
    interval = "n/d" if verdict.ci_low is None else f"[{verdict.ci_low:+.2f}; {verdict.ci_high:+.2f}] por trade"
    equity = "n/d" if d["equity"] is None else f"{d['equity']:.2f}"
    tiers = ", ".join(f"{k}: {v}" for k, v in sorted(d["tiers"].items())) or "nenhum braço activo"
    changes = ", ".join(f"{k}: {v}" for k, v in sorted(d["changes"].items())) or "sem alterações"
    lines = [
        f"# Relatório semanal do conselho, {_day(since)} a {_day(until)}", "",
        f"Conta: {account}", "",
        f"- Capital: {equity}", f"- Entradas e saídas de fundos: {d['cash_flows']:+.2f}",
        f"- Resultado líquido depois de custos: {d['net']:+.2f} em {d['trades']} trades",
        f"- Distância à realidade (real menos simulado): {gap_text}", f"- Braços por nível: {tiers}",
        f"- Componentes e braços alterados: {changes}", f"- Incidentes de execução: {d['incidents']}",
        f"- Gasto do conselheiro: {d['advisor_usd']:.2f} USD", "",
        f"**Veredicto: {VERDICT_PT[verdict.status]}.** Progresso até ao horizonte de avaliação: {verdict.progress:.0%}. "
        f"Expectativa líquida com intervalo {interval}.", "",
        "Nada neste relatório promete lucro: são estimativas a partir do passado.", "",
    ]
    path = Path(out_dir) / f"conselho_{_day(until)}.md"
    atomic_write_text(path, "\n".join(lines))
    conn.execute("INSERT INTO governance_reports (ts, kind, verdict, n_trades, expectancy_money, ci_low, ci_high,"
                 " payload_json) VALUES (?, 'board', ?, ?, ?, ?, ?, ?)",
                 (until, verdict.status, verdict.n, verdict.mean, verdict.ci_low, verdict.ci_high,
                  json.dumps(d, default=str)))
    return path


def report_facts(conn: Any, since: float, until: float, verdict: Verdict, equity: float) -> dict[str, Any]:
    """Relative numbers for the advisor's commentary: no balances, no account identifiers."""
    d = board_data(conn, since, until)
    return {"trades": d["trades"], "net_pct_of_equity": round(d["net"] / equity * 100.0, 3) if equity > 0 else None,
            "incidents": d["incidents"], "verdict": verdict.status, "progress": round(verdict.progress, 3),
            "trades_evaluated": verdict.n, "tiers": d["tiers"], "changes": d["changes"]}


def verdict_json(verdict: Verdict, now: float, rules: GovernanceRules) -> str:
    return json.dumps(dict(asdict(verdict), ts=now, horizon_trades=rules.horizon_trades,
                           horizon_days=rules.horizon_days), sort_keys=True)
