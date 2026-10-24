"""Tests for mt5_bot. Run with: pytest -q"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import mt5_bot
from conftest import BAR_SECONDS, FakeMT5, make_rates
from mt5_bot import Config, DailyState, Signal, StartupError, atr, ema, generate_signal, lots_for_risk

PROJECT_DIR = Path(__file__).resolve().parent.parent


def always(side: str, atr_value: float = 0.0010):
    def strategy(closed_bars: np.ndarray, cfg: Config) -> Signal:
        return Signal(side, atr_value)

    return strategy


def started(bot: mt5_bot.Bot) -> mt5_bot.Bot:
    """Start the bot and consume the first loop, which only records the current bar."""
    bot.startup()
    assert bot.run_once() is None
    return bot


def opening_sends(fake: FakeMT5) -> list[dict[str, Any]]:
    return [r for r in fake.sends if "position" not in r]


# ----------------------------------------------------------------------- indicators


class TestIndicators:
    def test_ema_constant_series_is_constant(self) -> None:
        out = ema(np.full(50, 1.2345), 10)
        assert np.isnan(out[:9]).all()
        np.testing.assert_allclose(out[9:], 1.2345)

    def test_ema_known_values(self) -> None:
        # period 3: seed = mean(1,2,3) = 2; alpha = 0.5 -> 3, 4, 5
        out = ema([1, 2, 3, 4, 5, 6], 3)
        np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0, 5.0])
        assert np.isnan(out[:2]).all()

    def test_ema_short_input_all_nan_and_bad_period(self) -> None:
        assert np.isnan(ema([1.0, 2.0], 5)).all()
        with pytest.raises(ValueError):
            ema([1.0, 2.0], 0)

    def test_atr_constant_range(self) -> None:
        n = 40
        out = atr(np.full(n, 1.1010), np.full(n, 1.1000), np.full(n, 1.1005), 14)
        assert np.isnan(out[:14]).all()
        np.testing.assert_allclose(out[14:], 0.0010)

    def test_atr_known_values_wilder(self) -> None:
        high = [10, 12, 13, 12, 15]
        low = [9, 10, 11, 10, 12]
        close = [9.5, 11, 12, 11, 14]
        # TR from bar 1: max(2, 2.5, 0.5)=2.5; max(2, 2, 0)=2; max(2, 0, 2)=2; max(3, 4, 1)=4
        out = atr(high, low, close, 2)
        assert np.isnan(out[:2]).all()
        assert out[2] == pytest.approx((2.5 + 2) / 2)
        assert out[3] == pytest.approx((2.25 * 1 + 2) / 2)
        assert out[4] == pytest.approx((2.125 * 1 + 4) / 2)

    def test_atr_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            atr([1, 2], [1], [1, 2], 1)


# ------------------------------------------------------------------------- strategy


def walk_forward_signals(rates: np.ndarray, cfg: Config) -> list[Signal]:
    """Feed the strategy growing windows of closed bars, as the live loop does."""
    signals = []
    for end in range(2, len(rates) + 1):
        sig = generate_signal(rates[:end], cfg)
        if sig is not None:
            signals.append(sig)
    return signals


@pytest.fixture
def strat_cfg(make_cfg) -> Config:
    return make_cfg(fast_ema=5, slow_ema=20, atr_period=14, bars=62)


class TestStrategy:
    def test_down_then_up_gives_exactly_one_buy(self, strat_cfg: Config) -> None:
        closes = np.concatenate([1.2 - 0.0005 * np.arange(80), 1.16 + 0.0005 * np.arange(1, 81)])
        signals = walk_forward_signals(make_rates(closes), strat_cfg)
        assert [s.side for s in signals] == ["BUY"]
        assert signals[0].atr > 0

    def test_up_then_down_gives_exactly_one_sell(self, strat_cfg: Config) -> None:
        closes = np.concatenate([1.1 + 0.0005 * np.arange(80), 1.14 - 0.0005 * np.arange(1, 81)])
        signals = walk_forward_signals(make_rates(closes), strat_cfg)
        assert [s.side for s in signals] == ["SELL"]

    def test_flat_series_gives_nothing(self, strat_cfg: Config) -> None:
        assert walk_forward_signals(make_rates(np.full(150, 1.1)), strat_cfg) == []

    def test_too_few_bars_gives_none(self, strat_cfg: Config) -> None:
        assert generate_signal(make_rates(np.linspace(1, 2, 10)), strat_cfg) is None

    def test_signal_validation(self) -> None:
        with pytest.raises(ValueError):
            Signal("HOLD", 0.001)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Signal("BUY", 0.0)
        with pytest.raises(ValueError):
            Signal("BUY", float("nan"))

    def test_bot_passes_only_closed_bars_to_strategy(self, make_bot, fake_mt5: FakeMT5) -> None:
        seen: list[np.ndarray] = []

        def spy(closed_bars: np.ndarray, cfg: Config) -> None:
            seen.append(closed_bars.copy())
            return None

        bot = started(make_bot(strategy=spy))
        assert seen == []  # first loop only records the bar
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(seen) == 1
        forming_bar_time = fake_mt5.rates["EURUSD"]["time"][-1]
        assert seen[0]["time"][-1] == forming_bar_time - BAR_SECONDS
        assert forming_bar_time not in seen[0]["time"]
        assert len(seen[0]) == bot.cfg.bars

    def test_acts_once_per_closed_bar(self, make_bot, fake_mt5: FakeMT5) -> None:
        calls: list[int] = []
        bot = started(make_bot(strategy=lambda bars, cfg: calls.append(1)))
        bot.run_once()
        bot.run_once()
        assert calls == []
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        bot.run_once()
        assert calls == [1]


# -------------------------------------------------------------------------- sizing


class TestLotsForRisk:
    def test_normal_case(self) -> None:
        assert lots_for_risk(50, 210, 0.01, 100, 0.01) == 0.23

    def test_step_point_one_rounds_down(self) -> None:
        assert lots_for_risk(50, 210, 0.1, 100, 0.1) == 0.2

    def test_step_point_zero_one_rounds_down(self) -> None:
        assert lots_for_risk(100, 421, 0.01, 100, 0.01) == 0.23  # 0.2375 -> 0.23

    def test_no_float_artifacts(self) -> None:
        # Naive float math gives 0.3 / 0.1 = 2.9999999999999996 -> 0.2
        assert lots_for_risk(0.3, 1, 0.1, 100, 0.1) == 0.3

    def test_below_minimum_returns_zero(self) -> None:
        assert lots_for_risk(1, 210, 0.01, 100, 0.01) == 0.0

    def test_capped_at_maximum(self) -> None:
        assert lots_for_risk(1_000_000, 10, 0.01, 50, 0.01) == 50.0

    @pytest.mark.parametrize(
        "args",
        [
            (0, 210, 0.01, 100, 0.01),
            (-50, 210, 0.01, 100, 0.01),
            (50, 0, 0.01, 100, 0.01),
            (50, -210, 0.01, 100, 0.01),
            (50, 210, 0.01, 100, 0),
            (50, 210, 0.01, 100, -0.01),
            (50, 210, 0, 100, 0.01),
            (50, 210, 1, 0.5, 0.01),
            (float("nan"), 210, 0.01, 100, 0.01),
            (float("inf"), 210, 0.01, 100, 0.01),
        ],
    )
    def test_invalid_inputs_return_zero(self, args: tuple) -> None:
        assert lots_for_risk(*args) == 0.0

    @pytest.mark.parametrize("risk", [7.3, 13.37, 49.99, 120.5])
    @pytest.mark.parametrize("loss", [37.1, 99.9, 210.0])
    def test_never_exceeds_risk(self, risk: float, loss: float) -> None:
        lots = lots_for_risk(risk, loss, 0.01, 100, 0.01)
        assert lots * loss <= risk + 1e-9

    def test_filling_mode_selection(self, fake_mt5: FakeMT5) -> None:
        assert mt5_bot.select_filling_mode(3, fake_mt5) == fake_mt5.ORDER_FILLING_IOC
        assert mt5_bot.select_filling_mode(1, fake_mt5) == fake_mt5.ORDER_FILLING_FOK
        assert mt5_bot.select_filling_mode(0, fake_mt5) == fake_mt5.ORDER_FILLING_RETURN


# -------------------------------------------------------------------------- config


class TestConfig:
    def test_shipped_config_is_valid_and_demo_safe(self) -> None:
        cfg = Config.load(PROJECT_DIR / "config.json")
        assert cfg.symbols == ("EURUSD",)
        assert cfg.timeframe == "M15"
        assert cfg.risk_per_trade_pct == 0.5
        assert cfg.max_daily_loss_pct == 2.0
        assert cfg.allow_live is False

    def test_valid_file_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"symbols": ["XAUUSD", "EURUSD.m"], "trading_hours_server": [22, 6]}))
        cfg = Config.load(path)
        assert cfg.symbols == ("XAUUSD", "EURUSD.m")
        assert cfg.trading_hours_server == (22, 6)

    def test_bom_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"timeframe": "H1"}), encoding="utf-8-sig")
        assert Config.load(path).timeframe == "H1"

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"symbol": ["EURUSD"]}))
        with pytest.raises(ValueError, match="Unknown config key"):
            Config.load(path)

    def test_invalid_json_and_missing_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            Config.load(bad)
        with pytest.raises(ValueError, match="Cannot read"):
            Config.load(tmp_path / "missing.json")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"symbols": []},
            {"symbols": [""]},
            {"symbols": ["EURUSD", "EURUSD"]},
            {"timeframe": "D1"},
            {"fast_ema": 50, "slow_ema": 50},
            {"fast_ema": 60, "slow_ema": 50},
            {"trading_hours_server": [8, 8]},
            {"trading_hours_server": [-1, 10]},
            {"trading_hours_server": [7, 25]},
            {"trading_hours_server": [7]},
            {"sl_atr_mult": 0},
            {"tp_atr_mult": -1},
            {"bars": 100},
            {"risk_per_trade_pct": 0},
            {"risk_per_trade_pct": 2.5},
            {"max_daily_loss_pct": 11},
            {"max_open_positions_total": 0},
            {"max_spread_points": 0},
            {"deviation_points": -1},
            {"magic": 0},
            {"magic": "123"},
            {"allow_live": "yes"},
            {"allow_live": 1},
            {"bars": True},
            {"poll_seconds": 0},
            {"heartbeat_minutes": 0},
            {"kill_switch_file": ""},
            {"mt5_path": ""},
        ],
    )
    def test_invalid_values_raise(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            Config.from_dict(overrides)

    def test_required_bars(self) -> None:
        assert Config(slow_ema=50, atr_period=14).required_bars() == 152

    def test_config_is_frozen(self) -> None:
        cfg = Config()
        with pytest.raises(AttributeError):
            cfg.allow_live = True  # type: ignore[misc]

    @pytest.mark.parametrize(
        "hour,window,expected",
        [(7, (7, 20), True), (19, (7, 20), True), (20, (7, 20), False), (6, (7, 20), False),
         (23, (22, 6), True), (3, (22, 6), True), (6, (22, 6), False), (0, (0, 24), True)],
    )
    def test_in_trading_hours(self, hour: int, window: tuple[int, int], expected: bool) -> None:
        assert mt5_bot.in_trading_hours(hour, window) is expected


# --------------------------------------------------------------------- daily state


class TestDailyState:
    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = DailyState(path)
        assert state.roll("2025-10-09", 10_000.0) is True
        again = DailyState(path)
        assert again.day == "2025-10-09"
        assert again.start_equity == 10_000.0
        assert again.roll("2025-10-09", 9_000.0) is False
        assert again.start_equity == 10_000.0  # a restart must not reset the day's baseline

    def test_halts_and_halt_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = DailyState(path)
        state.roll("2025-10-09", 10_000.0)
        assert not state.limit_reached(9_801.0, 2.0)
        assert state.limit_reached(9_800.0, 2.0)
        state.halt()
        assert DailyState(path).halted is True

    def test_resets_on_new_day(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = DailyState(path)
        state.roll("2025-10-09", 10_000.0)
        state.halt()
        assert state.roll("2025-10-10", 9_700.0) is True
        assert state.halted is False
        assert state.start_equity == 9_700.0
        assert DailyState(path).halted is False

    @pytest.mark.parametrize("content", ["{broken", "{}", '{"day": 1, "start_equity": 1, "halted": false}',
                                         '{"day": "x", "start_equity": "a", "halted": false}'])
    def test_corrupt_file_refuses(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "state.json"
        path.write_text(content)
        with pytest.raises(ValueError, match="corrupt"):
            DailyState(path)


# ----------------------------------------------------------------- startup checks


class TestStartup:
    def test_demo_account_starts(self, make_bot) -> None:
        make_bot().startup()

    def test_refuses_real_account_when_allow_live_false(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_REAL
        with pytest.raises(StartupError, match="REAL account"):
            make_bot().startup()

    def test_real_account_allowed_when_allow_live_true(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_REAL
        make_bot(allow_live=True).startup()

    @pytest.mark.parametrize(
        "breakage,message",
        [
            (lambda f: setattr(f, "init_ok", False), "initialize"),
            (lambda f: setattr(f.terminal, "connected", False), "not connected"),
            (lambda f: setattr(f.terminal, "trade_allowed", False), "Algo Trading"),
            (lambda f: setattr(f.account, "trade_allowed", False), "not allowed"),
            (lambda f: setattr(f.account, "trade_expert", False), "expert"),
            (lambda f: f.symbols.clear(), "not found"),
        ],
    )
    def test_actionable_startup_failures(self, make_bot, fake_mt5: FakeMT5, breakage, message: str) -> None:
        breakage(fake_mt5)
        with pytest.raises(StartupError, match=message):
            make_bot().startup()

    def test_env_credentials_used_and_never_logged(self, make_bot, fake_mt5: FakeMT5,
                                                   caplog: pytest.LogCaptureFixture) -> None:
        env = {"MT5_LOGIN": "87654321", "MT5_PASSWORD": "s3cr3t-P@ss", "MT5_SERVER": "Broker-Demo"}
        caplog.set_level(logging.DEBUG, logger="mt5_bot")
        make_bot(env=env, mt5_path=r"C:\MT5\terminal64.exe").startup()
        assert fake_mt5.init_calls[-1] == {
            "path": r"C:\MT5\terminal64.exe", "login": 87654321, "password": "s3cr3t-P@ss", "server": "Broker-Demo",
        }
        assert "s3cr3t-P@ss" not in caplog.text
        assert "87654321" not in caplog.text  # login is masked

    def test_partial_env_credentials_rejected(self, make_bot) -> None:
        with pytest.raises(StartupError, match="all three"):
            make_bot(env={"MT5_LOGIN": "1"}).startup()

    def test_main_returns_2_on_startup_failure(self, tmp_path: Path) -> None:
        # Uses the module-level stub, whose initialize() always fails.
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "log_file": str(tmp_path / "bot.log"),
            "state_file": str(tmp_path / "state.json"),
            "kill_switch_file": str(tmp_path / "KILL"),
        }))
        try:
            assert mt5_bot.main(["--config", str(cfg_path), "--dry-run"]) == mt5_bot.EXIT_STARTUP
        finally:
            for handler in list(mt5_bot.log.handlers):
                handler.close()
                mt5_bot.log.removeHandler(handler)
            mt5_bot.log.propagate = True
        assert "initialize() failed" in (tmp_path / "bot.log").read_text(encoding="utf-8")

    def test_main_returns_2_on_config_error(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"timeframe": "W1"}))
        assert mt5_bot.main(["--config", str(cfg_path)]) == mt5_bot.EXIT_STARTUP


# ------------------------------------------------------------------------ trading


class TestEntries:
    def test_opens_with_sl_tp_and_risk_based_volume(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("BUY", 0.0010)))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()

        [req] = opening_sends(fake_mt5)
        tick = fake_mt5.ticks["EURUSD"]
        assert req["type"] == fake_mt5.ORDER_TYPE_BUY
        assert req["magic"] == bot.cfg.magic
        assert 0 < req["sl"] < req["price"] < req["tp"]
        assert req["price"] == tick.ask
        assert req["type_filling"] == fake_mt5.ORDER_FILLING_IOC
        # 0.5% of 10k = 50 USD; SL ~200 points + 10 points deviation = ~210 USD per lot -> 0.23
        assert req["volume"] == 0.23
        worst_loss = -fake_mt5.order_calc_profit(fake_mt5.ORDER_TYPE_BUY, "EURUSD", req["volume"],
                                                 req["price"] + 10 * 0.00001, req["sl"])
        assert worst_loss <= 50.0
        assert len(fake_mt5.checks) == 1  # order_check always precedes order_send

    def test_sell_has_sl_above_and_tp_below(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("SELL")))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        [req] = opening_sends(fake_mt5)
        assert req["type"] == fake_mt5.ORDER_TYPE_SELL
        assert req["tp"] < req["price"] < req["sl"]

    def test_dry_run_checks_but_never_sends(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
        bot = started(make_bot(strategy=always("BUY"), dry_run=True))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.checks, "dry run must still call order_check"
        # Closing paths (opposite signal, kill switch) must not send either.
        fake_mt5.add_position("EURUSD", "SELL", magic=510001)
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        (tmp_path / "KILL").touch()
        assert bot.run() == mt5_bot.EXIT_OK
        assert fake_mt5.sends == []

    def test_spread_filter_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("BUY"), max_spread_points=20))
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.ticks["EURUSD"].ask = fake_mt5.ticks["EURUSD"].bid + 0.00050
        bot.run_once()
        assert fake_mt5.checks == [] and fake_mt5.sends == []

    def test_hours_filter_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("BUY"), trading_hours_server=[7, 20]))
        fake_mt5.advance_bar("EURUSD")
        tick = fake_mt5.ticks["EURUSD"]
        tick.time = tick.time - (tick.time % 86400) + 3 * 3600  # 03:00 server time
        bot.run_once()
        assert fake_mt5.checks == [] and fake_mt5.sends == []

    def test_restricted_trade_mode_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.symbols["EURUSD"].trade_mode = fake_mt5.SYMBOL_TRADE_MODE_CLOSEONLY
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_stops_level_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("BUY", 0.0010)))
        fake_mt5.symbols["EURUSD"].trade_stops_level = 500
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_below_min_volume_skips(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.equity = 100.0  # 0.5 USD risk < 0.01 lot * 210 USD
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_order_check_failure_skips_send(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.check_retcode = 10019
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(fake_mt5.checks) == 1 and fake_mt5.sends == []

    def test_one_position_per_symbol(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_position("EURUSD", "BUY", magic=510001)
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_foreign_positions_do_not_block_symbol(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_position("EURUSD", "SELL", magic=999)
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(opening_sends(fake_mt5)) == 1
        assert all(r.get("position") is None for r in fake_mt5.sends)  # foreign SELL untouched

    def test_max_open_positions_total(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_symbol("GBPUSD", bid=1.3)
        fake_mt5.add_position("GBPUSD", "BUY", magic=510001)
        bot = started(make_bot(strategy=always("BUY"), symbols=["EURUSD"], max_open_positions_total=1))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_close_on_opposite_then_reverse(self, make_bot, fake_mt5: FakeMT5) -> None:
        old = fake_mt5.add_position("EURUSD", "SELL", magic=510001)
        bot = started(make_bot(strategy=always("BUY"), close_on_opposite=True))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends[0]["position"] == old.ticket
        assert fake_mt5.sends[0]["type"] == fake_mt5.ORDER_TYPE_BUY
        assert fake_mt5.sends[0]["price"] == fake_mt5.ticks["EURUSD"].ask
        assert len(opening_sends(fake_mt5)) == 1

    def test_no_close_on_opposite_when_disabled(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_position("EURUSD", "SELL", magic=510001)
        bot = started(make_bot(strategy=always("BUY"), close_on_opposite=False))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_retries_on_requote_with_fresh_price(self, make_bot, fake_mt5: FakeMT5, waits: list[float]) -> None:
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.send_retcodes = [fake_mt5.TRADE_RETCODE_REQUOTE, fake_mt5.TRADE_RETCODE_DONE]
        original_send = fake_mt5.order_send

        def send_then_move_price(request: dict[str, Any]) -> Any:
            result = original_send(request)
            fake_mt5.set_tick("EURUSD", bid=1.10020)  # market moves after the requote
            return result

        fake_mt5.order_send = send_then_move_price  # type: ignore[method-assign]
        bot.run_once()
        assert len(fake_mt5.sends) == 2
        first, second = fake_mt5.sends
        assert second["price"] == pytest.approx(1.10030)
        assert second["price"] - second["sl"] == pytest.approx(first["price"] - first["sl"])
        assert second["volume"] == first["volume"]
        assert mt5_bot.RETRY_PAUSE_SECONDS in waits

    def test_gives_up_after_three_requotes(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.send_retcodes = [fake_mt5.TRADE_RETCODE_REQUOTE] * 5
        bot.run_once()
        assert len(fake_mt5.sends) == mt5_bot.MAX_SEND_ATTEMPTS

    def test_does_not_retry_other_retcodes(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.send_retcodes = [fake_mt5.TRADE_RETCODE_NO_MONEY]
        bot.run_once()
        assert len(fake_mt5.sends) == 1

    def test_exception_in_one_symbol_is_isolated(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_symbol("GBPUSD", bid=1.3)
        bot = started(make_bot(strategy=always("BUY"), symbols=["EURUSD", "GBPUSD"]))
        fake_mt5.rates_error["EURUSD"] = RuntimeError("boom")
        fake_mt5.advance_bar("GBPUSD")
        assert bot.run_once() is None
        assert [r["symbol"] for r in opening_sends(fake_mt5)] == ["GBPUSD"]


# ---------------------------------------------------------------- guards and ops


class TestGuards:
    def test_kill_switch_closes_only_bot_positions_and_exits(self, make_bot, fake_mt5: FakeMT5,
                                                              tmp_path: Path) -> None:
        mine = fake_mt5.add_position("EURUSD", "BUY", magic=510001)
        foreign = fake_mt5.add_position("EURUSD", "SELL", magic=777)
        bot = make_bot()
        bot.startup()
        (tmp_path / "KILL").touch()
        assert bot.run() == mt5_bot.EXIT_OK
        assert [r["position"] for r in fake_mt5.sends] == [mine.ticket]
        assert fake_mt5.positions == [foreign]

    def test_kill_switch_reports_failure_if_close_fails(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
        fake_mt5.add_position("EURUSD", "BUY", magic=510001)
        fake_mt5.send_retcodes = [10018]  # market closed
        bot = make_bot()
        bot.startup()
        (tmp_path / "KILL").touch()
        assert bot.run() == mt5_bot.EXIT_ERROR

    def test_daily_loss_closes_bot_positions_and_halts(self, make_bot, fake_mt5: FakeMT5) -> None:
        mine = fake_mt5.add_position("EURUSD", "BUY", magic=510001)
        foreign = fake_mt5.add_position("EURUSD", "BUY", magic=42)
        bot = started(make_bot(strategy=always("SELL"), close_on_opposite=False, max_daily_loss_pct=2.0))
        assert bot.state.start_equity == 10_000.0

        fake_mt5.account.equity = 9_790.0  # -2.1%
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert bot.state.halted
        assert [r.get("position") for r in fake_mt5.sends] == [mine.ticket]
        assert fake_mt5.positions == [foreign]

        sends_before = len(fake_mt5.sends)
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(fake_mt5.sends) == sends_before  # halted: no new entries

        # Next server day: guard resets and trading resumes.
        tick = fake_mt5.ticks["EURUSD"]
        tick.time = tick.time - (tick.time % 86400) + 86400 + 9 * 3600
        fake_mt5.rates["EURUSD"]["time"][-1] = tick.time - 60
        fake_mt5.advance_bar("EURUSD")
        tick.time = int(fake_mt5.rates["EURUSD"]["time"][-1]) + 60
        bot.run_once()
        assert not bot.state.halted
        assert len(opening_sends(fake_mt5)) == 1

    def test_halt_survives_restart(self, make_bot, fake_mt5: FakeMT5, make_cfg) -> None:
        cfg = make_cfg()
        bot = started(make_bot(cfg=cfg, strategy=always("BUY")))
        fake_mt5.account.equity = 9_000.0
        bot.run_once()
        assert bot.state.halted
        restarted = started(make_bot(cfg=cfg, strategy=always("BUY")))
        fake_mt5.advance_bar("EURUSD")
        restarted.run_once()
        assert restarted.state.halted and fake_mt5.sends == []

    def test_runtime_switch_to_real_account_stops(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_REAL
        assert bot.run_once() == mt5_bot.EXIT_SAFETY_STOP

    def test_runtime_account_change_stops(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.account.login = 11111111
        assert bot.run_once() == mt5_bot.EXIT_SAFETY_STOP

    def test_reconnect_with_exponential_backoff(self, fake_mt5: FakeMT5, make_cfg, waits: list[float]) -> None:
        clock = [0.0]
        bot = mt5_bot.Bot(make_cfg(), api=fake_mt5, wait=waits.append, env={}, monotonic=lambda: clock[0])
        bot.startup()
        fake_mt5.connected = False
        fake_mt5.init_ok = False
        attempt_times: list[float] = []
        original_init = fake_mt5.initialize

        def tracking_init(**kwargs: Any) -> bool:
            attempt_times.append(clock[0])
            return original_init(**kwargs)

        fake_mt5.initialize = tracking_init  # type: ignore[method-assign]
        while clock[0] <= 400:
            assert bot.run_once() is None
            clock[0] += 1.0
        gaps = [b - a for a, b in zip(attempt_times, attempt_times[1:])]
        assert attempt_times[0] == 5.0
        assert gaps == [10.0, 20.0, 40.0, 80.0, 120.0, 120.0]  # capped at 120s

        fake_mt5.connected = True
        fake_mt5.init_ok = True
        assert bot.run_once() is None
        assert bot._reconnect_delay is None

    def test_stop_request_ends_run(self, make_bot) -> None:
        bot = make_bot()
        bot.startup()
        bot.request_stop(2)
        assert bot.run() == mt5_bot.EXIT_OK

    def test_heartbeat_logged(self, make_bot, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="mt5_bot")
        started(make_bot())
        assert "Heartbeat: equity 10000.00 USD" in caplog.text


def test_server_datetime_is_server_wall_clock() -> None:
    dt = mt5_bot.server_datetime(1_760_000_400)
    assert (dt.year, dt.month, dt.day, dt.hour) == (2025, 10, 9, 9)
    assert math.isclose(dt.timestamp(), 1_760_000_400)
