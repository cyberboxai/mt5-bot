"""Glue between the trading loop and the specialist team.

* ``decision_context`` turns MT5 data into the immutable, relative snapshot agents see.
* ``BotGateway`` is bound to ONE champion proposal. The CIO can only preview it or submit
  it once with a multiplier in [0, 1]; side, SL, TP and strategy are fixed by the champion,
  and the hard risk layer runs inside ``Bot._try_open`` exactly as in v2.
* ``guardian_snapshot`` gives the Guardian its independent view of the broker state.
* ``execute_management`` applies Position Manager / advisor actions only after the risk
  layer accepts them (tighten-only stops, partial closes that leave a valid volume).
"""

from __future__ import annotations

import logging
import math
import uuid
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from .market import DEFAULT_OFFSET_HOURS
from .risk import RiskViolation, validate_sl_change
from .team.agents.cio import ExecutionResult, GateResult
from .team.agents.control import GuardianSnapshot
from .team.agents.execution import ManagementAction
from .team.agents.market import session_view
from .team.context import AccountView, DecisionContext, MarketView, PositionView, Proposal, read_only
from .team.events import OrderExecuted, OrderFailed, PositionManaged

if TYPE_CHECKING:  # pragma: no cover
    from .bot import Bot
    from .engine import EntryIntent

log = logging.getLogger("mt5_bot")


# ------------------------------------------------------------------ snapshots


def position_views(bot: "Bot", acc: Any, positions: Optional[list[Any]] = None) -> tuple[PositionView, ...]:
    positions = bot._bot_positions() if positions is None else positions
    equity = float(acc.equity) or 1.0
    risks = {id(p): r.risk_money for p, r in zip(positions, bot._open_risks(positions, equity))}
    out = []
    for p in positions:
        row = bot.registry.trade_by_ticket(int(p.ticket)) if bot.registry is not None else None
        side = bot._side_of(p)
        tick = bot.mt5.symbol_info_tick(p.symbol)
        unrealized_r = 0.0
        if tick is not None and row is not None and row.risk_money > 0:
            order_type = bot.mt5.ORDER_TYPE_BUY if side == "BUY" else bot.mt5.ORDER_TYPE_SELL
            price = tick.bid if side == "BUY" else tick.ask
            profit = bot.mt5.order_calc_profit(order_type, p.symbol, p.volume, p.price_open, price)
            if profit is not None and math.isfinite(profit):
                unrealized_r = float(profit) / row.risk_money
        bars_open = 0
        last_bar = bot._last_bar.get(p.symbol)
        if row is not None and row.open_bar_time and last_bar:
            bars_open = max(0, int((last_bar - int(row.open_bar_time)) // bot.cfg.bar_seconds))
        spec = bot.specs.get(p.symbol)
        out.append(PositionView(p.symbol, side, float(p.volume), float(p.price_open), float(p.sl or 0.0),
                                float(p.tp or 0.0), int(p.ticket), risks.get(id(p), 0.0) / equity * 100.0,
                                unrealized_r, bars_open, spec.currency_base if spec else "",
                                spec.currency_profit if spec else "", row.param_set_id if row else None))
    return tuple(out)


def account_view(bot: "Bot", acc: Any, positions: tuple[PositionView, ...]) -> AccountView:
    equity = float(acc.equity)
    guard, risk = bot.guard, bot.risk
    start = guard.state.start_equity or equity
    daily_loss = max(0.0, guard.daily_loss_pct(equity))
    return AccountView(
        is_real=bot.is_real, risk_pct=risk.risk_pct,
        equity_change_today_pct=(equity / start - 1.0) * 100.0 if start > 0 else 0.0,
        daily_loss_used_pct_of_limit=daily_loss / risk.max_daily_loss_pct * 100.0 if risk.max_daily_loss_pct else 0.0,
        drawdown_pct=guard.drawdown_pct(equity), drawdown_limit_pct=risk.max_drawdown_pct,
        open_risk_pct=sum(p.risk_pct for p in positions), max_open_risk_pct=risk.max_total_open_risk_pct,
        halted=bot._entry_block_reason(), positions=positions)


def decision_context(bot: "Bot", symbol: str, intent: Optional["EntryIntent"], acc: Any, closed: np.ndarray,
                     purpose: str = "entry", positions: Optional[tuple[PositionView, ...]] = None) -> DecisionContext:
    info = bot.mt5.symbol_info(symbol)
    tick = bot.mt5.symbol_info_tick(symbol)
    if info is None or tick is None or info.point <= 0:
        raise RuntimeError(f"{symbol}: no symbol info or tick for the decision context")
    spec = bot.specs.get(symbol)
    offset = bot.clock.offset if bot.clock.offset is not None else DEFAULT_OFFSET_HOURS * 3600
    since = bot.clock.seconds_since_change(symbol)
    market = MarketView(
        symbol=symbol, timeframe=bot.cfg.timeframe, bar_seconds=bot.cfg.bar_seconds, bar_time=int(closed[-1]["time"]),
        closed=read_only(closed), bid=float(tick.bid), ask=float(tick.ask), point=float(info.point),
        digits=int(info.digits), spread_points=(float(tick.ask) - float(tick.bid)) / float(info.point),
        tick_time=int(tick.time), utc_now=float(bot._utc_now()), server_offset=int(offset),
        currency_base=spec.currency_base if spec else "", currency_profit=spec.currency_profit if spec else "",
        tick_age_seconds=float(since) if since is not None else 0.0)
    views = position_views(bot, acc) if positions is None else positions
    proposal = None
    if intent is not None:
        sig = intent.signal
        params: dict = {}
        oos = win = None
        if intent.param_set_id is not None and bot.registry is not None:
            try:
                params = bot.registry.param_set(int(intent.param_set_id))[1]
            except KeyError:
                params = {}
            champ = bot.registry.champion(symbol)
            if champ.param_set_id == intent.param_set_id:
                oos = champ.metrics.get("expectancy_r")
                win = champ.metrics.get("win_rate")
        proposal = Proposal(sig.side, intent.strategy, intent.param_set_id, intent.description, float(sig.atr),
                            float(sig.sl_mult or 2.0), float(sig.tp_mult or 3.0), oos, win, params)
    others = {s: arr for s, arr in bot._closes.items() if s != symbol}
    return DecisionContext(f"{symbol}-{int(closed[-1]['time'])}-{uuid.uuid4().hex[:6]}", market,
                           account_view(bot, acc, views), proposal, others, purpose=purpose)


# --------------------------------------------------------------------- gateway


class BotGateway:
    """One proposal, at most one submission, size only ever scaled down."""

    def __init__(self, bot: "Bot", symbol: str, intent: "EntryIntent", acc: Any) -> None:
        self.bot = bot
        self.symbol = symbol
        self.intent = intent
        self.acc = acc
        self.submitted = False

    def preview(self, multiplier: float) -> GateResult:
        out = self.bot._try_open(self.symbol, self.intent, self.acc, multiplier, preview=True)
        return GateResult(bool(out.risk_allowed), out.reason, out.volume, out.base_volume, out.risk_pct)

    def submit(self, multiplier: float, dossier_id: str) -> ExecutionResult:
        if self.submitted:
            return ExecutionResult(False, False, "this proposal was already submitted once")
        self.submitted = True
        out = self.bot._try_open(self.symbol, self.intent, self.acc, multiplier, dossier_id=dossier_id)
        team = self.bot.team
        if team is not None:
            if out.ok:
                tick_utc = float(self.bot._utc_now())
                sessions = session_view(tick_utc)["sessions"]
                payload = OrderExecuted(self.intent.signal.side, out.volume, out.price, out.sl, out.tp,
                                        out.slippage_points, out.dry_run, sessions[0] if sessions else "off")
                team.bus.publish(payload, agent="execution_trader", symbol=self.symbol, correlation_id=dossier_id,
                                 severity="notice")
            else:
                team.bus.publish(OrderFailed(out.reason), agent="execution_trader", symbol=self.symbol,
                                 correlation_id=dossier_id)
        return ExecutionResult(out.sent, out.ok, out.reason, out.volume, out.price, out.sl, out.tp,
                               out.slippage_points, out.ticket, out.dry_run, out.risk_allowed,
                               "ok" if out.risk_allowed else out.reason, out.base_volume)


# -------------------------------------------------------------------- guardian


def guardian_snapshot(bot: "Bot", positions: list[Any], acc: Any) -> GuardianSnapshot:
    equity = float(acc.equity)
    journal = {int(r.ticket) for r in bot.registry.trades(mode="live", status="open") if r.ticket is not None}
    open_risk = sum(r.risk_money for r in bot._open_risks(positions, equity))
    return GuardianSnapshot(
        positions=tuple({"ticket": int(p.ticket), "symbol": p.symbol, "sl": float(p.sl or 0.0),
                         "tp": float(p.tp or 0.0), "volume": float(p.volume)} for p in positions),
        journal_open_tickets=frozenset(journal), equity=equity, open_risk_money=open_risk,
        max_total_open_risk_pct=bot.risk.max_total_open_risk_pct, risk_pct=bot.risk.risk_pct,
        max_positions=bot.risk.max_positions, entries_while_halted=bot._entries_while_halted)


# ------------------------------------------------------------------ management


def _floor_step(volume: float, step: float) -> float:
    if step <= 0:
        return 0.0
    d = (Decimal(str(volume)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(step))
    return float(d)


def execute_management(bot: "Bot", action: ManagementAction) -> tuple[bool, str]:
    """Apply one risk-reducing action. Returns (applied, detail). Never widens risk."""
    positions = [p for p in bot._bot_positions(action.symbol) if int(p.ticket) == int(action.ticket)]
    if not positions:
        return False, "position no longer open"
    pos = positions[0]
    info = bot.mt5.symbol_info(pos.symbol)
    tick = bot.mt5.symbol_info_tick(pos.symbol)
    if info is None or tick is None:
        return False, "no quote"
    side = bot._side_of(pos)
    old_sl = float(pos.sl or 0.0)
    if action.action == "modify_sl":
        new_sl = round(float(action.new_sl), int(info.digits))
        try:
            validate_sl_change(side, float(pos.sl or 0.0), new_sl)
        except RiskViolation as exc:
            return False, f"rejected by the risk layer: {exc}"
        if not pos.sl or (side == "BUY" and new_sl <= pos.sl) or (side == "SELL" and new_sl >= pos.sl):
            return False, "not a tightening"
        gap = (int(getattr(info, "trade_stops_level", 0) or 0) + (tick.ask - tick.bid) / info.point + 1) * info.point
        current = tick.bid if side == "BUY" else tick.ask
        if (side == "BUY" and new_sl >= current - gap) or (side == "SELL" and new_sl <= current + gap):
            return False, "new stop too close to the market"
        if bot.cfg.dry_run:
            return True, f"DRY RUN: would move SL {pos.sl} -> {new_sl}"
        request = {"action": getattr(bot.mt5, "TRADE_ACTION_SLTP", 6), "symbol": pos.symbol, "position": pos.ticket,
                   "sl": new_sl, "tp": pos.tp, "magic": bot.cfg.magic, "comment": "mt5_bot"}
        result = bot.mt5.order_send(request)
        if result is None or result.retcode not in bot._success_codes:
            return False, f"SL modification failed ({getattr(result, 'retcode', 'none')})"
        row = bot.registry.trade_by_ticket(int(pos.ticket))
        if row is not None:
            bot.registry.update_trade_sl(row.id, new_sl, float(pos.tp))
        return True, f"SL {old_sl} -> {new_sl} ({action.reason})"
    if action.action == "partial_close":
        volume = _floor_step(float(pos.volume) * float(action.fraction), float(info.volume_step))
        remaining = float(pos.volume) - volume
        if volume < float(info.volume_min) or remaining < float(info.volume_min) - 1e-12:
            return False, "partial close would leave an invalid volume"
        ok = bot._close_position(pos, f"partial close: {action.reason}", volume=volume)
        if ok and bot.team is not None:
            pm = bot.team.agents.get("position_manager")
            if pm is not None:
                pm.mark_partial(pos.ticket)  # type: ignore[attr-defined]
        return ok, f"closed {volume} of {pos.volume} lots ({action.reason})"
    if action.action == "close":
        return bot._close_position(pos, action.reason), f"closed ({action.reason})"
    return False, f"unknown action {action.action!r}"


def manage_positions(bot: "Bot", symbol: str, acc: Any, closed: np.ndarray) -> None:
    pm = bot.cfg.team.position_manager
    if bot.team is None or not (pm.any_enabled or bot.cfg.claude.manage_positions):
        return
    views = [p for p in position_views(bot, acc) if p.symbol == symbol]
    if not views:
        return
    info = bot.mt5.symbol_info(symbol)
    news_agent = bot.team.agents.get("news_sentinel")
    minutes = None
    if news_agent is not None:
        upcoming = news_agent.upcoming(float(bot._utc_now()), set(), 6.0)  # type: ignore[attr-defined]
        future = [u["minutes"] for u in upcoming if u["minutes"] >= 0]
        minutes = min(future) if future else None

    def features(p: PositionView) -> dict:
        return {"symbol": p.symbol, "position_side": p.side, "unrealized_r": p.unrealized_r, "bars_open": p.bars_open,
                "timeframe": bot.cfg.timeframe, "purpose": "management"}

    actions = bot.team.plan_management(views, {symbol: closed}, {symbol: float(info.point) if info else 0.0},
                                       {symbol: minutes}, features)
    for action in actions:
        applied, detail = execute_management(bot, action)
        log.info("%s: position %s management %s (%s): %s", symbol, action.ticket, action.action, action.source,
                 detail)
        bot.team.bus.publish(PositionManaged(action.action, detail, applied), agent="position_manager", symbol=symbol)


# ----------------------------------------------------------- research menu


def strategy_menu() -> dict[str, dict[str, list[str]]]:
    """The vetted menu the advisor may choose hypotheses from: existing strategies and the
    exact grid values defined in code. Nothing outside it can enter the optimiser."""
    from . import strategies as strategy_lib

    return {name: {p.name: [str(v) for v in p.values] for p in strat.param_space}
            for name, strat in strategy_lib.library().items()}


def menu_lookup(strategy: str, param: str, value: str) -> Any:
    """Typed grid value for an advisor hypothesis, or None when it is not on the menu."""
    from . import strategies as strategy_lib

    try:
        strat = strategy_lib.get(strategy)
    except KeyError:
        return None
    spec = next((p for p in strat.param_space if p.name == param), None)
    if spec is None:
        return None
    for candidate in spec.values:
        if str(candidate) == str(value).strip():
            return candidate
    return None
