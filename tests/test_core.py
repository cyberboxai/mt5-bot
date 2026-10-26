"""Core behaviour carried over from v1: indicators, sizing, config, startup, entries, guards."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from conftest import BAR_SECONDS, FakeMT5, FixedSource
from mt5bot import bot as botmod
from mt5bot import cli, limits
from mt5bot.bot import StartupError, select_filling_mode
from mt5bot.config import Config, in_trading_hours
from mt5bot.indicators import atr, ema, rsi, rolling_max_prev, rolling_percentile_rank
from mt5bot.market import server_datetime
from mt5bot.risk import EquityGuard, lots_for_risk
from mt5bot.strategies.base import Signal

PROJECT_DIR = Path(__file__).resolve().parent.parent


def started(bot: botmod.Bot) -> botmod.Bot:
    """Start the bot and consume the first loop, which only records the current bar."""
    bot.startup()
    assert bot.run_once() is None
    return bot


def opening_sends(fake: FakeMT5) -> list[dict[str, Any]]:
    return [r for r in fake.sends if "position" not in r and r.get("action") == fake.TRADE_ACTION_DEAL
            and r.get("sl")]


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
        out = atr(high, low, close, 2)
        assert np.isnan(out[:2]).all()
        assert out[2] == pytest.approx((2.5 + 2) / 2)
        assert out[3] == pytest.approx((2.25 * 1 + 2) / 2)
        assert out[4] == pytest.approx((2.125 * 1 + 4) / 2)

    def test_atr_length_mismatch(self) -> None:
        with pytest.raises(ValueError):
            atr([1, 2], [1], [1, 2], 1)

    def test_rsi_extremes(self) -> None:
        assert rsi(np.arange(30, dtype=float), 14)[-1] == 100.0
        assert rsi(np.arange(30, 0, -1, dtype=float), 14)[-1] == 0.0

    def test_rolling_max_prev_excludes_current(self) -> None:
        out = rolling_max_prev([1, 5, 2, 9, 3], 2)
        assert np.isnan(out[:2]).all()
        assert list(out[2:]) == [5, 5, 9]

    def test_percentile_rank(self) -> None:
        out = rolling_percentile_rank(np.arange(10, dtype=float), 5)
        assert out[-1] == 100.0
        assert np.isnan(out[:4]).all()


# -------------------------------------------------------------------------- sizing


class TestLotsForRisk:
    def test_normal_case(self) -> None:
        assert lots_for_risk(50, 210, 0.01, 100, 0.01) == 0.23

    def test_step_point_one_rounds_down(self) -> None:
        assert lots_for_risk(50, 210, 0.1, 100, 0.1) == 0.2

    def test_no_float_artifacts(self) -> None:
        assert lots_for_risk(0.3, 1, 0.1, 100, 0.1) == 0.3

    def test_below_minimum_returns_zero(self) -> None:
        assert lots_for_risk(1, 210, 0.01, 100, 0.01) == 0.0

    def test_capped_at_maximum(self) -> None:
        assert lots_for_risk(1_000_000, 10, 0.01, 50, 0.01) == 50.0

    @pytest.mark.parametrize("args", [
        (0, 210, 0.01, 100, 0.01), (-50, 210, 0.01, 100, 0.01), (50, 0, 0.01, 100, 0.01),
        (50, 210, 0.01, 100, 0), (50, 210, 0, 100, 0.01), (50, 210, 1, 0.5, 0.01),
        (float("nan"), 210, 0.01, 100, 0.01), (float("inf"), 210, 0.01, 100, 0.01),
    ])
    def test_invalid_inputs_return_zero(self, args: tuple) -> None:
        assert lots_for_risk(*args) == 0.0

    @pytest.mark.parametrize("risk", [7.3, 13.37, 49.99, 120.5])
    @pytest.mark.parametrize("loss", [37.1, 99.9, 210.0])
    def test_never_exceeds_risk(self, risk: float, loss: float) -> None:
        lots = lots_for_risk(risk, loss, 0.01, 100, 0.01)
        assert lots * loss <= risk + 1e-9

    def test_filling_mode_selection(self, fake_mt5: FakeMT5) -> None:
        assert select_filling_mode(3, fake_mt5) == fake_mt5.ORDER_FILLING_IOC
        assert select_filling_mode(1, fake_mt5) == fake_mt5.ORDER_FILLING_FOK
        assert select_filling_mode(0, fake_mt5) == fake_mt5.ORDER_FILLING_RETURN


# -------------------------------------------------------------------------- config


class TestConfig:
    def test_shipped_config_is_valid_and_safe(self) -> None:
        cfg = Config.load(PROJECT_DIR / "config.json")
        assert cfg.symbols == ("EURUSD",)
        assert cfg.account_mode == "any"
        assert cfg.real_money_confirmed is False
        assert cfg.risk_profile == "balanced"
        from mt5bot.profiles import effective_limits

        for real in (False, True):
            lim = effective_limits(cfg, real)
            assert lim.t3_risk_pct <= limits.MAX_RISK_PER_TRADE_PCT and lim.t1_risk_pct <= lim.t2_risk_pct
            assert lim.daily_loss_pct <= limits.MAX_DAILY_LOSS_PCT
            assert lim.explore_day_pct <= limits.MAX_EXPLORATION_DAY_PCT
        assert effective_limits(cfg, True).t1_risk_pct < effective_limits(cfg, False).t1_risk_pct

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

    def test_removed_v1_keys_explain_replacement(self) -> None:
        with pytest.raises(ValueError, match="account_mode"):
            Config.from_dict({"allow_live": True})
        with pytest.raises(ValueError, match="risk_per_trade_pct_demo"):
            Config.from_dict({"risk_per_trade_pct": 0.5})

    def test_invalid_json_and_missing_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            Config.load(bad)
        with pytest.raises(ValueError, match="Cannot read"):
            Config.load(tmp_path / "missing.json")

    @pytest.mark.parametrize("overrides", [
        {"symbols": []}, {"symbols": [""]}, {"symbols": ["EURUSD", "EURUSD"]}, {"timeframe": "D1"},
        {"trading_hours_server": [8, 8]}, {"trading_hours_server": [-1, 10]}, {"trading_hours_server": [7]},
        {"bars": 100}, {"bars": True}, {"magic": 0}, {"magic": "123"},
        {"account_mode": "live"}, {"real_money_confirmed": "yes"}, {"real_money_confirmed": 1},
        {"risk_per_trade_pct_demo": 0}, {"max_open_positions_total": 0}, {"risk_profile": "yolo"},
        {"tiers": {"t2_enter_p": 0.5}}, {"tiers": {"exploration_day_pct": 5.0}}, {"universe": {"timeframes": ["D1"]}},
        {"capital": {"lock_fraction": 0.99}}, {"universe": {"max_spread_points_by_symbol": {"EURUSD": 0}}},
        {"deviation_points": -1}, {"poll_seconds": 0}, {"heartbeat_minutes": 0}, {"kill_switch_file": ""},
        {"mt5_path": ""}, {"enabled_strategies": ["martingale"]}, {"holidays": ["2025/12/25"]},
        {"tiers": {"t1_exit_p": 0.6}}, {"history_months": 3},
    ])
    def test_invalid_values_raise(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            Config.from_dict(overrides)

    def test_config_is_frozen(self) -> None:
        cfg = Config()
        with pytest.raises(AttributeError):
            cfg.real_money_confirmed = True  # type: ignore[misc]

    @pytest.mark.parametrize("hour,window,expected", [
        (7, (7, 20), True), (19, (7, 20), True), (20, (7, 20), False), (6, (7, 20), False),
        (23, (22, 6), True), (3, (22, 6), True), (6, (22, 6), False), (0, (0, 24), True),
    ])
    def test_in_trading_hours(self, hour: int, window: tuple[int, int], expected: bool) -> None:
        assert in_trading_hours(hour, window) is expected


# ------------------------------------------------------------------ daily guard


class TestDailyGuard:
    def test_persists_across_instances(self, tmp_path: Path) -> None:
        guard = EquityGuard(tmp_path)
        assert "new_day" in guard.update("2025-10-09", 10_000.0, 2.0, 12.0)
        again = EquityGuard(tmp_path)
        assert again.state.day == "2025-10-09"
        assert again.state.start_equity == 10_000.0
        assert again.update("2025-10-09", 9_900.0, 2.0, 12.0) == []
        assert again.state.start_equity == 10_000.0  # a restart must not reset the day's baseline

    def test_daily_limit_halts_and_persists(self, tmp_path: Path) -> None:
        guard = EquityGuard(tmp_path)
        guard.update("2025-10-09", 10_000.0, 2.0, 12.0)
        assert guard.update("2025-10-09", 9_801.0, 2.0, 12.0) == []
        assert guard.update("2025-10-09", 9_800.0, 2.0, 12.0) == ["daily_limit"]
        assert EquityGuard(tmp_path).state.daily_halted is True

    def test_resets_on_new_day(self, tmp_path: Path) -> None:
        guard = EquityGuard(tmp_path)
        guard.update("2025-10-09", 10_000.0, 2.0, 12.0)
        guard.update("2025-10-09", 9_700.0, 2.0, 12.0)
        assert guard.state.daily_halted
        assert "new_day" in guard.update("2025-10-10", 9_700.0, 2.0, 12.0)
        assert guard.state.daily_halted is False
        assert guard.state.start_equity == 9_700.0

    @pytest.mark.parametrize("content", ["{broken", "[]", '{"day": 1}', '{"start_equity": "a"}',
                                         '{"daily_halted": "no"}', '{"unknown": 1}'])
    def test_corrupt_file_refuses(self, tmp_path: Path, content: str) -> None:
        (tmp_path / "risk_state.json").write_text(content)
        with pytest.raises(ValueError, match="corrupt"):
            EquityGuard(tmp_path)


# ----------------------------------------------------------------- startup checks


class TestStartup:
    def test_demo_account_starts(self, make_bot) -> None:
        make_bot().startup()

    @pytest.mark.parametrize("breakage,message", [
        (lambda f: setattr(f, "init_ok", False), "initialize"),
        (lambda f: setattr(f.terminal, "trade_allowed", False), "Algo Trading"),
        (lambda f: setattr(f.account, "trade_allowed", False), "not allowed"),
        (lambda f: setattr(f.account, "trade_expert", False), "expert"),
        (lambda f: f.symbols.clear(), "not found"),
    ])
    def test_actionable_startup_failures(self, make_bot, fake_mt5: FakeMT5, breakage, message: str) -> None:
        breakage(fake_mt5)
        with pytest.raises(StartupError, match=message):
            make_bot().startup()

    def test_waits_for_connection_then_fails(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.terminal.connected = False
        clock = [0.0]
        bot = make_bot(monotonic=lambda: clock[0])
        bot._wait = lambda s: clock.__setitem__(0, clock[0] + s)  # type: ignore[method-assign]
        with pytest.raises(StartupError, match="not connected"):
            bot.startup()
        assert clock[0] >= botmod.CONNECT_WAIT_SECONDS

    def test_env_credentials_used_and_never_logged(self, make_bot, fake_mt5: FakeMT5,
                                                   caplog: pytest.LogCaptureFixture) -> None:
        env = {"MT5_LOGIN": "87654321", "MT5_PASSWORD": "s3cr3t-P@ss", "MT5_SERVER": "Broker-Demo"}
        caplog.set_level(logging.DEBUG, logger="mt5_bot")
        make_bot(env=env, mt5_path=r"C:\MT5\terminal64.exe").startup()
        call = fake_mt5.init_calls[-1]
        assert call["path"] == r"C:\MT5\terminal64.exe"
        assert (call["login"], call["password"], call["server"]) == (87654321, "s3cr3t-P@ss", "Broker-Demo")
        assert "s3cr3t-P@ss" not in caplog.text
        assert "87654321" not in caplog.text  # login is masked

    def test_partial_env_credentials_rejected(self, make_bot) -> None:
        with pytest.raises(StartupError, match="all three"):
            make_bot(env={"MT5_LOGIN": "1"}).startup()

    def test_discovers_terminal_when_not_running(self, fake_mt5: FakeMT5, make_cfg, waits, tmp_path) -> None:
        calls: list[dict] = []

        def initialize(**kwargs: Any) -> bool:
            calls.append(kwargs)
            return "path" in kwargs  # attaching fails, launching from a path works

        fake_mt5.initialize = initialize  # type: ignore[method-assign]
        found = [r"C:\Program Files\Other MT5\terminal64.exe", r"C:\Program Files\STARTRADER MT5\terminal64.exe"]
        bot = botmod.Bot(make_cfg(), api=fake_mt5, wait=waits.append, env={"ProgramFiles": r"C:\Program Files"},
                         signal_source=FixedSource(), globber=lambda pattern: found if "Program Files\\" in pattern
                         or "Program Files/" in pattern else [])
        bot.startup()
        assert "path" not in calls[0]
        assert calls[1]["path"].endswith(r"STARTRADER MT5\terminal64.exe")  # STARTRADER preferred
        bot.close()

    def test_symbol_suffix_auto_detected(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.symbols.clear()
        fake_mt5.add_symbol("EURUSD.m")
        bot = make_bot(symbols=["EURUSD"])
        bot.startup()
        assert bot.symbols == {"EURUSD": "EURUSD.m"}

    def test_main_returns_2_on_startup_failure(self, tmp_path: Path) -> None:
        # Uses the module-level stub, whose initialize() always fails.
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "log_file": str(tmp_path / "bot.log"), "state_root": str(tmp_path / "state"),
            "kill_switch_file": str(tmp_path / "KILL"), "health_file": str(tmp_path / "health.json"),
        }))
        try:
            assert cli.main(["--config", str(cfg_path), "--dry-run"]) == botmod.EXIT_STARTUP
        finally:
            for handler in list(cli.log.handlers):
                handler.close()
                cli.log.removeHandler(handler)
            cli.log.propagate = True
        assert "initialize() failed" in (tmp_path / "bot.log").read_text(encoding="utf-8")

    def test_main_returns_2_on_config_error(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"timeframe": "W1"}))
        assert cli.main(["--config", str(cfg_path)]) == botmod.EXIT_STARTUP


# ------------------------------------------------------------------------ trading


class TestEntries:
    def test_opens_with_sl_tp_and_risk_based_volume(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(source=FixedSource("BUY", 0.0010), commission_per_lot=0.0))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        [req] = opening_sends(fake_mt5)
        tick = fake_mt5.ticks["EURUSD"]
        assert req["type"] == fake_mt5.ORDER_TYPE_BUY
        assert req["magic"] == bot.cfg.magic
        assert 0 < req["sl"] < req["price"] < req["tp"]
        assert req["price"] == tick.ask
        assert req["type_filling"] == fake_mt5.ORDER_FILLING_IOC
        # 0.5% of 10k = 50 USD; SL 200 points + 10 points deviation = 210 USD per lot -> 0.23
        assert req["volume"] == 0.23
        worst_loss = -fake_mt5.order_calc_profit(fake_mt5.ORDER_TYPE_BUY, "EURUSD", req["volume"],
                                                 req["price"] + 10 * 0.00001, req["sl"])
        assert worst_loss <= 50.0
        assert len(fake_mt5.checks) == 1  # order_check always precedes order_send

    def test_commission_is_part_of_the_risk(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(source=FixedSource("BUY", 0.0010), commission_per_lot=40.0))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        [req] = opening_sends(fake_mt5)
        assert req["volume"] == 0.2  # 50 / (210 + 40) = 0.2

    def test_entry_is_journaled_as_live_trade(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        [row] = bot.registry.trades(mode="live")
        assert row.status == "open" and row.ticket == fake_mt5.positions[0].ticket
        assert row.risk_money > 0 and row.sl and row.tp

    def test_sell_has_sl_above_and_tp_below(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(source=FixedSource("SELL")))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        [req] = opening_sends(fake_mt5)
        assert req["type"] == fake_mt5.ORDER_TYPE_SELL
        assert req["tp"] < req["price"] < req["sl"]

    def test_dry_run_checks_but_never_sends(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
        bot = started(make_bot(dry_run=True))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.checks, "dry run must still call order_check"
        fake_mt5.add_position("EURUSD", "SELL", magic=510001, sl=1.2, tp=1.0)
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        (tmp_path / "KILL").touch()
        assert bot.run() == botmod.EXIT_OK
        assert fake_mt5.sends == []

    def test_spread_filter_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(universe={"max_spread_points_by_symbol": {"EURUSD": 20}}))
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.ticks["EURUSD"].ask = fake_mt5.ticks["EURUSD"].bid + 0.00050
        bot.run_once()
        assert fake_mt5.checks == [] and fake_mt5.sends == []

    def test_hours_filter_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot(trading_hours_server=[7, 20]))
        fake_mt5.advance_bar("EURUSD")
        tick = fake_mt5.ticks["EURUSD"]
        tick.time = tick.time - (tick.time % 86400) + 3 * 3600  # 03:00 server time
        bot.run_once()
        assert fake_mt5.checks == [] and fake_mt5.sends == []

    def test_restricted_trade_mode_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.symbols["EURUSD"].trade_mode = fake_mt5.SYMBOL_TRADE_MODE_CLOSEONLY
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_stops_level_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.symbols["EURUSD"].trade_stops_level = 500
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_below_min_volume_skips(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.equity = 100.0
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_order_check_failure_skips_send(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.check_retcode = 10019
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(fake_mt5.checks) == 1 and fake_mt5.sends == []

    def test_one_position_per_symbol(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_position("EURUSD", "BUY", magic=510001, sl=1.09, tp=1.12)
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert opening_sends(fake_mt5) == []

    def test_foreign_positions_do_not_block_symbol_on_hedging(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_position("EURUSD", "SELL", magic=999)
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(opening_sends(fake_mt5)) == 1
        assert all(r.get("position") is None for r in fake_mt5.sends)

    def test_max_open_positions_total(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_symbol("GBPUSD", bid=1.3)
        fake_mt5.add_position("GBPUSD", "BUY", magic=510001, price_open=1.3, sl=1.29, tp=1.32)
        bot = started(make_bot(symbols=["EURUSD"], max_open_positions_total=1))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert opening_sends(fake_mt5) == []

    def test_close_on_opposite_then_reverse(self, make_bot, fake_mt5: FakeMT5) -> None:
        old = fake_mt5.add_position("EURUSD", "SELL", magic=510001, sl=1.11, tp=1.09)
        bot = started(make_bot(close_on_opposite=True))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends[0]["position"] == old.ticket
        assert fake_mt5.sends[0]["type"] == fake_mt5.ORDER_TYPE_BUY
        assert fake_mt5.sends[0]["price"] == fake_mt5.ticks["EURUSD"].ask
        assert len(opening_sends(fake_mt5)) == 1

    def test_no_close_on_opposite_when_disabled(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_position("EURUSD", "SELL", magic=510001, sl=1.11, tp=1.09)
        bot = started(make_bot(close_on_opposite=False))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_retries_on_requote_with_fresh_price(self, make_bot, fake_mt5: FakeMT5, waits: list[float]) -> None:
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.send_retcodes = [fake_mt5.TRADE_RETCODE_REQUOTE, fake_mt5.TRADE_RETCODE_DONE]
        original_send = fake_mt5.order_send

        def send_then_move_price(request: dict[str, Any]) -> Any:
            result = original_send(request)
            fake_mt5.set_tick("EURUSD", bid=1.10020)
            return result

        fake_mt5.order_send = send_then_move_price  # type: ignore[method-assign]
        bot.run_once()
        assert len(fake_mt5.sends) == 2
        first, second = fake_mt5.sends
        assert second["price"] == pytest.approx(1.10030)
        assert second["price"] - second["sl"] == pytest.approx(first["price"] - first["sl"])
        assert second["volume"] == first["volume"]
        assert botmod.RETRY_PAUSE_SECONDS in waits

    def test_gives_up_after_three_requotes(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.send_retcodes = [fake_mt5.TRADE_RETCODE_REQUOTE] * 5
        bot.run_once()
        assert len(fake_mt5.sends) == botmod.MAX_SEND_ATTEMPTS

    def test_does_not_retry_other_retcodes(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.send_retcodes = [fake_mt5.TRADE_RETCODE_NO_MONEY]
        bot.run_once()
        assert len(fake_mt5.sends) == 1

    def test_market_closed_backs_off(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        fake_mt5.send_retcodes = [fake_mt5.TRADE_RETCODE_MARKET_CLOSED]
        bot.run_once()
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(fake_mt5.sends) == 1  # no second attempt while the market reported closed

    def test_slippage_above_limit_pauses_entries(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.fill_slippage = 0.00050  # 50 points
        bot = started(make_bot(max_slippage_points=20, close_on_opposite=False))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(opening_sends(fake_mt5)) == 1
        fake_mt5.positions.clear()
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(opening_sends(fake_mt5)) == 1

    def test_exception_in_one_symbol_is_isolated(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.add_symbol("GBPUSD", bid=1.3)
        bot = started(make_bot(symbols=["EURUSD", "GBPUSD"]))
        fake_mt5.rates_error["EURUSD"] = RuntimeError("boom")
        fake_mt5.advance_bar("GBPUSD")
        assert bot.run_once() is None
        assert [r["symbol"] for r in opening_sends(fake_mt5)] == ["GBPUSD"]

    def test_only_closed_bars_reach_the_signal_source(self, make_bot, fake_mt5: FakeMT5) -> None:
        source = FixedSource(None)
        bot = started(make_bot(source=source))
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        forming = fake_mt5.rates["EURUSD"]["time"][-1]
        assert source.seen[-1]["time"][-1] == forming - BAR_SECONDS
        assert forming not in source.seen[-1]["time"]
        assert len(source.seen[-1]) == bot.cfg.bars

    def test_acts_once_per_closed_bar(self, make_bot, fake_mt5: FakeMT5) -> None:
        source = FixedSource(None)
        bot = started(make_bot(source=source))
        calls = len(source.seen)
        bot.run_once()
        bot.run_once()
        assert len(source.seen) == calls
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        bot.run_once()
        assert len(source.seen) == calls + 1


class TestNetting:
    def test_netting_close_is_opposite_deal_without_position_ticket(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.margin_mode = fake_mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING
        mine = fake_mt5.add_position("EURUSD", "SELL", magic=510001, sl=1.11, tp=1.09)
        bot = started(make_bot())
        assert bot.hedging is False
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        close = fake_mt5.sends[0]
        assert "position" not in close and close["type"] == fake_mt5.ORDER_TYPE_BUY
        assert close["volume"] == mine.volume
        assert mine not in fake_mt5.positions

    def test_netting_foreign_position_blocks_entry(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.margin_mode = fake_mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING
        fake_mt5.add_position("EURUSD", "SELL", magic=999, sl=1.11, tp=1.09)
        bot = started(make_bot())
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert fake_mt5.sends == []

    def test_netting_sl_repair_uses_sltp_action(self, make_bot, fake_mt5: FakeMT5) -> None:
        fake_mt5.account.margin_mode = fake_mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING
        pos = fake_mt5.add_position("EURUSD", "BUY", magic=510001, price_open=1.1)
        make_bot().startup()
        [req] = fake_mt5.sends
        assert req["action"] == fake_mt5.TRADE_ACTION_SLTP and req["position"] == pos.ticket
        assert req["symbol"] == "EURUSD"


# ---------------------------------------------------------------- guards and ops


class TestGuards:
    def test_kill_switch_closes_only_bot_positions_and_exits(self, make_bot, fake_mt5: FakeMT5,
                                                              tmp_path: Path) -> None:
        mine = fake_mt5.add_position("EURUSD", "BUY", magic=510001, sl=1.09, tp=1.12)
        foreign = fake_mt5.add_position("EURUSD", "SELL", magic=777)
        bot = make_bot()
        bot.startup()
        (tmp_path / "KILL").touch()
        assert bot.run() == botmod.EXIT_OK
        assert [r["position"] for r in fake_mt5.sends] == [mine.ticket]
        assert fake_mt5.positions == [foreign]

    def test_kill_switch_reports_failure_if_close_fails(self, make_bot, fake_mt5: FakeMT5, tmp_path: Path) -> None:
        fake_mt5.add_position("EURUSD", "BUY", magic=510001, sl=1.09, tp=1.12)
        fake_mt5.send_retcodes = [10018]
        bot = make_bot()
        bot.startup()
        (tmp_path / "KILL").touch()
        assert bot.run() == botmod.EXIT_ERROR

    def test_daily_loss_closes_bot_positions_and_halts(self, make_bot, fake_mt5: FakeMT5) -> None:
        mine = fake_mt5.add_position("EURUSD", "BUY", magic=510001, sl=1.09, tp=1.12)
        foreign = fake_mt5.add_position("EURUSD", "BUY", magic=42)
        bot = started(make_bot(source=FixedSource("SELL"), close_on_opposite=False, max_daily_loss_pct_demo=2.0))
        assert bot.guard.state.start_equity == 10_000.0
        fake_mt5.account.equity = 9_790.0  # -2.1%
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert bot.guard.state.daily_halted
        assert [r.get("position") for r in fake_mt5.sends] == [mine.ticket]
        assert fake_mt5.positions == [foreign]
        sends_before = len(fake_mt5.sends)
        fake_mt5.advance_bar("EURUSD")
        bot.run_once()
        assert len(fake_mt5.sends) == sends_before  # halted: no new entries

    def test_halt_survives_restart(self, make_bot, fake_mt5: FakeMT5, make_cfg) -> None:
        cfg = make_cfg()
        bot = started(make_bot(cfg=cfg))
        fake_mt5.account.equity = 9_000.0
        bot.run_once()
        assert bot.guard.halted_reason
        bot.close()
        restarted = started(make_bot(cfg=cfg))
        fake_mt5.advance_bar("EURUSD")
        restarted.run_once()
        assert restarted.guard.halted_reason and opening_sends(fake_mt5) == []

    def test_runtime_switch_to_real_account_stops(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.account.trade_mode = fake_mt5.ACCOUNT_TRADE_MODE_REAL
        assert bot.run_once() == botmod.EXIT_SAFETY_STOP

    def test_runtime_account_change_stops(self, make_bot, fake_mt5: FakeMT5) -> None:
        bot = started(make_bot())
        fake_mt5.account.login = 11111111
        assert bot.run_once() == botmod.EXIT_SAFETY_STOP

    def test_reconnect_with_exponential_backoff(self, make_bot, fake_mt5: FakeMT5) -> None:
        clock = [0.0]
        bot = make_bot(monotonic=lambda: clock[0])
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
        assert gaps == [10.0, 20.0, 40.0, 80.0, 120.0, 120.0]
        fake_mt5.connected = True
        fake_mt5.init_ok = True
        assert bot.run_once() is None
        assert bot._reconnect_delay is None

    def test_stop_request_ends_run(self, make_bot) -> None:
        bot = make_bot()
        bot.startup()
        bot.request_stop(2)
        assert bot.run() == botmod.EXIT_OK

    def test_heartbeat_logged(self, make_bot, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="mt5_bot")
        started(make_bot())
        assert "Heartbeat: equity 10000.00 USD" in caplog.text


def test_server_datetime_is_server_wall_clock() -> None:
    dt = server_datetime(1_760_000_400)
    assert (dt.year, dt.month, dt.day, dt.hour) == (2025, 10, 9, 9)
    assert math.isclose(dt.timestamp(), 1_760_000_400)


def test_signal_validation() -> None:
    with pytest.raises(ValueError):
        Signal("HOLD", 0.001)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Signal("BUY", 0.0)
    with pytest.raises(ValueError):
        Signal("BUY", float("nan"))
    with pytest.raises(ValueError):
        Signal("BUY", 0.001, sl_mult=limits.MAX_SL_ATR_MULT + 1)
