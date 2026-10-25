"""Strategy library contract tests. These run for EVERY registered strategy, so a new
strategy file is covered automatically; add strategy-specific behaviour tests below
(see the template at the end of this file).
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from conftest import make_rates, synthetic_bars
from mt5bot import strategies as S
from mt5bot.strategies.base import Param

LIBRARY = S.library_names()
PACKAGE = Path(__file__).resolve().parent.parent / "mt5bot"


@pytest.fixture(scope="module")
def trend_bars() -> np.ndarray:
    return synthetic_bars(2500, seed=7, trend=0.15)


def test_library_contains_required_strategies() -> None:
    for name in ("ema_cross", "donchian", "rsi_reversion", "bollinger", "session_breakout",
                 "volfilter_ema_cross", "volfilter_donchian"):
        assert name in LIBRARY
    assert S.get(S.NO_TRADE) is S.NOTRADE


@pytest.mark.parametrize("name", LIBRARY)
class TestContract:
    def test_deterministic(self, name: str, trend_bars: np.ndarray) -> None:
        strat = S.get(name)
        p = strat.default_params()
        a, b = strat.signals(trend_bars, p), strat.signals(trend_bars.copy(), p)
        np.testing.assert_array_equal(a.side, b.side)
        np.testing.assert_array_equal(np.nan_to_num(a.atr), np.nan_to_num(b.atr))

    def test_produces_signals_on_suitable_data(self, name: str, trend_bars: np.ndarray) -> None:
        strat = S.get(name)
        sides = strat.signals(trend_bars, strat.default_params()).side
        assert (sides != 0).sum() > 0, "the test data should exercise the strategy"
        assert set(np.unique(sides)) <= {-1, 0, 1}

    def test_no_lookahead(self, name: str, trend_bars: np.ndarray) -> None:
        """Signals must be identical when future bars are appended."""
        strat = S.get(name)
        rng = np.random.default_rng(1)
        for p in [strat.default_params()] + strat.sample_params(rng, 3):
            full = strat.signals(trend_bars, p)
            for k in (700, 1100, 1733, 2499):
                prefix = strat.signals(trend_bars[:k], p)
                np.testing.assert_array_equal(prefix.side, full.side[:k], err_msg=f"{name} {p} k={k}")
                sig = strat.signal(trend_bars[:k], p)
                assert (sig is None) == (full.side[k - 1] == 0)
                if sig is not None:
                    assert (1 if sig.side == "BUY" else -1) == full.side[k - 1]
                    assert sig.atr == pytest.approx(full.atr[k - 1])

    def test_parameter_bounds_enforced(self, name: str) -> None:
        strat = S.get(name)
        p = strat.default_params()
        strat.validate_params(p)
        for param in strat.param_space:
            bad = dict(p)
            bad[param.name] = "out-of-range" if not isinstance(param.values[0], str) else 10**6
            with pytest.raises(ValueError):
                strat.validate_params(bad)
            if not isinstance(param.values[0], str):
                above = dict(p)
                above[param.name] = max(param.values) + (param.values[-1] - param.values[0] or 1)
                with pytest.raises(ValueError):
                    strat.validate_params(above)
        with pytest.raises(ValueError):
            strat.validate_params({**p, "extra": 1})
        with pytest.raises(ValueError):
            strat.signals(make_rates(np.full(50, 1.1)), {**p, "extra": 1})

    def test_samples_and_neighbors_stay_inside_bounds(self, name: str) -> None:
        strat = S.get(name)
        rng = np.random.default_rng(3)
        samples = strat.sample_params(rng, 25)
        assert samples and len({tuple(sorted(s.items())) for s in samples}) == len(samples)
        for s in samples:
            strat.validate_params(s)
            for n in strat.neighbors(s):
                strat.validate_params(n)
                assert sum(n[k] != s[k] for k in s) == 1

    def test_sl_tp_multiples_within_hard_limits(self, name: str) -> None:
        from mt5bot import limits

        strat = S.get(name)
        for param in strat.param_space:
            if param.name == "sl_mult":
                assert limits.MIN_SL_ATR_MULT <= min(param.values) and max(param.values) <= limits.MAX_SL_ATR_MULT
            if param.name == "tp_mult":
                assert limits.MIN_TP_ATR_MULT <= min(param.values) and max(param.values) <= limits.MAX_TP_ATR_MULT

    def test_short_input_returns_no_signal(self, name: str) -> None:
        strat = S.get(name)
        bars = make_rates(np.linspace(1.1, 1.2, 30))
        assert (strat.signals(bars, strat.default_params()).side == 0).all()


def test_notrade_never_trades(trend_bars: np.ndarray) -> None:
    assert (S.NOTRADE.signals(trend_bars, {}).side == 0).all()
    assert S.NOTRADE.signal(trend_bars, {}) is None


def test_param_grid_helpers() -> None:
    p = Param.float_range("x", 1.0, 2.0, 0.5)
    assert p.values == (1.0, 1.5, 2.0)
    assert p.neighbors(1.5) == [1.0, 2.0]
    assert Param.int_range("n", 10, 30, 10).neighbors(10) == [20]
    assert not Param.int_range("n", 1, 3).contains(True)


# ----------------------------------------------------------- specific behaviours


def walk(strategy_name: str, bars: np.ndarray, params: dict) -> list[str]:
    strat = S.get(strategy_name)
    return ["BUY" if s > 0 else "SELL" for s in strat.signals(bars, params).side if s != 0]


def test_ema_cross_down_then_up_gives_one_buy() -> None:
    closes = np.concatenate([1.2 - 0.0005 * np.arange(400), 1.0 + 0.0005 * np.arange(1, 401)])
    params = {"fast": 5, "slow": 40, "sl_mult": 2.0, "tp_mult": 3.0}
    assert walk("ema_cross", make_rates(closes), params) == ["BUY"]


def test_ema_cross_flat_series_gives_nothing() -> None:
    params = {"fast": 5, "slow": 40, "sl_mult": 2.0, "tp_mult": 3.0}
    assert walk("ema_cross", make_rates(np.full(400, 1.1)), params) == []


def test_donchian_breakout_once() -> None:
    closes = np.concatenate([np.full(150, 1.1), [1.1050], np.full(30, 1.1050)])
    bars = make_rates(closes, half_range=0.0001)
    assert walk("donchian", bars, {"channel": 20, "sl_mult": 2.0, "tp_mult": 3.0}) == ["BUY"]


def test_session_breakout_respects_server_session() -> None:
    day = 86400 * 20_000  # midnight server time
    n = 96 * 2
    closes = np.full(n, 1.1)
    bars = make_rates(closes, start_time=day, half_range=0.0002)
    # Day 2: range 08:00-09:00, then a breakout close at 10:00.
    i = 96 + 10 * 4
    bars["close"][i] = 1.1010
    bars["high"][i] = 1.1012
    params = {"start_hour": 8, "range_minutes": 60, "window_hours": 3, "sl_mult": 2.0, "tp_mult": 3.0}
    sides = S.get("session_breakout").signals(bars, params).side
    assert list(np.flatnonzero(sides)) == [i] and sides[i] == 1


def test_no_runtime_code_generation_anywhere() -> None:
    """No eval/exec/compile/__import__ and no code download in the package."""
    forbidden = {"eval", "exec", "compile", "__import__"}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):  # the builtins eval(), exec(), compile(), __import__()
                    name = func.id
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                        and func.value.id == "builtins":
                    name = func.attr
                else:
                    continue
                assert name not in forbidden, f"{path.name}: {name}() is forbidden"
        text = path.read_text(encoding="utf-8")
        if path.name != "notify.py":
            assert "urllib" not in text and "http.client" not in text, f"{path.name} must not use the network"
    init = (PACKAGE / "strategies" / "__init__.py").read_text(encoding="utf-8")
    assert 'importlib.import_module(f"{__name__}.{_module}")' in init  # package-local modules only


# ---------------------------------------------------------------------------------
# TEMPLATE for a new strategy (copy, rename and adapt):
#
# def test_my_strategy_signals_on_known_pattern() -> None:
#     bars = make_rates([...closes that should trigger exactly one signal...])
#     params = {"my_param": 10, "sl_mult": 2.0, "tp_mult": 3.0}
#     assert walk("my_strategy", bars, params) == ["BUY"]
#
# The contract tests above (determinism, no lookahead, bounds) already run for it.
# ---------------------------------------------------------------------------------
