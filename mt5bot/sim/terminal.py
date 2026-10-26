"""``SimulatedTerminal``: the subset of the MetaTrader5 API the bot uses, driven by
``SyntheticMarket``. Server-side SL/TP are filled on every closed bar (stop first when
both are touched, like the backtester), deals are recorded with commission, and equity
follows floating P&L. It lets the REAL bot code run with no MT5, broker or network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import numpy as np

from .market import RATES_DTYPE, SyntheticMarket

CONSTANTS: dict[str, int] = {
    "TIMEFRAME_M1": 1, "TIMEFRAME_M5": 5, "TIMEFRAME_M15": 15, "TIMEFRAME_M30": 30, "TIMEFRAME_H1": 16385,
    "TIMEFRAME_H4": 16388, "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1, "POSITION_TYPE_BUY": 0,
    "POSITION_TYPE_SELL": 1, "TRADE_ACTION_DEAL": 1, "TRADE_ACTION_SLTP": 6, "TRADE_ACTION_REMOVE": 8,
    "ORDER_TIME_GTC": 0, "ORDER_FILLING_FOK": 0, "ORDER_FILLING_IOC": 1, "ORDER_FILLING_RETURN": 2,
    "SYMBOL_TRADE_MODE_DISABLED": 0, "SYMBOL_TRADE_MODE_FULL": 4, "ACCOUNT_TRADE_MODE_DEMO": 0,
    "ACCOUNT_TRADE_MODE_CONTEST": 1, "ACCOUNT_TRADE_MODE_REAL": 2, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING": 0,
    "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING": 2, "DEAL_ENTRY_IN": 0, "DEAL_ENTRY_OUT": 1, "DEAL_ENTRY_OUT_BY": 3,
    "DEAL_TYPE_BUY": 0, "DEAL_TYPE_SELL": 1, "DEAL_TYPE_BALANCE": 2, "TRADE_RETCODE_REQUOTE": 10004,
    "TRADE_RETCODE_REJECT": 10006, "TRADE_RETCODE_PLACED": 10008, "TRADE_RETCODE_DONE": 10009,
    "TRADE_RETCODE_DONE_PARTIAL": 10010, "TRADE_RETCODE_INVALID_STOPS": 10016, "TRADE_RETCODE_MARKET_CLOSED": 10018,
    "TRADE_RETCODE_NO_MONEY": 10019, "TRADE_RETCODE_PRICE_CHANGED": 10020, "TRADE_RETCODE_PRICE_OFF": 10021,
}
COMMISSION_PER_LOT_SIDE = 3.5
TIMEFRAME_SECONDS = {1: 60, 5: 300, 15: 900, 30: 1800, 16385: 3600, 16388: 14400}
MAX_BASE_BARS = 12_000
SERVER_OFFSET = 2 * 3600
SIM_LOGIN = 10_000_001
SIM_SERVER = "CYBERBOX-Simulation"


class SimulatedTerminal:
    def __init__(self, market: SyntheticMarket, warmup_bars: int = 1500, balance: float = 10_000.0,
                 slippage_points: float = 0.5) -> None:
        for name, value in CONSTANTS.items():
            setattr(self, name, value)
        self.market = market
        self.slippage_points = slippage_points
        self.rng = np.random.default_rng(int(market.rng.integers(1 << 31)))
        self.rates: dict[str, np.ndarray] = market.history(warmup_bars)
        self.positions: list[SimpleNamespace] = []
        self.deals: list[SimpleNamespace] = []
        self.orders: list[SimpleNamespace] = []
        self._ticket = 500_000
        self.account = SimpleNamespace(
            login=SIM_LOGIN, server=SIM_SERVER, currency="USD", trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING, leverage=100, trade_allowed=True, trade_expert=True,
            equity=balance, balance=balance)
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True, path=r"C:\CYBERBOX\Simulation")
        self._agg_cache: dict[tuple[str, int], tuple[int, np.ndarray]] = {}
        self._open_forming_bars()

    # ---------------------------------------------------------------- market clock

    def _open_forming_bars(self) -> None:
        t = self.market.time
        for name, rates in self.rates.items():
            last = rates[-1]
            forming = np.zeros(1, dtype=RATES_DTYPE)
            forming["time"] = t
            forming["open"] = forming["high"] = forming["low"] = forming["close"] = last["close"]
            forming["spread"], forming["tick_volume"] = last["spread"], 1
            self.rates[name] = np.concatenate([rates, forming])

    @property
    def server_time(self) -> int:
        return int(self.rates[self.market.symbols[0].name][-1]["time"]) + 60

    def utc_now(self) -> float:
        return float(self.server_time - SERVER_OFFSET)

    def advance_bar(self) -> int:
        """Close the forming bar with the next synthetic bar, fill stops, open a new forming bar."""
        bar_time, bars = self.market.next_bars()
        for sym, (o, h, lo, c, spr, vol) in zip(self.market.symbols, bars):
            rates = self.rates[sym.name]
            row = rates[-1]
            row["time"], row["open"], row["high"], row["low"], row["close"] = bar_time, o, h, lo, c
            row["spread"], row["tick_volume"] = spr, vol
            self._fill_stops(sym.name, row)
            if len(rates) > MAX_BASE_BARS + 1000:  # bounded memory over long runs
                self.rates[sym.name] = rates[-MAX_BASE_BARS:]
        self._open_forming_bars()
        self._mark_to_market()
        return bar_time

    # ------------------------------------------------------------------ money

    def _value_per_price(self, symbol: str, price: Optional[float] = None) -> float:
        model = self.market.model(symbol)
        contract = model.contract if model else 100_000.0
        if symbol.endswith("USD"):
            return contract
        quote = price if price else float(self.rates[symbol][-1]["close"])
        return contract / max(quote, 1e-9)

    def _profit(self, symbol: str, is_buy: bool, volume: float, open_price: float, close_price: float) -> float:
        direction = 1.0 if is_buy else -1.0
        return (close_price - open_price) * direction * volume * self._value_per_price(symbol, close_price)

    def _point(self, symbol: str) -> float:
        model = self.market.model(symbol)
        return model.point if model else 0.00001

    def _mark_to_market(self) -> None:
        floating = 0.0
        for p in self.positions:
            tick = self.symbol_info_tick(p.symbol)
            price = tick.bid if p.type == self.POSITION_TYPE_BUY else tick.ask
            floating += self._profit(p.symbol, p.type == self.POSITION_TYPE_BUY, p.volume, p.price_open, price)
        self.account.equity = round(self.account.balance + floating, 2)

    def _deal(self, p: SimpleNamespace, entry: int, price: float, profit: float, volume: float) -> None:
        self._ticket += 1
        self.deals.append(SimpleNamespace(
            ticket=self._ticket, position_id=p.ticket, symbol=p.symbol, entry=entry, price=price, profit=profit,
            commission=-COMMISSION_PER_LOT_SIDE * volume, swap=0.0, fee=0.0, volume=volume, type=1 - p.type,
            magic=p.magic, time=self.server_time))

    def _close(self, p: SimpleNamespace, price: float, volume: Optional[float] = None) -> None:
        volume = p.volume if volume is None else volume
        profit = round(self._profit(p.symbol, p.type == self.POSITION_TYPE_BUY, volume, p.price_open, price), 2)
        self._deal(p, self.DEAL_ENTRY_OUT, price, profit, volume)
        self.account.balance = round(self.account.balance + profit - COMMISSION_PER_LOT_SIDE * volume, 2)
        if volume >= p.volume - 1e-9:
            self.positions.remove(p)
        else:
            p.volume = round(p.volume - volume, 2)

    def _fill_stops(self, symbol: str, bar: Any) -> None:
        spread = float(bar["spread"]) * self._point(symbol)
        for p in [q for q in self.positions if q.symbol == symbol]:
            buy = p.type == self.POSITION_TYPE_BUY
            hit_sl = bool(p.sl) and ((bar["low"] <= p.sl) if buy else (bar["high"] + spread >= p.sl))
            hit_tp = bool(p.tp) and ((bar["high"] >= p.tp) if buy else (bar["low"] + spread <= p.tp))
            if hit_sl:
                self._close(p, float(p.sl))
            elif hit_tp:
                self._close(p, float(p.tp))

    # ------------------------------------------------------------ MT5 API surface

    def initialize(self, **kwargs: Any) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self) -> tuple[int, str]:
        return (1, "Success")

    def terminal_info(self) -> Any:
        return self.terminal

    def account_info(self) -> Any:
        return self.account

    def symbol_select(self, name: str, enable: bool) -> bool:
        return name in self.rates

    def symbols_get(self, group: Optional[str] = None) -> tuple[Any, ...]:
        needle = (group or "").strip("*").upper()
        return tuple(self.symbol_info(n) for n in self.rates if needle in n.upper())

    def symbol_info(self, name: str) -> Any:
        model = self.market.model(name)
        if model is None:
            return None
        price = float(self.rates[name][-1]["close"])
        return SimpleNamespace(
            name=name, point=model.point, digits=model.digits, trade_mode=self.SYMBOL_TRADE_MODE_FULL,
            trade_stops_level=0, trade_freeze_level=0, volume_min=0.01, volume_max=50.0, volume_step=0.01,
            filling_mode=2, trade_contract_size=model.contract,
            trade_tick_value=model.point * self._value_per_price(name, price), trade_tick_size=model.point,
            swap_long=-0.5, swap_short=-0.3, swap_mode=1, swap_rollover3days=3, currency_base=name[:3],
            currency_profit=name[3:6])

    def symbol_info_tick(self, name: str) -> Any:
        rates = self.rates.get(name)
        if rates is None:
            return None
        last = rates[-1]
        point = self._point(name)
        bid = float(last["close"])
        model = self.market.model(name)
        ask = round(bid + int(last["spread"]) * point, model.digits if model else 5)
        return SimpleNamespace(time=int(last["time"]) + 60, bid=bid, ask=ask)

    def _aggregated(self, name: str, seconds: int) -> np.ndarray:
        """Bars of ``seconds`` built from the base bars (the last one may still be forming)."""
        base = self.rates[name]
        key = (name, seconds)
        stamp = int(base[-1]["time"]) * 100_000 + len(base)
        cached = self._agg_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        times = np.asarray(base["time"], dtype=np.int64)
        bucket = times // seconds
        starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
        ends = np.r_[starts[1:], len(base)]
        out = np.zeros(len(starts), dtype=RATES_DTYPE)
        out["time"] = bucket[starts] * seconds
        out["open"] = base["open"][starts]
        out["close"] = base["close"][ends - 1]
        out["high"] = np.maximum.reduceat(np.asarray(base["high"]), starts)
        out["low"] = np.minimum.reduceat(np.asarray(base["low"]), starts)
        out["tick_volume"] = np.add.reduceat(np.asarray(base["tick_volume"], dtype=np.uint64), starts)
        out["spread"] = np.minimum.reduceat(np.asarray(base["spread"]), starts)
        self._agg_cache[key] = (stamp, out)
        return out

    def copy_rates_from_pos(self, name: str, timeframe: int, start: int, count: int) -> Any:
        rates = self.rates.get(name)
        if rates is None:
            return None
        seconds = TIMEFRAME_SECONDS.get(int(timeframe), self.market.bar_seconds)
        if seconds > self.market.bar_seconds:
            rates = self._aggregated(name, seconds)
        end = len(rates) - start
        return rates[max(0, end - count):end].copy()

    def positions_get(self, symbol: Optional[str] = None) -> tuple[Any, ...]:
        return tuple(p for p in self.positions if symbol is None or p.symbol == symbol)

    def orders_get(self) -> tuple[Any, ...]:
        return tuple(self.orders)

    def history_deals_get(self, *args: Any, position: Optional[int] = None) -> tuple[Any, ...]:
        return tuple(d for d in self.deals if position is None or d.position_id == position)

    def order_calc_profit(self, order_type: int, symbol: str, volume: float, price_open: float,
                          price_close: float) -> float:
        return self._profit(symbol, order_type == self.ORDER_TYPE_BUY, volume, price_open, price_close)

    def order_calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float:
        notional = volume * (self.market.model(symbol).contract if self.market.model(symbol) else 100_000.0)
        if symbol.endswith("USD"):
            notional *= price
        return notional / self.account.leverage

    def order_check(self, request: dict[str, Any]) -> Any:
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, request: dict[str, Any]) -> Any:
        action = request.get("action")
        done = self.TRADE_RETCODE_DONE
        if action == self.TRADE_ACTION_SLTP:
            for p in self.positions:
                if p.ticket == request.get("position"):
                    p.sl, p.tp = request.get("sl", p.sl), request.get("tp", p.tp)
            return SimpleNamespace(retcode=done, comment="sltp", volume=0.0, price=0.0, order=0, deal=0)
        if action == self.TRADE_ACTION_REMOVE:
            self.orders = [o for o in self.orders if o.ticket != request.get("order")]
            return SimpleNamespace(retcode=done, comment="removed", volume=0.0, price=0.0, order=0, deal=0)
        symbol = request["symbol"]
        tick = self.symbol_info_tick(symbol)
        is_buy = request["type"] == self.ORDER_TYPE_BUY
        # Like a real terminal: slippage in whole points, fills on the symbol's price grid.
        slip = round(abs(float(self.rng.normal(0, self.slippage_points)))) * self._point(symbol)
        model = self.market.model(symbol)
        price = round((tick.ask + slip) if is_buy else (tick.bid - slip), model.digits if model else 5)
        if "position" in request:
            target = next((p for p in self.positions if p.ticket == request["position"]), None)
            if target is None:
                return SimpleNamespace(retcode=self.TRADE_RETCODE_REJECT, comment="no position", volume=0.0,
                                       price=0.0, order=0, deal=0)
            self._close(target, price, float(request["volume"]))
            self._mark_to_market()
            return SimpleNamespace(retcode=done, comment="closed", volume=request["volume"], price=price,
                                   order=self._ticket, deal=self._ticket)
        self._ticket += 1
        pos = SimpleNamespace(ticket=self._ticket, symbol=symbol,
                              type=self.POSITION_TYPE_BUY if is_buy else self.POSITION_TYPE_SELL,
                              volume=float(request["volume"]), magic=request.get("magic", 0), price_open=price,
                              sl=float(request.get("sl", 0.0)), tp=float(request.get("tp", 0.0)),
                              time=self.server_time)
        self.positions.append(pos)
        self._deal(pos, self.DEAL_ENTRY_IN, price, 0.0, pos.volume)
        self.account.balance = round(self.account.balance - COMMISSION_PER_LOT_SIDE * pos.volume, 2)
        self._mark_to_market()
        return SimpleNamespace(retcode=done, comment="done", volume=pos.volume, price=price, order=pos.ticket,
                               deal=self._ticket)
