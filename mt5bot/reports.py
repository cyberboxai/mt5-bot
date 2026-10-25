"""Daily and weekly plain-text and CSV reports, plus the health file."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from .backtest import longest_losing_streak, max_drawdown, profit_factor
from .persistence import atomic_write_json, atomic_write_text
from .registry import Registry, TradeRow


def trade_stats(rows: Sequence[TradeRow]) -> dict[str, float]:
    closed = [r for r in rows if r.status == "closed" and r.r is not None]
    rs = np.array([r.r for r in closed], dtype=float)
    pnl = np.array([r.pnl or 0.0 for r in closed], dtype=float)
    n = len(closed)
    return {
        "trades": n,
        "win_rate": float((rs > 0).mean()) if n else 0.0,
        "expectancy_r": float(rs.mean()) if n else 0.0,
        "profit_factor": profit_factor(rs) if n else 0.0,
        "net_pnl": float(pnl.sum()) if n else 0.0,
        "max_drawdown_r": max_drawdown(np.cumsum(rs)) if n else 0.0,
        "max_drawdown_money": max_drawdown(np.cumsum(pnl)) if n else 0.0,
        "longest_losing_streak": longest_losing_streak(rs.tolist()),
    }


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def write_report(registry: Registry, out_dir: Path, label: str, since: float, until: float,
                 account_desc: str, symbols: Sequence[str]) -> Path:
    """Write <label>.txt, <label>_trades.csv and <label>_equity.csv. Returns the .txt path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    live = [r for r in registry.trades(mode="live", status="closed") if since <= (r.close_ts or 0) < until]
    shadow = [r for r in registry.trades(mode="shadow", status="closed") if since <= (r.close_ts or 0) < until]
    points = [p for p in registry.equity_points(since) if p["ts"] < until]
    stats = trade_stats(live)
    shadow_stats = trade_stats(shadow)

    lines = [
        f"MT5 bot report {label}",
        f"Account: {account_desc}",
        f"Period: {_fmt_ts(since)} to {_fmt_ts(until)}",
        "",
        "LIVE TRADES (closed in the period)",
        f"  trades: {stats['trades']}",
        f"  win rate: {stats['win_rate']:.1%}",
        f"  expectancy: {stats['expectancy_r']:+.3f} R per trade",
        f"  profit factor: {stats['profit_factor']:.2f}",
        f"  net P&L: {stats['net_pnl']:.2f}",
        f"  max drawdown: {stats['max_drawdown_r']:.2f} R ({stats['max_drawdown_money']:.2f})",
        f"  longest losing streak: {stats['longest_losing_streak']}",
        "",
        f"SHADOW TRADES: {shadow_stats['trades']} closed, expectancy {shadow_stats['expectancy_r']:+.3f} R",
        "",
    ]
    if points:
        first, last = points[0], points[-1]
        lines.append(f"EQUITY: {first['equity']:.2f} -> {last['equity']:.2f} ({len(points)} snapshots)")
        lines.append("")
    lines.append("CHAMPIONS")
    for symbol in symbols:
        champ = registry.champion(symbol)
        lines.append(f"  {symbol}: {champ.describe()} since {_fmt_ts(registry.champion_since(symbol))}")
        last = next((p for p in registry.promotions(200) if p["symbol"] == symbol
                     and p["action"] in ("promote", "demote", "rollback")), None)
        if last is not None:
            lines.append(f"    why (#{last['id']} {last['action']}): {last['reason']}")
        else:
            lines.append("    why: no candidate has passed every gate yet (observation mode)")
    lines.append("")
    lines.append("This report describes past results only. It is not a forecast and does not guarantee profit.")
    txt = out_dir / f"{label}.txt"
    atomic_write_text(txt, "\n".join(lines) + "\n")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "mode", "symbol", "param_set_id", "side", "open_utc", "close_utc", "entry", "exit", "sl",
                     "tp", "volume", "risk_money", "pnl", "r", "ticket", "exit_reason"])
    for r in live + shadow:
        writer.writerow([r.id, r.mode, r.symbol, r.param_set_id, r.side, _fmt_ts(r.open_ts), _fmt_ts(r.close_ts),
                         r.entry, r.exit, r.sl, r.tp, r.volume, r.risk_money, r.pnl, r.r, r.ticket, r.exit_reason])
    atomic_write_text(out_dir / f"{label}_trades.csv", buf.getvalue())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["utc", "equity", "balance", "peak", "open_positions"])
    for p in points:
        writer.writerow([_fmt_ts(p["ts"]), p["equity"], p["balance"], p["peak"], p["open_positions"]])
    atomic_write_text(out_dir / f"{label}_equity.csv", buf.getvalue())
    return txt


def write_health(path: Path, payload: dict[str, Any], durable: bool = True) -> None:
    payload = dict(payload)
    payload["written_utc"] = _fmt_ts(time.time())
    atomic_write_json(path, payload, durable)


def format_health(payload: dict[str, Any]) -> str:
    """Human-readable --status output."""
    lines = [
        f"Health file written: {payload.get('written_utc', '?')}",
        f"Account: {payload.get('account', '?')}  ({payload.get('account_type', '?')})",
        f"State folder: {payload.get('state_dir', '?')}",
        f"Equity: {payload.get('equity', '?')}  peak: {payload.get('peak_equity', '?')}  "
        f"drawdown: {payload.get('drawdown_pct', '?')}%",
        f"Open bot positions: {payload.get('open_positions', '?')}",
        f"Halted: {payload.get('halted') or 'no'}",
        f"Last loop: {payload.get('last_loop_utc', '?')}   last tick: {payload.get('last_tick_server', '?')}",
        f"Mode: {'DRY RUN' if payload.get('dry_run') else 'LIVE ORDERS'}",
        "",
    ]
    for symbol, info in (payload.get("symbols") or {}).items():
        lines.append(f"{symbol}: champion {info.get('champion')}  market {info.get('market')}")
        lines.append(f"    {info.get('status_line', '')}")
        perf = info.get("champion_performance") or {}
        if perf:
            lines.append(f"    since promotion: live {perf['live_trades']} trades, expectancy "
                         f"{perf['live_expectancy_r']}R vs out-of-sample {perf['oos_expectancy_r']}R; "
                         f"shadow {perf['shadow_trades']} trades")
        for ch in info.get("challengers", []):
            lines.append(f"    challenger {ch['candidate']}: shadow {ch['shadow_trades']} trades / "
                         f"{ch['shadow_days']} days, mean {ch['shadow_expectancy_r']:+.3f}R, "
                         f"evidence {'ok' if ch['enough_evidence'] else 'collecting'}, "
                         f"consistent {'yes' if ch['consistent'] else 'no'}")
    opt = payload.get("optimizer") or {}
    if opt:
        lines.append("")
        lines.append(f"Optimizer: {json.dumps(opt, sort_keys=True)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ HTML report (v3)


def _esc(value: Any) -> str:
    import html

    return html.escape(str(value), quote=True)


def write_html_report(registry: Registry, office: Any, out_dir: Path, label: str, since: float, until: float,
                      account_desc: str, symbols: Sequence[str]) -> Path:
    """Self-contained HTML summary (PT-PT) for the Reporter: trades, decisions, team and advisor."""
    out_dir.mkdir(parents=True, exist_ok=True)
    live = [r for r in registry.trades(mode="live", status="closed") if since <= (r.close_ts or 0) < until]
    shadow = [r for r in registry.trades(mode="shadow", status="closed") if since <= (r.close_ts or 0) < until]
    stats, shadow_stats = trade_stats(live), trade_stats(shadow)
    decisions = office.dossiers(limit=5000, since_ts=since) if office is not None else []
    decisions = [d for d in decisions if d["ts"] < until]
    entered = sum(1 for d in decisions if d["final_action"] == "enter")
    ablation = office.latest_ablation() if office is not None else None
    verdict = (ablation or {}).get("payload", {}).get("verdict", "sem dados suficientes")
    spent = 0.0
    if office is not None:
        spent = sum(float(c["cost_usd"]) for c in office.advisor_calls(5000, since_ts=since) if c["ts"] < until)
    rows = "".join(
        f"<tr><td>{_esc(r.symbol)}</td><td>{_esc(r.side)}</td><td>{_fmt_ts(r.open_ts)}</td><td>{_fmt_ts(r.close_ts)}"
        f"</td><td>{r.r:+.2f}</td><td>{(r.pnl or 0):.2f}</td><td>{_esc(r.exit_reason or '')}</td></tr>"
        for r in live)
    champions = "".join(f"<li>{_esc(s)}: {_esc(registry.champion(s).describe())}</li>" for s in symbols)
    text = f"""<!doctype html>
<html lang="pt-PT"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CYBERBOX Trading Office - relatório {_esc(label)}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#14202b;background:#f6f8fa}}
h1{{font-size:22px}} h2{{font-size:17px;margin-top:28px}} table{{border-collapse:collapse;width:100%;background:#fff}}
td,th{{border:1px solid #d0d7de;padding:6px 8px;text-align:left;font-size:13px}} th{{background:#eaeef2}}
.kpi{{display:inline-block;margin:6px 16px 6px 0;padding:10px 14px;background:#fff;border:1px solid #d0d7de;border-radius:8px}}
.note{{font-size:12px;color:#57606a;margin-top:28px}}
</style></head><body>
<h1>CYBERBOX Trading Office: relatório {_esc(label)}</h1>
<p>Conta: {_esc(account_desc)} &middot; Período: {_fmt_ts(since)} a {_fmt_ts(until)}</p>
<h2>Trades reais fechados</h2>
<div class="kpi">Trades: {stats['trades']}</div><div class="kpi">Taxa de acerto: {stats['win_rate']:.1%}</div>
<div class="kpi">Expectativa: {stats['expectancy_r']:+.3f} R</div><div class="kpi">P&amp;L líquido: {stats['net_pnl']:.2f}</div>
<div class="kpi">Drawdown máx.: {stats['max_drawdown_r']:.2f} R</div>
<table><tr><th>Símbolo</th><th>Lado</th><th>Abertura</th><th>Fecho</th><th>R</th><th>P&amp;L</th><th>Motivo</th></tr>
{rows or '<tr><td colspan="7">Sem trades reais fechados no período.</td></tr>'}</table>
<h2>Trades sombra (validação)</h2>
<p>{shadow_stats['trades']} fechados, expectativa {shadow_stats['expectancy_r']:+.3f} R.</p>
<h2>Equipa de especialistas</h2>
<p>{len(decisions)} reuniões de decisão, {entered} entradas. Ablação: {_esc(verdict)}.</p>
<h2>Conselheiro Claude</h2>
<p>Custo estimado no período: {spent:.4f} USD.</p>
<h2>Campeões</h2><ul>{champions}</ul>
<p class="note">Este relatório descreve apenas resultados passados. Não é uma previsão e não garante lucro.
A equipa e o conselheiro só podem reduzir o tamanho base; o bot pode decidir não negociar.</p>
</body></html>
"""
    path = out_dir / f"{label}.html"
    atomic_write_text(path, text)
    return path
