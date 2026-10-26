"""Daily and weekly plain-text and CSV reports, plus the health file."""

from __future__ import annotations

import csv
import io
import json
import time
from collections import Counter, defaultdict
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
    lines.append("LIVE TRADES BY TIER")
    for tier in ("T1", "T2", "T3", "EXT"):
        rows = [r for r in live if (r.tier or "EXT") == tier]
        if rows:
            t = trade_stats(rows)
            lines.append(f"  {tier}: {t['trades']} trades, expectancy {t['expectancy_r']:+.3f} R, P&L {t['net_pnl']:.2f}")
    lines.append("")
    lines.append("TIER CHANGES IN THE PERIOD")
    changes = [p for p in registry.promotions(500) if since <= p["ts"] < until
               and str(p["action"]).startswith(("tier_", "manual"))]
    for p in reversed(changes[:40]):
        lines.append(f"  #{p['id']} {_fmt_ts(p['ts'])} {p['symbol']}: {p['old_desc']} -> {p['new_desc']}")
        lines.append(f"      why: {p['reason'][:300]}")
    if not changes:
        lines.append("  none")
    lines.append("")
    lines.extend(self_review(registry, live, since, until))
    lines.append("This report describes past results only. It is not a forecast and does not guarantee profit.")
    txt = out_dir / f"{label}.txt"
    atomic_write_text(txt, "\n".join(lines) + "\n")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "mode", "symbol", "timeframe", "arm_id", "tier", "param_set_id", "side", "open_utc",
                     "close_utc", "entry", "exit", "sl", "tp", "volume", "risk_money", "risk_pct", "pnl", "r", "ticket",
                     "exit_reason"])
    for r in live + shadow:
        writer.writerow([r.id, r.mode, r.symbol, r.timeframe, r.arm_id, r.tier, r.param_set_id, r.side,
                         _fmt_ts(r.open_ts), _fmt_ts(r.close_ts), r.entry, r.exit, r.sl, r.tp, r.volume, r.risk_money,
                         r.risk_pct, r.pnl, r.r, r.ticket, r.exit_reason])
    atomic_write_text(out_dir / f"{label}_trades.csv", buf.getvalue())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["utc", "equity", "balance", "peak", "open_positions"])
    for p in points:
        writer.writerow([_fmt_ts(p["ts"]), p["equity"], p["balance"], p["peak"], p["open_positions"]])
    atomic_write_text(out_dir / f"{label}_equity.csv", buf.getvalue())
    return txt


def self_review(registry: Registry, live: Sequence[TradeRow], since: float, until: float) -> list[str]:
    """The period's self-review: where decisions stopped, what filters were worth, costs,
    regimes, management policies and cash flows (which never count as performance)."""
    from . import funnel

    conn = registry.conn
    lines = ["SELF-REVIEW"]
    rep = funnel.report(conn, since, until)
    if rep.get("available"):
        cf = funnel.counterfactuals(conn, since, until)
        lines.append(f"  decision funnel: {rep['bars_evaluated']} bars evaluated, {rep['signals']} signals, "
                     f"{rep['orders']} orders, {rep['blocked']} blocked")
        for b in rep["top_blockers"][:5]:
            virtual = cf.get(b["reason"]) or {}
            value = (f"; blocked signals' shadow outcome {virtual['virtual_mean_r']:+.2f} R over {int(virtual['n'])}"
                     if virtual.get("n") else "")
            lines.append(f"    {b['reason']}: {b['count']} ({b['share']:.0%}) {b['description']}{value}")
    else:
        lines.append("  decision funnel: no data for the period")
    slips = [float((r.extra or {}).get("slippage_points") or 0.0) for r in live]
    exits = Counter(r.exit_reason or "?" for r in live)
    if live:
        lines.append(f"  execution: mean slippage {sum(slips) / len(slips):+.1f} points over {len(live)} fills; "
                     "exits " + ", ".join(f"{k} {v}" for k, v in exits.most_common()))
    by_regime: dict[str, list[TradeRow]] = defaultdict(list)
    for r in live:
        by_regime[str((r.extra or {}).get("regime") or "untagged")].append(r)
    for regime_name, rows in sorted(by_regime.items()):
        t = trade_stats(rows)
        lines.append(f"  regime {regime_name}: {t['trades']} trades, expectancy {t['expectancy_r']:+.3f} R")
    policies = Counter(row[0] for row in conn.execute("SELECT policy FROM arms WHERE active = 1"))
    if policies:
        lines.append("  management policies (active arms): " + ", ".join(f"{k} {v}" for k, v in policies.most_common()))
    for p in registry.promotions(500):
        if p["action"] == "policy" and since <= p["ts"] < until:
            lines.append(f"    {_fmt_ts(p['ts'])} {p['symbol']}: {p['old_desc']} -> {p['new_desc']} ({p['reason'][:160]})")
    flows = list(conn.execute("SELECT amount FROM cash_flows WHERE ts >= ? AND ts < ?", (since, until)))
    if flows:
        deposits = sum(float(r[0]) for r in flows if float(r[0]) > 0)
        withdrawals = -sum(float(r[0]) for r in flows if float(r[0]) < 0)
        lines.append(f"  cash flows: deposits {deposits:.2f}, withdrawals {withdrawals:.2f} "
                     "(excluded from performance and from the breakers)")
    lines.append("")
    return lines


def write_health(path: Path, payload: dict[str, Any], durable: bool = True) -> None:
    payload = dict(payload)
    payload["written_utc"] = _fmt_ts(time.time())
    atomic_write_json(path, payload, durable)


def format_health(payload: dict[str, Any]) -> str:
    """Human-readable --status output."""
    cap = payload.get("capital") or {}
    expl = payload.get("exploration") or {}
    lim = payload.get("limits") or {}
    lines = [
        f"Health file written: {payload.get('written_utc', '?')}",
        f"Account: {payload.get('account', '?')}  ({payload.get('account_type', '?')})  version {payload.get('version')}",
        f"State folder: {payload.get('state_dir', '?')}",
        f"Equity: {payload.get('equity', '?')}  peak: {payload.get('peak_equity', '?')}  "
        f"drawdown: {payload.get('drawdown_pct', '?')}%  today: -{payload.get('daily_loss_pct', 0)}%",
        f"Profit floor: {cap.get('floor')}  cushion: {cap.get('cushion')} (factor {cap.get('cushion_factor')})  "
        f"locked profit: {cap.get('locked_profit')}",
        f"Risk profile: {lim.get('profile')}  T1 {lim.get('t1_risk_pct')}% / T2 {lim.get('t2_risk_pct')}% / "
        f"T3 {lim.get('t3_risk_pct')}%  daily {lim.get('daily_loss_pct')}%  breaker {lim.get('drawdown_breaker_pct')}%",
        f"Exploration budget: day {expl.get('day_used', 0)}/{expl.get('day_budget', 0)}, week "
        f"{expl.get('week_used', 0)}/{expl.get('week_budget', 0)}, open {expl.get('open_risk', 0)}, room "
        f"{expl.get('room', 0)}",
        f"Arms by tier: {payload.get('tiers', {})}   mode: {payload.get('mode', '?')}",
        f"Open bot positions: {payload.get('open_positions', '?')}",
        f"Halted / blocked: {payload.get('entry_block') or payload.get('halted') or 'no'}",
        f"Trading hours (server): {payload.get('trading_hours_server')}",
        f"Last loop: {payload.get('last_loop_utc', '?')}   last tick: {payload.get('last_tick_server', '?')}",
        f"Mode: {'DRY RUN' if payload.get('dry_run') else 'LIVE ORDERS'}",
        "",
        f"Status: {payload.get('status_line', '')}",
        "",
    ]
    for symbol, info in (payload.get("symbols") or {}).items():
        lines.append(f"{symbol}: market {info.get('market')}")
        for arm in info.get("arms", [])[:4]:
            post = arm.get("posterior") or {}
            p = post.get("p_positive")
            lines.append(f"    {arm.get('tier')} #{arm.get('id')} {arm.get('description')}  "
                         f"P(E>0)={'?' if p is None else f'{p:.2f}'}  trades {post.get('n_obs', 0)}")
    research = payload.get("research") or {}
    if research:
        lines.append("")
        lines.append(f"Research: {json.dumps(research, sort_keys=True)}")
    flow = payload.get("flow") or {}
    if flow.get("active"):
        lines.append(f"Flow Doctor adjustments: {len(flow['active'])} active, knobs {flow.get('knobs')}")
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
    tiers = "".join(f"<li>{_esc(r['symbol'])} {_esc(r['timeframe'])} {_esc(r['strategy'])}: {_esc(r['tier'])}</li>"
                    for r in registry.conn.execute("SELECT symbol, timeframe, strategy, tier FROM arms WHERE active = 1"
                                                   " AND tier != 'T0' ORDER BY tier DESC, symbol LIMIT 40"))
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
<h2>Braços com evidência (T1 a T3)</h2><ul>{tiers or '<li>Nenhum: só exploração com micro-risco.</li>'}</ul>
<p class="note">Este relatório descreve apenas resultados passados. Não é uma previsão e não garante lucro.
A equipa e o conselheiro só podem reduzir o tamanho calculado; o bot pode decidir não negociar.</p>
</body></html>
"""
    path = out_dir / f"{label}.html"
    atomic_write_text(path, text)
    return path
