"""The single decision path, end to end on the fake terminal: every gate leaves a reason code
in the funnel, every order carries SL and TP, the hard ceilings cannot be bypassed, and the
account-size machinery (exploration budget, minimum-lot exception, split orders) and the
self-healing quarantine behave as documented."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pytest

from conftest import FakeMT5, FixedSource
from mt5bot import bot as botmod
from mt5bot.arms import EntryIntent
from mt5bot.strategies.base import Signal


class Scripted:
    """Signal source returning ``make(call_number)`` on every bar (None: no signal)."""

    def __init__(self, make: Callable[[int], Optional[EntryIntent]]) -> None:
        self.make = make
        self.calls = 0

    def on_bar(self, symbol: str, closed: np.ndarray, spec: Any, costs: Any, equity: float, risk_pct: float):
        self.calls += 1
        return self.make(self.calls)

    def exit_signal(self, symbol: str, param_set_id: Any, side: str, closed: np.ndarray) -> bool:
        return False


def intent(side: str = "BUY", atr: float = 0.0010, sl: float = 2.0, tp: float = 3.0, **kwargs: Any) -> EntryIntent:
    return EntryIntent(Signal(side, atr, sl, tp), None, "scripted", f"scripted {side}", **kwargs)


def started(make_bot: Any, source: Any, **cfg: Any) -> Any:
    bot = make_bot(source=source, **cfg)
    bot.startup()
    assert bot.run_once() is None  # the first loop only records the bar
    return bot


def next_bar(bot: Any, fake: FakeMT5, before: Optional[Callable[[], None]] = None) -> None:
    fake.advance_bar("EURUSD")
    if before is not None:
        before()
    bot.run_once()
    bot.funnel.flush()


def reasons(bot: Any) -> list[str]:
    return [r[0] for r in bot.registry.conn.execute("SELECT reason FROM funnel_events ORDER BY id")]


def opening_sends(fake: FakeMT5) -> list[dict[str, Any]]:
    return [r for r in fake.sends if "position" not in r and r.get("action") == fake.TRADE_ACTION_DEAL]


# ------------------------------------------------------------------ gates


def _outside_hours(bot: Any, fake: FakeMT5) -> None:
    hour = botmod.server_datetime(fake.ticks["EURUSD"].time).hour
    bot.trading_window = lambda: ((hour + 2) % 24, (hour + 3) % 24 or 24)


GATES: dict[str, tuple[Callable[[Any, FakeMT5], None], str]] = {
    "restricted": (lambda bot, fake: setattr(fake.symbols["EURUSD"], "trade_mode",
                                             fake.SYMBOL_TRADE_MODE_CLOSEONLY), "TRADE_MODE_RESTRICTED"),
    "spread": (lambda bot, fake: fake.set_tick("EURUSD", fake.ticks["EURUSD"].bid, spread_points=100,
                                               time=fake.ticks["EURUSD"].time), "SPREAD_LIMIT"),
    "order_check": (lambda bot, fake: setattr(fake, "check_retcode", 10013), "ORDER_CHECK_FAIL"),
    "hours": (_outside_hours, "OUTSIDE_HOURS"),
}


@pytest.mark.parametrize("gate", sorted(GATES))
def test_every_gate_leaves_a_reason_code(make_bot, fake_mt5: FakeMT5, gate: str) -> None:
    setup, code = GATES[gate]
    bot = started(make_bot, FixedSource("BUY"))
    next_bar(bot, fake_mt5, lambda: setup(bot, fake_mt5))
    found = reasons(bot)
    assert "BAR_EVALUATED" in found and code in found, found
    assert [s for s in opening_sends(fake_mt5) if gate != "order_check"] == []
    assert bot.registry.trades(mode="live") == []
    bot.close()


def test_a_stop_the_minimum_lot_cannot_afford_is_refused_with_its_code(make_bot, fake_mt5: FakeMT5) -> None:
    bot = started(make_bot, Scripted(lambda n: intent(atr=0.05)))  # 1000-pip stop: 0.01 lot risks 1% of 10k
    next_bar(bot, fake_mt5)
    assert "MIN_LOT_EXCEEDS_RISK" in reasons(bot) and opening_sends(fake_mt5) == []
    bot.close()


def test_a_normal_entry_ends_in_order_ok(make_bot, fake_mt5: FakeMT5) -> None:
    bot = started(make_bot, FixedSource("BUY"))
    next_bar(bot, fake_mt5)
    found = reasons(bot)
    assert found.index("BAR_EVALUATED") < found.index("ORDER_OK")
    assert len(opening_sends(fake_mt5)) == 1
    bot.close()


# --------------------------------------------------------- hard guarantees


def test_hard_ceilings_hold_whatever_the_proposal_says(make_bot, fake_mt5: FakeMT5) -> None:
    bot = started(make_bot, FixedSource(None))
    fake_mt5.advance_bar("EURUSD")
    bot.run_once()
    acc = fake_mt5.account_info()
    greedy = intent(tier="T3", risk_pct=50.0)
    for multiplier in (1.0, 50.0, float("inf")):
        out = bot._try_open("EURUSD", greedy, acc, multiplier)
        assert not out.sent
    bot.funnel.flush()
    assert opening_sends(fake_mt5) == [] and "RISK_PER_TRADE" in reasons(bot)
    bot.close()


@pytest.mark.parametrize("seed", range(10))
def test_every_order_has_sl_and_tp_on_the_right_side(make_bot, fake_mt5: FakeMT5, seed: int) -> None:
    rng = np.random.default_rng(seed)
    side = str(rng.choice(["BUY", "SELL"]))
    proposal = intent(side, float(rng.uniform(0.0004, 0.004)), float(rng.choice([1.0, 1.5, 2.0, 2.5, 3.0])),
                      float(rng.choice([1.0, 2.0, 3.0, 4.0])))
    bot = started(make_bot, Scripted(lambda n: proposal))
    next_bar(bot, fake_mt5)
    sends = opening_sends(fake_mt5)
    assert sends, reasons(bot)
    limit = bot.risk.limits.max_trade_risk_pct
    for req in sends:
        assert req["sl"] and req["tp"]
        if side == "BUY":
            assert req["sl"] < req["price"] < req["tp"]
        else:
            assert req["tp"] < req["price"] < req["sl"]
        order_type = fake_mt5.ORDER_TYPE_BUY if side == "BUY" else fake_mt5.ORDER_TYPE_SELL
        worst = -fake_mt5.order_calc_profit(order_type, "EURUSD", req["volume"], req["price"], req["sl"])
        assert worst <= fake_mt5.account.equity * limit / 100.0 + 1e-6
    bot.close()


def test_large_orders_are_split_and_count_as_one_position(make_bot, fake_mt5: FakeMT5) -> None:
    fake_mt5.symbols["EURUSD"].volume_max = 0.1
    bot = started(make_bot, FixedSource("BUY"), commission_per_lot=0.0)
    next_bar(bot, fake_mt5)
    sends = opening_sends(fake_mt5)
    assert len(sends) == 3 and [s["volume"] for s in sends] == [0.1, 0.1, 0.03]  # 0.23 lots in 3 orders
    assert all(s["sl"] and s["tp"] for s in sends)
    rows = bot.registry.trades(mode="live")
    groups = {r.extra.get("group") for r in rows}
    assert len(rows) == 3 and len(groups) == 1 and None not in groups
    risks = bot._open_risks(bot._bot_positions(), float(fake_mt5.account.equity))
    assert len({r.group for r in risks}) == 1
    bot.close()


# ------------------------------------------------------ account-size machinery


def test_spent_exploration_budget_blocks_exploration(make_bot, fake_mt5: FakeMT5) -> None:
    bot = started(make_bot, Scripted(lambda n: intent(tier="T1", exploration=True)))
    now = bot._utc_now()
    trade_id = bot.registry.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 50.0, ticket=77,
                                       open_ts=now - 600, tier="T1", extra={"exploration": True})
    bot.registry.close_trade(trade_id, 1.09, -60.0, -1.2, "sl", close_ts=now - 300)  # beyond the 0.2%/day budget
    next_bar(bot, fake_mt5)
    assert "EXPLORATION_BUDGET_SPENT" in reasons(bot) and opening_sends(fake_mt5) == []
    bot.close()


def test_exploration_trades_at_micro_risk(make_bot, fake_mt5: FakeMT5) -> None:
    bot = started(make_bot, Scripted(lambda n: intent(tier="T1", exploration=True)), commission_per_lot=0.0)
    next_bar(bot, fake_mt5)
    [row] = bot.registry.trades(mode="live")
    assert row.tier == "T1" and row.extra["exploration"]
    assert row.risk_pct <= bot.risk.limits.t1_risk_pct + 1e-9
    assert "EXPLORATION" in reasons(bot)
    bot.close()


def test_minimum_lot_exception_on_a_small_account(make_bot, fake_mt5: FakeMT5) -> None:
    fake_mt5.account.balance = fake_mt5.account.equity = 1_500.0
    bot = started(make_bot, Scripted(lambda n: intent(tier="T1", exploration=True)), commission_per_lot=0.0)
    lim = bot.risk.limits
    next_bar(bot, fake_mt5)
    [row] = bot.registry.trades(mode="live")
    # 0.01 lot to a 200-point stop risks ~0.14% of 1500: above the T1 tier, within the exploration cap.
    assert row.volume == 0.01 and lim.t1_risk_pct < row.risk_pct <= lim.explore_trade_pct
    assert row.extra["min_lot_exception"] and "MIN_LOT_EXCEPTION" in reasons(bot)
    bot.close()


# ------------------------------------------------------------- self-healing


def test_repeated_execution_failures_quarantine_the_symbol(make_bot, fake_mt5: FakeMT5) -> None:
    mono = [1_000.0]
    bot = started(make_bot, FixedSource("BUY"), monotonic=lambda: mono[0])
    fake_mt5.check_retcode = 10013
    for _ in range(botmod.QUARANTINE_AFTER):
        next_bar(bot, fake_mt5)
    assert reasons(bot).count("ORDER_CHECK_FAIL") == botmod.QUARANTINE_AFTER
    fake_mt5.check_retcode = 0
    next_bar(bot, fake_mt5)
    assert reasons(bot)[-1] == "SYMBOL_QUARANTINED" and opening_sends(fake_mt5) == []
    bot._write_health(fake_mt5.account_info(), None)
    health = json.loads(Path(bot.cfg.health_file).read_text())
    assert health["quarantined"]["EURUSD"] > 0
    mono[0] += botmod.QUARANTINE_MINUTES * 60 + 1
    next_bar(bot, fake_mt5)
    assert "ORDER_OK" in reasons(bot) and len(opening_sends(fake_mt5)) == 1
    assert "EURUSD" not in bot._quarantine
    bot.close()


def test_quarantine_doubles_on_repeat_and_is_capped() -> None:
    minutes = [min(botmod.QUARANTINE_MINUTES * 2 ** (k - 1), botmod.QUARANTINE_MAX_MINUTES) for k in range(1, 8)]
    assert minutes[:5] == [30, 60, 120, 240, 480] and max(minutes) == botmod.QUARANTINE_MAX_MINUTES


def test_scripted_intents_keep_their_identity(make_bot, fake_mt5: FakeMT5) -> None:
    proposal = replace(intent(), policy="breakeven", regime="trend")
    bot = started(make_bot, Scripted(lambda n: proposal))
    next_bar(bot, fake_mt5)
    [row] = bot.registry.trades(mode="live")
    assert row.extra["policy"] == "breakeven" and row.extra["regime"] == "trend"
    bot.close()
