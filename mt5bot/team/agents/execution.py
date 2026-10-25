"""Execution: the Execution Trader and the Position Manager.

The Execution Trader reports what the bot's gateway did (it re-verifies spread and price
at send time with a fresh tick, attaches SL/TP, retries only requotes) and keeps realised
slippage statistics per symbol and session. It never changes the approved size.

The Position Manager proposes ONLY risk-reducing actions: break-even, a trailing stop
that only tightens, time stops, partial closes and pre-news exits. Every rule is off by
default (see ``PositionManagerConfig``); each action is re-validated by the risk layer
(``validate_sl_change``) before anything is sent.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

from ... import indicators
from ..agent import Agent, AgentState
from ..context import PositionView
from ..events import Event, OrderExecuted


class ExecutionTrader(Agent):
    id = "execution_trader"
    subscriptions = ("order.*",)

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._slippage: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))

    def observe(self, event: Event) -> None:
        if event.topic == OrderExecuted.TOPIC and event.symbol:
            session = event.payload.get("session") or "?"
            self._slippage[f"{event.symbol}|{session}"].append(float(event.payload.get("slippage_points", 0.0)))

    def slippage_report(self) -> dict[str, dict]:
        out = {}
        for key, values in list(self._slippage.items()):
            arr = np.asarray(values, dtype=float)
            symbol, session = key.split("|", 1)
            out[key] = {"symbol": symbol, "session": session, "n": int(arr.size),
                        "mean_points": round(float(arr.mean()), 3) if arr.size else None,
                        "p95_points": round(float(np.percentile(arr, 95)), 3) if arr.size else None}
        return out

    def executing(self, symbol: str, side: str, multiplier: float, correlation_id: str) -> None:
        self.set_state(AgentState.EXECUTING, task=f"{symbol}: a enviar {side}",
                       message=f"{symbol}: envio {side} com SL/TP, tamanho x{multiplier:.2f}; verifico spread e preço",
                       symbol=symbol, correlation_id=correlation_id)

    def done(self, symbol: str, ok: bool, detail: str, correlation_id: str) -> None:
        self.stats["slippage"] = self.slippage_report()
        self.set_state(AgentState.MONITORING if ok else AgentState.IDLE, task=f"{symbol}: {'executado' if ok else 'não enviado'}",
                       message=f"{symbol}: {detail}", symbol=symbol, correlation_id=correlation_id)


@dataclass(frozen=True)
class ManagementAction:
    ticket: int
    symbol: str
    action: str           # modify_sl | partial_close | close
    new_sl: float = 0.0
    fraction: float = 0.0
    reason: str = ""
    source: str = "rules"  # rules | advisor


def _improves(side: str, current_sl: float, new_sl: float) -> bool:
    """True when new_sl reduces risk (moves toward profit). Missing SL is the repair path, not ours."""
    if not current_sl:
        return False
    return new_sl > current_sl if side == "BUY" else new_sl < current_sl


class PositionManager(Agent):
    id = "position_manager"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._partial_done: set[int] = set()

    def plan(self, positions: Sequence[PositionView], bars_by_symbol: dict[str, np.ndarray], point: dict[str, float],
             minutes_to_news: dict[str, Optional[float]]) -> list[ManagementAction]:
        pm = self.cfg.team.position_manager
        actions: list[ManagementAction] = []
        if not pm.any_enabled:
            return actions
        for p in positions:
            bars = bars_by_symbol.get(p.symbol)
            pt = point.get(p.symbol, 0.0)
            news = minutes_to_news.get(p.symbol)
            if pm.pre_news_close_minutes is not None and news is not None and 0 <= news <= pm.pre_news_close_minutes:
                actions.append(ManagementAction(p.ticket, p.symbol, "close", reason=f"high-impact news in {news:.0f} min"))
                continue
            if pm.time_stop_bars is not None and p.bars_open >= pm.time_stop_bars:
                actions.append(ManagementAction(p.ticket, p.symbol, "close", reason=f"time stop after {p.bars_open} bars"))
                continue
            if pm.partial_close_at_r is not None and p.unrealized_r >= pm.partial_close_at_r \
                    and p.ticket not in self._partial_done:
                actions.append(ManagementAction(p.ticket, p.symbol, "partial_close", fraction=pm.partial_close_fraction,
                                                reason=f"{p.unrealized_r:.2f}R reached"))
            candidates: list[tuple[float, str]] = []
            if pm.breakeven_after_r is not None and p.unrealized_r >= pm.breakeven_after_r:
                be = p.entry + (pt if p.side == "BUY" else -pt)
                candidates.append((be, f"break-even after {p.unrealized_r:.2f}R"))
            if pm.trailing_atr_mult is not None and bars is not None and len(bars) > 20:
                atr = float(indicators.atr(bars["high"], bars["low"], bars["close"], 14)[-1])
                close = float(bars["close"][-1])
                if math.isfinite(atr) and atr > 0:
                    trail = close - pm.trailing_atr_mult * atr if p.side == "BUY" else close + pm.trailing_atr_mult * atr
                    candidates.append((trail, f"trailing {pm.trailing_atr_mult} ATR"))
            best: Optional[tuple[float, str]] = None
            for level, why in candidates:
                if _improves(p.side, p.sl, level) and (best is None or _improves(p.side, best[0], level)):
                    best = (level, why)
            if best is not None:
                actions.append(ManagementAction(p.ticket, p.symbol, "modify_sl", new_sl=best[0], reason=best[1]))
        if actions:
            self.set_state(AgentState.MONITORING, task=f"{len(actions)} acção(ões) de gestão",
                           message=f"{actions[0].symbol}: {actions[0].action} ({actions[0].reason})")
        return actions

    def from_advisor(self, position: PositionView, suggestion: Any) -> Optional[ManagementAction]:
        """Turn an advisor suggestion into a candidate action. Anything that adds risk is dropped."""
        if not getattr(suggestion, "available", False):
            return None
        action = suggestion.action
        if action == "tighten_stop" and _improves(position.side, position.sl, float(suggestion.new_stop_price)):
            return ManagementAction(position.ticket, position.symbol, "modify_sl", new_sl=float(suggestion.new_stop_price),
                                    reason="advisor: tighten stop", source="advisor")
        if action == "partial_close" and 0.0 < float(suggestion.close_fraction) < 1.0:
            return ManagementAction(position.ticket, position.symbol, "partial_close",
                                    fraction=float(suggestion.close_fraction), reason="advisor: partial close",
                                    source="advisor")
        if action == "close_now":
            return ManagementAction(position.ticket, position.symbol, "close", reason="advisor: close now",
                                    source="advisor")
        return None

    def mark_partial(self, ticket: int) -> None:
        self._partial_done.add(int(ticket))
