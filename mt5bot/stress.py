"""Nightly stress scenarios on the current book, run in the shared matching engine.

Each scenario copies the open positions into a fresh exchange, applies one shock bar and reads the
loss and margin level the broker's own rules would leave: a gap fills beyond the stop, a spread
explosion hits stops with the market unchanged, and a disconnection leaves positions to their
server-side stops. A breach of the daily or drawdown limits reduces exposure until the next report.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence

from .adapters.exchange import SimCosts, SimExchange
from .core import broker as b


@dataclass(frozen=True)
class Scenario:
    name: str
    move_pct: float = 0.0        # price gap against the book, in percent
    spread_mult: float = 1.0


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("gap_2pct", 2.0), Scenario("gap_3pct", 3.0), Scenario("gap_5pct", 5.0),
    Scenario("spread_x10", 0.0, 10.0), Scenario("disconnect_30min", 0.6), Scenario("flash_spike", 1.5, 3.0),
)


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    loss_pct: float
    margin_level: float
    stopped_out: int


class _Stress(SimExchange):
    def __init__(self, positions: Sequence[Any], specs: Mapping[str, Any], quotes: Mapping[str, Any], equity: float,
                 balance: float, leverage: int, so_level: float) -> None:
        super().__init__(SimCosts(slippage_points=0.0, commission_per_lot_side=0.0), balance, 1, leverage)
        self.specs = specs
        self.quotes = {k: SimpleNamespace(time=0, bid=float(v.bid), ask=float(v.ask)) for k, v in quotes.items()}
        self.account_state.margin_so_so = so_level
        for p in positions:
            self.positions_book.append(SimpleNamespace(ticket=int(p.ticket), symbol=p.symbol, type=int(p.type),
                                                       volume=float(p.volume), magic=p.magic, price_open=float(p.price_open),
                                                       sl=float(p.sl or 0.0), tp=float(p.tp or 0.0), time=0, comment="",
                                                       swap=0.0))
        self.mark_to_market()
        self.account_state.balance += equity - self.account_state.equity   # floating swap and history differ
        self.mark_to_market()

    def quote(self, symbol: str) -> Any:
        return self.quotes[symbol]

    def server_time(self) -> int:
        return 0

    def contract(self, symbol: str) -> float:
        return float(self.specs[symbol].contract_size)

    def point(self, symbol: str) -> float:
        return float(self.specs[symbol].point)

    def digits(self, symbol: str) -> int:
        return int(self.specs[symbol].digits)

    def value_per_price(self, symbol: str, price: Optional[float] = None) -> float:
        return float(self.specs[symbol].value_per_price)

    def shock(self, symbol: str, move: float, spread_mult: float) -> None:
        q = self.quotes[symbol]
        base = q.ask - q.bid
        spread = base * spread_mult
        bid = q.bid + move - (spread - base) / 2.0      # a wider spread opens on both sides of the mid
        self.fill_stops(symbol, bid, max(q.bid, bid), min(q.bid, bid), spread)
        self.quotes[symbol] = SimpleNamespace(time=0, bid=bid, ask=bid + spread)


def _worst_direction(p: Any) -> float:
    return -1.0 if p.type == b.POSITION_TYPE_BUY else 1.0


def run_scenario(s: Scenario, positions: Sequence[Any], specs: Mapping[str, Any], quotes: Mapping[str, Any], equity: float,
                 balance: float, leverage: int, so_level: float) -> ScenarioResult:
    ex = _Stress(positions, specs, quotes, equity, balance, leverage, so_level)
    moved: set[str] = set()
    for p in list(ex.positions_book):
        if p.symbol in moved:
            continue
        moved.add(p.symbol)
        ex.shock(p.symbol, _worst_direction(p) * float(quotes[p.symbol].bid) * s.move_pct / 100.0, s.spread_mult)
    ex.mark_to_market()
    forced = ex.enforce_stop_out()
    ex.mark_to_market()
    acc = ex.account_state
    loss = max(equity - acc.equity, 0.0) / equity * 100.0 if equity > 0 else 0.0
    return ScenarioResult(s.name, round(loss, 4), acc.margin_level, forced)


def run_all(positions: Sequence[Any], specs: Mapping[str, Any], quotes: Mapping[str, Any], equity: float, balance: float,
            leverage: int, so_level: float, daily_room_pct: float, drawdown_room_pct: float) -> dict[str, Any]:
    """Every scenario on one book. ``breached``: a scenario would exceed what the limits still allow."""
    results = [run_scenario(s, positions, specs, quotes, equity, balance, leverage, so_level) for s in SCENARIOS]
    worst = max(results, key=lambda r: r.loss_pct) if results else None
    breached = worst is not None and (worst.loss_pct >= daily_room_pct or worst.loss_pct >= drawdown_room_pct)
    levels = [r.margin_level for r in results if r.margin_level > 0]
    return {"scenarios": [asdict(r) for r in results], "worst": worst.name if worst else None,
            "worst_loss_pct": worst.loss_pct if worst else 0.0, "worst_margin_level": min(levels) if levels else None,
            "breached": bool(breached), "positions": len(positions)}
