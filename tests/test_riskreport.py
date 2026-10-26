"""``--risk-report``: the Monte Carlo is honest (zero edge loses a little, never promises gains),
monotone in edge and in profile aggressiveness, and the CLI prints it in PT-PT."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mt5bot import limits, riskreport
from mt5bot.config import Config
from mt5bot.profiles import effective_limits


def lim(profile: str = "balanced", real: bool = False):
    return effective_limits(Config.from_dict({"risk_profile": profile}), real)


def test_zero_edge_loses_and_more_edge_does_better() -> None:
    zero, small, moderate = riskreport.run_report(lim(), days=120, paths=400)
    assert zero.edge_r < 0 < small.edge_r < moderate.edge_r
    assert zero.final_median < 0 and zero.loss_prob > 0.5
    assert zero.final_median < small.final_median < moderate.final_median
    assert moderate.dd_mean <= zero.dd_mean


def test_the_hard_breaker_bounds_every_path() -> None:
    sim = riskreport.simulate(lim("aggressive"), edge_r=-0.3, days=250, paths=300, trades_per_day=6.0,
                              mix={"T3": 1.0})
    assert sim["max_dd"].max() <= limits.MAX_DRAWDOWN_FROM_PEAK_PCT / 100 + 0.03  # one trade of overshoot at most
    assert sim["ruined"].mean() > 0  # a losing aggressive profile can trip the breaker: the report must show it


def test_profiles_are_ordered_by_drawdown() -> None:
    dd = {p: riskreport.run_report(lim(p), days=120, paths=300, mix={"T2": 0.5, "T3": 0.5})[0].dd_mean
          for p in ("conservative", "balanced", "aggressive")}
    assert dd["conservative"] < dd["balanced"] < dd["aggressive"]


def test_real_accounts_are_simulated_with_the_stricter_numbers() -> None:
    demo = riskreport.run_report(lim(real=False), days=120, paths=300)[0]
    real = riskreport.run_report(lim(real=True), days=120, paths=300)[0]
    assert real.dd_mean <= demo.dd_mean


def test_text_is_portuguese_and_never_promises_profit() -> None:
    lims = lim()
    text = riskreport.format_report_pt(lims, riskreport.run_report(lims, days=60, paths=200), 60, 200, 3.0,
                                       riskreport.DEFAULT_MIX)
    assert "ESTIMATIVAS" in text and "não pode garantir lucro" in text and "sem vantagem" in text
    assert "garantido" not in text.replace("não pode garantir", "")


def test_cli_risk_report_runs_offline(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from mt5bot import cli

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"state_root": str(tmp_path / "state"), "health_file": str(tmp_path / "health.json")}))
    assert cli.main(["--config", str(cfg), "--risk-report", "--days", "60", "--paths", "200"]) == 0
    out = capsys.readouterr().out
    assert "Relatório de risco do perfil 'balanced'" in out and "Probabilidade de o disjuntor disparar" in out
