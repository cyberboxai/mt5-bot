"""``--preflight``: a read-only check of what would happen if the bot started now.

It connects, reads and reports in PT-PT. It never sends or checks an order, never opens the
account database and never starts research, so it is safe at any moment, even beside a running
bot. It reuses the bot's own feasibility code, so what it says is what the bot will do.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Optional

from .account_mode import account_policy_problem, banner_data, banner_lines, money
from .risk import RiskManager

log = logging.getLogger("mt5_bot")

EXIT_OK, EXIT_STARTUP, EXIT_SAFETY_STOP = 0, 2, 3
DEADLINE_SECONDS = 50.0


def _problem(text: list[str], line: str) -> None:
    text.append(f"  PROBLEMA: {line}")


def run_preflight(bot: Any, clock: Callable[[], float] = time.monotonic) -> tuple[int, str]:
    """(exit code, PT-PT report). ``bot`` is an unstarted ``Bot``: only its read paths are used."""
    started = clock()
    out = ["CYBERBOX: verificação prévia (apenas leitura; nenhuma ordem é enviada)", ""]
    if not bot._initialize():
        out.append(f"Sem ligação ao MT5: {bot._last_error()}. O terminal está instalado e com sessão iniciada?")
        return EXIT_STARTUP, "\n".join(out)
    term = bot.broker.terminal()
    acc = bot.broker.account()
    if term is None or acc is None:
        out.append(f"O terminal não devolveu dados da conta: {bot._last_error()}. Inicie sessão numa conta.")
        return EXIT_STARTUP, "\n".join(out)
    bot.is_real = not bot._is_demo(acc)
    problem = account_policy_problem(bot.cfg.account_mode, bot.is_real, bot.real_money_confirmed)
    if problem:
        out.append(problem)
        return EXIT_SAFETY_STOP, "\n".join(out)
    from .core import broker as br

    bot.hedging = getattr(acc, "margin_mode", br.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) \
        == br.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    bot.risk = RiskManager(bot.cfg, bot.is_real)
    lim = bot.risk.limits
    out.extend(banner_lines(banner_data(acc, bot.is_real, bot.cfg.account_mode, bot.hedging, lim, bot.cfg.dry_run)))
    out.extend(["", "Permissões:"])
    problems = 0
    checks = ((getattr(term, "connected", False), "terminal ligado à corretora",
               "o terminal não está ligado à corretora"),
              (getattr(term, "trade_allowed", False), "negociação algorítmica activa no terminal",
               "negociação algorítmica desligada: active o botão Algo Trading"),
              (getattr(acc, "trade_allowed", False), "conta autoriza negociação",
               "a conta não autoriza negociação (palavra-passe de investidor?)"),
              (getattr(acc, "trade_expert", False), "corretora autoriza robôs",
               "a corretora não autoriza robôs nesta conta"))
    for ok, good, bad in checks:
        if ok:
            out.append(f"  ok: {good}")
        else:
            problems += 1
            _problem(out, bad)
    if not math.isfinite(float(acc.equity)) or float(acc.equity) <= 0:
        problems += 1
        _problem(out, "capital zero, negativo ou desconhecido")

    out.extend(["", "Símbolos:"])
    from .bot import resolve_symbol

    for wanted in bot.cfg.symbols:
        name = resolve_symbol(bot.market, wanted)
        if name is None or not bot.market.select_symbol(name) or bot.market.symbol_info(name) is None:
            problems += 1
            _problem(out, f"{wanted} não existe nesta corretora; abra a Observação de Mercado e copie o nome exacto")
            continue
        bot.symbols[wanted] = name
        bot._refresh_spec(name)
    bot.universe = list(bot.symbols.values())
    bot._select_universe(acc)
    for symbol in bot.universe:
        spec = bot.specs.get(symbol)
        tick = bot.market.tick(symbol)
        if tick is not None:
            bot.clock.observe(symbol, int(tick.time), float(tick.bid), float(tick.ask))
        note = "" if symbol in bot.cfg.universe.candidates or symbol in bot.cfg.symbols else " (com sufixo da corretora)"
        out.append(f"  {symbol}{note}: lote mínimo {spec.volume_min:g}, passo {spec.volume_step:g}, máximo "
                   f"{spec.volume_max:g}, comissão {bot.commission.get(symbol, 0.0):.2f} por lote")
    offset: Optional[int] = bot.clock.offset
    out.append("")
    out.append("Hora do servidor: " + ("sem ticks recentes, desvio ainda desconhecido" if offset is None
                                       else f"UTC{offset / 3600:+.1f} h"))
    report = bot._scan_feasibility(acc)
    timed_out = clock() - started > DEADLINE_SECONDS
    currency = report["currency"]
    out.extend(["", "O que pode ser negociado agora, com este saldo:"])
    feasible = [r for r in report["rows"] if r["feasible"]]
    by_key: dict[tuple[str, str], list[dict]] = {}
    for row in report["rows"]:
        by_key.setdefault((row["symbol"], row["timeframe"]), []).append(row)
    for (symbol, tf), rows in sorted(by_key.items()):
        good = [r for r in rows if r["feasible"]]
        if good:
            best = min(good, key=lambda r: r["min_lot_risk"])
            out.append(f"  {symbol} {tf}: sim, {', '.join(r['stop_policy'] for r in good)}; risco desde "
                       f"{money(best['min_lot_risk'], currency)} ({best['min_lot_risk_pct']:.2f} %)")
        else:
            cheap = min(rows, key=lambda r: r["floor_equity"] if r["floor_equity"] is not None else math.inf)
            out.append(f"  {symbol} {tf}: não ({cheap['reason_pt']}); piso {money(cheap['floor_equity'], currency)}")
    out.append("")
    out.append(f"Piso de viabilidade: {money(report['floor'], currency)}")
    out.append(f"Tecto de capacidade: {money(report['ceiling'], currency)} (estimativa)")
    out.append(f"Modo de operação: {report['mode_pt']} ({report['label']})")
    if report["message"]:
        out.append(report["message"])
    if report.get("undeployed_text"):
        out.append(report["undeployed_text"])
    out.extend(["", "Primeiros candidatos prováveis (menor custo em R; só entram se um braço passar as portas):"])
    for row in sorted(feasible, key=lambda r: r["cost_r"] if r["cost_r"] is not None else math.inf)[:5]:
        cost = f"{row['cost_r']:.2f}R" if row["cost_r"] is not None else "?"
        out.append(f"  {row['symbol']} {row['timeframe']} {row['stop_policy']}: custo {cost}, risco "
                   f"{money(row['min_lot_risk'], currency)}")
    if not feasible:
        out.append("  nenhum com este saldo")
        problems += 1
    if timed_out:
        out.append("Aviso: a verificação demorou mais do que o previsto.")
    out.extend(["", "Pronto a arrancar." if not problems else f"{problems} problema(s) impedem ou limitam a negociação."])
    return EXIT_OK, "\n".join(out)

