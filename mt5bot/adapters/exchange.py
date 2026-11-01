"""``SimExchange``: one matching engine behind ``SimBroker``, ``ShadowBroker`` and ``FakeBroker``.

It implements ``BrokerPort`` with the behaviour that decides real outcomes: spread from the
quote, slippage and latency from a (calibrated) cost model, stop-level constraints, partial
fills, rejections, swap at rollover, commission, gaps through stops, margin and stop-out.

``chaos`` scripts the failures a live terminal can produce, including the worst one: the broker
filled the order and the answer never arrived.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence

import numpy as np

from ..core import broker as b
from ..core.broker import BrokerReply, OrderRequest

COMMISSION_PER_LOT_SIDE = 3.5
RC_DONE, RC_REJECT, RC_REQUOTE, RC_TIMEOUT = 10009, 10006, 10004, 10012
RC_PARTIAL, RC_INVALID_STOPS, RC_NO_MONEY, RC_PLACED, RC_POSITION_CLOSED = 10010, 10016, 10019, 10008, 10036


@dataclass(frozen=True)
class SimCosts:
    """Friction of the simulated broker. ``lookup`` plugs in the model calibrated from own fills:
    (symbol, server_hour) -> (slippage_points, latency_ms, reject_rate) or None."""

    slippage_points: float = 0.5
    latency_ms: float = 80.0
    commission_per_lot_side: float = COMMISSION_PER_LOT_SIDE
    reject_rate: float = 0.0
    requote_rate: float = 0.0
    partial_rate: float = 0.0
    partial_fraction: float = 0.5
    stop_level_points: int = 0
    lookup: Optional[Callable[[str, int], Optional[tuple[float, float, float]]]] = None


class SimExchange:
    """Positions, deals and account. Subclasses provide ``quote``, ``server_time`` and specs."""

    def __init__(self, costs: SimCosts = SimCosts(), balance: float = 10_000.0, seed: int = 1, leverage: int = 100,
                 login: int = 10_000_001, server: str = "CYBERBOX-Simulation", hedging: bool = True) -> None:
        self.costs = costs
        self.rng = np.random.default_rng(seed)
        self.positions_book: list[SimpleNamespace] = []
        self.deals_book: list[SimpleNamespace] = []
        self.orders_book: list[SimpleNamespace] = []
        self.sent: list[OrderRequest] = []
        self.checks: list[OrderRequest] = []
        self.chaos: deque[Any] = deque()
        self._late: list[list[Any]] = []
        self._blind_reads = 0
        self._ticket = 500_000
        self.account_state = SimpleNamespace(
            login=login, server=server, currency="USD", trade_mode=b.ACCOUNT_TRADE_MODE_DEMO,
            margin_mode=b.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING if hedging else b.ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
            leverage=leverage, trade_allowed=True, trade_expert=True, equity=balance, balance=balance, margin=0.0,
            margin_free=balance, margin_level=0.0, margin_so_call=100.0, margin_so_so=50.0, margin_so_mode=0)
        self.terminal_state = SimpleNamespace(connected=True, trade_allowed=True, path=r"C:\CYBERBOX\Simulation")

    # ---- provided by subclasses

    def quote(self, symbol: str) -> Any:
        raise NotImplementedError

    def server_time(self) -> int:
        raise NotImplementedError

    def contract(self, symbol: str) -> float:
        return 100_000.0

    def point(self, symbol: str) -> float:
        return 0.00001

    def digits(self, symbol: str) -> int:
        return 5

    def swap_points(self, symbol: str, is_buy: bool) -> float:
        return -0.5 if is_buy else -0.3

    def swap_triple_weekday(self) -> int:
        return 3

    # ---- money

    def value_per_price(self, symbol: str, price: Optional[float] = None) -> float:
        contract = self.contract(symbol)
        if symbol[:6].endswith("USD"):
            return contract
        quote = price if price else float(self.quote(symbol).bid)
        return contract / max(quote, 1e-9)

    def profit(self, symbol: str, is_buy: bool, volume: float, open_price: float, close_price: float) -> float:
        direction = 1.0 if is_buy else -1.0
        return (close_price - open_price) * direction * volume * self.value_per_price(symbol, close_price)

    def margin_for(self, symbol: str, volume: float, price: float) -> float:
        notional = volume * self.contract(symbol)
        if symbol[:6].endswith("USD"):
            notional *= price
        return notional / self.account_state.leverage

    def mark_to_market(self) -> None:
        acc = self.account_state
        floating = margin = 0.0
        for p in self.positions_book:
            quote = self.quote(p.symbol)
            is_buy = p.type == b.POSITION_TYPE_BUY
            floating += self.profit(p.symbol, is_buy, p.volume, p.price_open, quote.bid if is_buy else quote.ask)
            floating += p.swap
            margin += self.margin_for(p.symbol, p.volume, p.price_open)
        acc.equity = round(acc.balance + floating, 2)
        acc.margin = round(margin, 2)
        acc.margin_free = round(acc.equity - margin, 2)
        acc.margin_level = round(acc.equity / margin * 100.0, 2) if margin > 0 else 0.0

    def enforce_stop_out(self) -> int:
        """Broker liquidation: close the worst position while margin level is below stop-out."""
        closed = 0
        self.mark_to_market()
        acc = self.account_state
        while self.positions_book and acc.margin > 0 and acc.margin_level < acc.margin_so_so:
            worst = min(self.positions_book, key=self._floating)
            quote = self.quote(worst.symbol)
            self._close(worst, quote.bid if worst.type == b.POSITION_TYPE_BUY else quote.ask, comment="[so]")
            self.mark_to_market()
            closed += 1
        return closed

    def _floating(self, p: SimpleNamespace) -> float:
        quote = self.quote(p.symbol)
        is_buy = p.type == b.POSITION_TYPE_BUY
        return self.profit(p.symbol, is_buy, p.volume, p.price_open, quote.bid if is_buy else quote.ask)

    def charge_rollover(self, nights: int = 1) -> None:
        """Swap accrues on the position, like MT5, and is realised when it closes."""
        for p in self.positions_book:
            is_buy = p.type == b.POSITION_TYPE_BUY
            points = self.swap_points(p.symbol, is_buy) * nights
            p.swap = round(p.swap + points * self.point(p.symbol) * self.value_per_price(p.symbol) * p.volume, 2)

    # ---- fills

    def _costs_now(self, symbol: str) -> tuple[float, float, float]:
        if self.costs.lookup is not None:
            found = self.costs.lookup(symbol, (self.server_time() % 86400) // 3600)
            if found is not None:
                return found
        return self.costs.slippage_points, self.costs.latency_ms, self.costs.reject_rate

    def slippage(self, symbol: str) -> float:
        points = self._costs_now(symbol)[0]
        return round(abs(float(self.rng.normal(0, points)))) * self.point(symbol) if points > 0 else 0.0

    def _deal(self, p: SimpleNamespace, entry: int, price: float, profit: float, volume: float, swap: float = 0.0,
              comment: str = "", order: int = 0) -> None:
        self._ticket += 1
        self.deals_book.append(SimpleNamespace(
            ticket=self._ticket, order=order or self._ticket, position_id=p.ticket, symbol=p.symbol, entry=entry,
            price=price, profit=profit, commission=-self.costs.commission_per_lot_side * volume, swap=swap, fee=0.0,
            volume=volume, type=p.type if entry == b.DEAL_ENTRY_IN else 1 - p.type, magic=p.magic,
            time=self.server_time(), comment=comment))

    def _close(self, p: SimpleNamespace, price: float, volume: Optional[float] = None, comment: str = "") -> None:
        volume = p.volume if volume is None else volume
        share = min(volume / p.volume, 1.0)
        profit = round(self.profit(p.symbol, p.type == b.POSITION_TYPE_BUY, volume, p.price_open, price), 2)
        swap = round(p.swap * share, 2)
        self._deal(p, b.DEAL_ENTRY_OUT, price, profit, volume, swap, comment or p.comment)
        acc = self.account_state
        acc.balance = round(acc.balance + profit + swap - self.costs.commission_per_lot_side * volume, 2)
        if volume >= p.volume - 1e-9:
            self.positions_book.remove(p)
        else:
            p.volume, p.swap = round(p.volume - volume, 2), round(p.swap - swap, 2)

    def _fill_open(self, request: OrderRequest, volume: float, attach_stops: bool = True) -> SimpleNamespace:
        quote = self.quote(request.symbol)
        is_buy = request.side == "BUY"
        slip = self.slippage(request.symbol)
        price = round((quote.ask + slip) if is_buy else (quote.bid - slip), self.digits(request.symbol))
        self._ticket += 1
        pos = SimpleNamespace(
            ticket=self._ticket, symbol=request.symbol, type=b.POSITION_TYPE_BUY if is_buy else b.POSITION_TYPE_SELL,
            volume=float(volume), magic=request.magic, price_open=price, sl=float(request.sl) if attach_stops else 0.0,
            tp=float(request.tp) if attach_stops else 0.0, time=self.server_time(), comment=request.comment, swap=0.0)
        self.positions_book.append(pos)
        self._deal(pos, b.DEAL_ENTRY_IN, price, 0.0, volume, comment=request.comment, order=pos.ticket)
        acc = self.account_state
        acc.balance = round(acc.balance - self.costs.commission_per_lot_side * volume, 2)
        self.mark_to_market()
        return pos

    def fill_stops(self, symbol: str, bar_open: float, high: float, low: float, spread: float) -> None:
        """Server-side SL/TP on one bar: stop first when both are touched, gaps fill at the open."""
        for p in [q for q in self.positions_book if q.symbol == symbol]:
            buy = p.type == b.POSITION_TYPE_BUY
            shift = 0.0 if buy else spread   # SELL positions trigger on the ask
            o, h, lo = bar_open + shift, high + shift, low + shift
            slip = self.slippage(symbol)
            if p.sl and ((o <= p.sl) if buy else (o >= p.sl)):
                self._close(p, round(o - slip if buy else o + slip, self.digits(symbol)), comment=f"[sl {p.sl}]")
            elif p.tp and ((o >= p.tp) if buy else (o <= p.tp)):
                self._close(p, float(o), comment=f"[tp {p.tp}]")   # gaps through a TP fill at the better price
            elif p.sl and ((lo <= p.sl) if buy else (h >= p.sl)):
                self._close(p, round(p.sl - slip if buy else p.sl + slip, self.digits(symbol)), comment=f"[sl {p.sl}]")
            elif p.tp and ((h >= p.tp) if buy else (lo <= p.tp)):
                self._close(p, float(p.tp), comment=f"[tp {p.tp}]")

    # ---- chaos

    def _settle_late(self) -> None:
        for item in list(self._late):
            item[0] -= 1
            if item[0] <= 0:
                self._late.remove(item)
                self._fill_open(item[1], item[1].volume)

    # ---- BrokerPort

    def connect(self, **kwargs: Any) -> bool:
        return True

    def disconnect(self) -> None:
        return None

    def last_error(self) -> Any:
        return (1, "Success")

    def terminal(self) -> Any:
        return self.terminal_state

    def account(self) -> Any:
        return self.account_state

    def positions(self, symbol: Optional[str] = None) -> Optional[Sequence[Any]]:
        self._settle_late()
        if self._blind_reads > 0:
            self._blind_reads -= 1
            return None
        return tuple(p for p in self.positions_book if symbol is None or p.symbol == symbol)

    def pending_orders(self) -> Optional[Sequence[Any]]:
        return tuple(self.orders_book)

    def deals_between(self, date_from: float, date_to: float) -> Optional[Sequence[Any]]:
        return tuple(self.deals_book)

    def deals_for_position(self, position: int) -> Optional[Sequence[Any]]:
        return tuple(d for d in self.deals_book if d.position_id == position)

    def history_orders_between(self, date_from: float, date_to: float) -> Optional[Sequence[Any]]:
        return ()

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float,
                    price_close: float) -> Optional[float]:
        return self.profit(symbol, side == "BUY", volume, price_open, price_close)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> Optional[float]:
        return self.margin_for(symbol, volume, price)

    def check(self, request: OrderRequest) -> Optional[BrokerReply]:
        self.checks.append(request)
        return BrokerReply(0, comment="Done")

    def send(self, request: OrderRequest) -> BrokerReply:
        self.sent.append(request)
        if request.kind == "sltp":
            return self._send_stops(request)
        if request.kind == "remove":
            self.orders_book = [o for o in self.orders_book if o.ticket != request.order]
            return BrokerReply(RC_DONE, comment="removed")
        if request.kind == "close":
            return self._send_close(request)
        return self._send_open(request)

    def _send_stops(self, request: OrderRequest) -> BrokerReply:
        script = self.chaos.popleft() if self.chaos else None
        if script == "reject_stops":
            return BrokerReply(RC_INVALID_STOPS, comment="invalid stops")
        for p in self.positions_book:
            if p.ticket == request.position:
                p.sl, p.tp = request.sl, request.tp
                return BrokerReply(RC_DONE, comment="sltp")
        return BrokerReply(RC_POSITION_CLOSED, comment="no position")

    def _send_close(self, request: OrderRequest) -> BrokerReply:
        script = self.chaos.popleft() if self.chaos else None
        if script in ("timeout_no_fill", "none_no_fill"):
            return BrokerReply(RC_TIMEOUT if script == "timeout_no_fill" else None, error=str(script))
        quote = self.quote(request.symbol)
        slip = self.slippage(request.symbol)
        is_buy = request.side == "BUY"
        price = round((quote.ask + slip) if is_buy else (quote.bid - slip), self.digits(request.symbol))
        target = next((p for p in self.positions_book if p.ticket == request.position), None)
        if target is None and request.position is None:     # netting: the opposite deal closes it
            target = next((p for p in self.positions_book if p.symbol == request.symbol), None)
        if target is None:
            return BrokerReply(RC_POSITION_CLOSED, comment="no position")
        self._close(target, price, float(request.volume))
        self.mark_to_market()
        if script in ("timeout_after_fill", "none_after_fill"):
            return BrokerReply(RC_TIMEOUT if script == "timeout_after_fill" else None, error=str(script))
        return BrokerReply(RC_DONE, self._ticket, self._ticket, request.volume, price, "closed")

    def _send_open(self, request: OrderRequest) -> BrokerReply:
        script = self.chaos.popleft() if self.chaos else None
        if isinstance(script, tuple):
            name, arg = script
            if name == "reject":
                return BrokerReply(int(arg), comment=f"chaos reject {arg}")
            if name == "late_fill":      # the reply is a timeout; the fill appears after ``arg`` reads
                self._late.append([int(arg), request])
                return BrokerReply(RC_TIMEOUT, comment="timeout")
            if name == "partial":
                pos = self._fill_open(request, round(request.volume * float(arg), 2))
                return BrokerReply(RC_PARTIAL, pos.ticket, self._ticket, pos.volume, pos.price_open, "partial")
            if name == "blind":          # the terminal cannot answer queries for ``arg`` reads
                self._blind_reads = int(arg)
                return BrokerReply(None, error="connection lost")
        if script == "timeout_no_fill":
            return BrokerReply(RC_TIMEOUT, comment="timeout")
        if script == "none_no_fill":
            return BrokerReply(None, error="no answer")
        if script in ("timeout_after_fill", "none_after_fill", "exception_after_fill"):
            self._fill_open(request, request.volume)
            if script == "timeout_after_fill":
                return BrokerReply(RC_TIMEOUT, comment="timeout")
            return BrokerReply(None, error=f"{script}: the request reached the server")
        if script == "ignore_stops":      # market execution: the broker fills and drops SL/TP
            pos = self._fill_open(request, request.volume, attach_stops=False)
            return BrokerReply(RC_DONE, pos.ticket, self._ticket, pos.volume, pos.price_open, "done")
        if script == "placed":
            pos = self._fill_open(request, request.volume)
            return BrokerReply(RC_PLACED, pos.ticket, 0, 0.0, 0.0, "placed")
        limit = self.costs.stop_level_points * self.point(request.symbol)
        if limit > 0 and (abs(request.price - request.sl) < limit or abs(request.price - request.tp) < limit):
            return BrokerReply(RC_INVALID_STOPS, comment="stops too close")
        reject_rate = self._costs_now(request.symbol)[2]
        roll = float(self.rng.random())
        if roll < reject_rate:
            return BrokerReply(RC_REJECT, comment="rejected")
        if roll < reject_rate + self.costs.requote_rate:
            return BrokerReply(RC_REQUOTE, comment="requote")
        needed = self.margin_for(request.symbol, request.volume, request.price)
        self.mark_to_market()
        if needed > self.account_state.margin_free:
            return BrokerReply(RC_NO_MONEY, comment="no money")
        if self.costs.partial_rate and float(self.rng.random()) < self.costs.partial_rate:
            volume = max(round(request.volume * self.costs.partial_fraction, 2), 0.01)
            pos = self._fill_open(request, volume)
            return BrokerReply(RC_PARTIAL, pos.ticket, self._ticket, volume, pos.price_open, "partial")
        pos = self._fill_open(request, request.volume)
        return BrokerReply(RC_DONE, pos.ticket, self._ticket, pos.volume, pos.price_open, "done")
