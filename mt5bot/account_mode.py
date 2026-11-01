"""Which account the bot may trade: ``real`` (default), ``demo`` or ``auto``.

The account type always comes from ``account_info().trade_mode``, never from the server name.
Wrong type: the bot refuses with exit code 3 and says, in PT-PT, what to do. The panel can show
the mode but cannot change it: switching means restarting with the other launcher.

The first start on a real account, or one after a long absence, waits 10 seconds so a human
can still abort. The acknowledgment is per account (login + server) in the account's state
folder; unattended restarts of an acknowledged account never wait.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from .persistence import atomic_write_json, mask

log = logging.getLogger("mt5_bot")

ACK_FILE = "account_ack.json"

REAL_MONEY_HELP = (
    "Este terminal tem uma conta REAL iniciada, e a configuração não confirma o uso de dinheiro real. Defina "
    "\"real_money_confirmed\": true em config.json, ou arranque uma vez com:\n"
    "    python mt5_bot.py --confirm-real-money\n"
    "Leia primeiro a secção de riscos do README: podem existir perdas e não há lucro garantido."
)

REAL_EXPECTED = (
    "O modo pedido é REAL, mas o terminal tem uma conta DEMO iniciada. Nada foi negociado.\n"
    "Para a conta real: no MT5 abra Ficheiro > Iniciar sessão na conta de negociação, entre na conta real e corra "
    "start_bot.bat.\n"
    "Para testar em demo: corra start_bot_demo.bat, ou python mt5_bot.py --account demo."
)

DEMO_EXPECTED = (
    "O modo pedido é DEMO, mas o terminal tem uma conta REAL iniciada. Por segurança nada foi negociado.\n"
    "Para testar em demo: no MT5 abra Ficheiro > Iniciar sessão na conta de negociação e entre numa conta demo.\n"
    "Para usar a conta real: corra start_bot.bat."
)


def account_policy_problem(mode: str, is_real: bool, confirmed: bool) -> Optional[str]:
    """Reason the logged-in account may not be used, or None."""
    if mode == "demo" and is_real:
        return DEMO_EXPECTED
    if mode == "real" and not is_real:
        return REAL_EXPECTED
    if is_real and not confirmed:
        return REAL_MONEY_HELP
    return None


def needs_countdown(state_dir: Path, now: float, max_age_days: float) -> bool:
    """True on first use of this account, or after more than ``max_age_days`` without a start."""
    path = Path(state_dir) / ACK_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return now - float(data["last_seen"]) > max_age_days * 86400.0
    except (OSError, ValueError, KeyError, TypeError):
        return True


def acknowledge(state_dir: Path, login: Any, server: str, now: Optional[float] = None) -> None:
    """Record that this account was started. Holds the masked login only."""
    stamp = time.time() if now is None else now
    path = Path(state_dir) / ACK_FILE
    first = stamp
    try:
        first = float(json.loads(path.read_text(encoding="utf-8")).get("first_seen", stamp))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    atomic_write_json(path, {"account": mask(login), "server": server, "first_seen": first, "last_seen": stamp})


def money(value: Optional[float], currency: str) -> str:
    return "?" if value is None else f"{value:,.2f} {currency}".replace(",", " ")


def banner_data(acc: Any, is_real: bool, mode: str, hedging: bool, lim: Any, dry_run: bool = False) -> dict[str, Any]:
    """The startup banner as data, for the console, the log and the panel. No secrets: masked login."""
    equity = float(getattr(acc, "equity", 0.0) or 0.0)
    currency = str(getattr(acc, "currency", "") or "")

    def of(pct: float) -> float:
        return round(equity * pct / 100.0, 2)

    return {"account": mask(acc.login), "server": str(acc.server), "type": "REAL" if is_real else "DEMO",
            "account_mode": mode, "equity": round(equity, 2), "currency": currency,
            "leverage": getattr(acc, "leverage", None), "margin_mode": "hedging" if hedging else "netting",
            "profile": lim.profile, "dry_run": dry_run,
            "limits": {"risk_per_trade": of(lim.t3_risk_pct), "min_viable_cap": of(lim.min_viable_cap_pct),
                       "daily_loss": of(lim.daily_loss_pct), "weekly_soft": of(lim.weekly_loss_pct),
                       "drawdown_breaker": of(lim.drawdown_breaker_pct), "open_risk": of(lim.max_open_risk_pct)}}


def banner_lines(data: dict[str, Any]) -> list[str]:
    """Plain PT-PT lines of the banner."""
    cur, lim = data["currency"], data["limits"]
    lines = [
        "=" * 72,
        f"  CONTA {data['type']}  {data['account']} em {data['server']} (modo pedido: {data['account_mode']})",
        f"  capital {money(data['equity'], cur)}, alavancagem 1:{data['leverage'] or '?'}, conta {data['margin_mode']}",
        f"  perfil de risco {data['profile']}",
        f"  limites em dinheiro: por trade até {money(lim['risk_per_trade'], cur)}, lote mínimo até "
        f"{money(lim['min_viable_cap'], cur)}, risco aberto {money(lim['open_risk'], cur)}",
        f"  perda diária {money(lim['daily_loss'], cur)}, semanal {money(lim['weekly_soft'], cur)}, disjuntor "
        f"{money(lim['drawdown_breaker'], cur)}",
    ]
    if data["dry_run"]:
        lines.append("  ENSAIO: nenhuma ordem será enviada")
    if data["type"] == "REAL":
        lines.append("  DINHEIRO REAL EM RISCO. Pode perder. Nenhum lucro é garantido.")
    lines.append("=" * 72)
    return lines
