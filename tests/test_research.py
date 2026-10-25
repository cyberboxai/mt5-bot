"""Research and review: vetted hypotheses go through the normal walk-forward gates (and
count as trials), the look-ahead audit catches a leaking strategy, code paths are shared,
the HTML report is written with the honest disclaimer."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from conftest import EURUSD_SPEC, TEST_COSTS, synthetic_bars
from mt5bot import strategies as S
from mt5bot.officedb import OfficeStore
from mt5bot.optimizer import OptimizerSettings, hypothesis_params, optimize_symbol, valid_extra_params
from mt5bot.registry import Registry
from mt5bot.reports import write_html_report
from mt5bot.strategies.base import Param, Strategy
from mt5bot.team.agents.research import code_path_audit, lookahead_audit


def test_hypotheses_become_on_grid_parameter_sets_only() -> None:
    hyps = [{"strategy": "donchian", "param": "channel", "value": 40},
            {"strategy": "donchian", "param": "channel", "value": 41},       # off grid
            {"strategy": "donchian", "param": "os.system", "value": 1},      # unknown parameter
            {"strategy": "no_such_strategy", "param": "x", "value": 1}]
    extra = hypothesis_params(hyps)
    assert list(extra) == ["donchian"]
    valid = valid_extra_params("donchian", extra["donchian"])
    assert len(valid) == 1 and valid[0]["channel"] == 40


def test_hypotheses_are_counted_as_trials_in_the_walk_forward() -> None:
    bars = synthetic_bars(24 * 200, seed=5)
    settings = OptimizerSettings(trials_per_strategy=3, max_trials_per_cycle=10, bar_seconds=3600)
    base = optimize_symbol("EURUSD", bars, EURUSD_SPEC, TEST_COSTS, settings, ["donchian"])
    hypothesis = dict(S.get("donchian").default_params(), channel=90)
    more = optimize_symbol("EURUSD", bars, EURUSD_SPEC, TEST_COSTS, settings, ["donchian"],
                           extra_params={"donchian": [hypothesis, {"channel": 7}]})
    assert more.trials == base.trials + 1  # the invalid one is dropped, the valid one is a full trial
    assert more.candidates[0].metrics["total_trials"] == base.candidates[0].metrics["total_trials"] + 1


class Leaky(Strategy):
    name = "leaky"
    param_space = (Param.int_range("n", 1, 1),)

    def warmup_bars(self, params) -> int:
        return 5

    def compute(self, bars, params):
        close = np.asarray(bars["close"], dtype=float)
        future = np.concatenate([close[1:], close[-1:]])  # peeks at the next bar: look-ahead
        return np.where(future > close, 1, -1).astype(np.int8)


def test_lookahead_audit_catches_a_leaking_strategy(monkeypatch) -> None:
    bars = synthetic_bars(900, seed=2)
    assert lookahead_audit("donchian", S.get("donchian").default_params(), bars) is None
    monkeypatch.setattr(S, "get", lambda name: Leaky() if name == "leaky" else S.registered()[name])
    assert "signals differ" in lookahead_audit("leaky", {"n": 1}, bars)


def test_live_shadow_and_backtest_share_code() -> None:
    assert all(code_path_audit().values())


def test_quant_researcher_hands_hypotheses_to_the_next_cycle(tmp_path: Path) -> None:
    from team_helpers import runtime

    registry = Registry(tmp_path / "registry.sqlite")
    rt = runtime(tmp_path, registry=registry)
    try:
        rt.store.add_hypothesis("advisor", "donchian", "channel", 40, "test")
        rt.agents["quant_researcher"].tick(1e12)
        pending = json.loads(registry.get_kv("research_hypotheses"))
        assert pending[0]["strategy"] == "donchian" and pending[0]["value"] == 40
        assert rt.store.hypotheses()[0]["status"] == "submitted"
    finally:
        rt.close()
        registry.close()


def test_html_report_is_honest_and_escaped(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "registry.sqlite")
    office = OfficeStore(tmp_path / "office.sqlite")
    try:
        tid = registry.open_trade("live", "EURUSD", None, "BUY", 1.1, 1.09, 1.12, 0.1, 50.0, ticket=1)
        registry.close_trade(tid, 1.12, 100.0, 2.0, "<script>tp</script>")
        path = write_html_report(registry, office, tmp_path / "reports", "daily_test", 0, 2e9, "DEMO ***321",
                                 ["EURUSD"])
        text = path.read_text(encoding="utf-8")
        assert "CYBERBOX Trading Office" in text and "não garante lucro" in text
        assert "<script>" not in text and "&lt;script&gt;" in text
        assert "—" not in text
    finally:
        office.close()
        registry.close()
