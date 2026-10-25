"""Ablation: replay resolved decision dossiers with each agent (and the advisor) removed,
and compare the full team with the simple baseline (the v2 core: every proposal at base size).

Every resolved dossier carries ``r_base``: the R the champion's proposal actually made
(from its shadow trade, filled by the backtester's rules). Replaying a configuration gives
a multiplier m_i per dossier, so its per-proposal return is ``m_i * r_base_i`` (0 when it
skips). Weights stored in a dossier were learned only from EARLIER outcomes, so the replay
is out of sample. The comparison metric is the per-proposal Sharpe ratio (mean / std of
the return series, skipped trades included as zeros), which uniform resizing cannot fake.

``team_mode: auto`` selects ``full`` only when the paired bootstrap lower bound of
Sharpe(team) - Sharpe(baseline) is above zero at ``ablation_confidence``; otherwise it
stays on (or falls back to) the baseline and says so.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from .ensemble import Constraint, EnsembleSettings, Vote, aggregate, conservative

BOOTSTRAP_SAMPLES = 1000


def sharpe(rs: np.ndarray) -> float:
    rs = np.asarray(rs, dtype=float)
    if rs.size < 2:
        return 0.0
    sd = float(rs.std(ddof=1))
    return float(rs.mean() / sd) if sd > 1e-12 else 0.0


def series_metrics(rs: Sequence[float]) -> dict[str, float]:
    arr = np.asarray(rs, dtype=float)
    curve = np.cumsum(arr)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:] - curve)) if arr.size else 0.0
    taken = arr[arr != 0]
    return {"n": int(arr.size), "trades": int(taken.size), "total_r": round(float(arr.sum()), 4),
            "mean_r": round(float(arr.mean()), 5) if arr.size else 0.0, "sharpe": round(sharpe(arr), 5),
            "max_drawdown_r": round(dd, 4),
            "win_rate": round(float((taken > 0).mean()), 4) if taken.size else None}


def replay_multiplier(dossier: Mapping[str, Any], settings: EnsembleSettings, exclude: Iterable[str] = (),
                      with_advisor: bool = True, include_benched: Iterable[str] = ()) -> float:
    """Recompute the team multiplier of one stored decision without ``exclude`` (and, as a
    counterfactual, with the benched agents in ``include_benched`` voting)."""
    excluded = set(exclude)
    revived = set(include_benched)
    side = 1 if dossier.get("side") == "BUY" else -1
    votes, constraints = [], []
    for op in dossier.get("opinions", []):
        agent = op.get("agent")
        if agent in excluded:
            continue
        if op.get("benched") and agent not in revived:
            continue  # benched agents were observed in shadow only; they did not shape the real decision
        role = op.get("role")
        if role == "voter" and op.get("stance") in ("long", "short", "flat"):
            votes.append(Vote(agent, op["stance"], float(op.get("confidence", 0.0)), float(op.get("weight", 1.0))))
        elif role == "gate":
            if op.get("stance") == "veto":
                constraints.append(Constraint(agent, "veto", 0.0, "replay"))
            elif float(op.get("size_hint", 1.0)) < 1.0:
                constraints.append(Constraint(agent, "size", float(op["size_hint"]), "replay"))
    result = aggregate(side, votes, constraints, settings)
    ensemble = dossier.get("ensemble") or {}
    rules = float(ensemble.get("cio_rule_multiplier", 1.0))
    m = conservative(result.multiplier, rules)
    if with_advisor:
        m = conservative(m, float(dossier.get("advisor_multiplier") if dossier.get("advisor_multiplier")
                                  is not None else 1.0))
    return m


def _paired_bootstrap(a: np.ndarray, b: np.ndarray, confidence: float, seed: int = 20261025) -> tuple[float, float]:
    """Percentile interval of sharpe(a) - sharpe(b) under paired resampling of proposals."""
    rng = np.random.default_rng(seed)
    n = a.size
    diffs = np.empty(BOOTSTRAP_SAMPLES)
    for i in range(BOOTSTRAP_SAMPLES):
        idx = rng.integers(0, n, n)
        diffs[i] = sharpe(a[idx]) - sharpe(b[idx])
    alpha = (1 - confidence) / 2
    return float(np.quantile(diffs, alpha)), float(np.quantile(diffs, 1 - alpha))


def ablation_report(dossiers: Sequence[Mapping[str, Any]], settings: EnsembleSettings, agents: Sequence[str],
                    confidence: float = 0.9, min_samples: int = 50) -> dict[str, Any]:
    rows = [d for d in dossiers if d.get("r_base") is not None and d.get("side") in ("BUY", "SELL")]
    r_base = np.array([float(d["r_base"]) for d in rows], dtype=float)
    out: dict[str, Any] = {"n": len(rows), "min_samples": min_samples, "confidence": confidence}
    if not rows:
        out.update(baseline=series_metrics([]), team=series_metrics([]), agents={}, team_beats_baseline=False,
                   verdict="insufficient data")
        return out
    team_m = np.array([replay_multiplier(d, settings) for d in rows])
    no_adv = np.array([replay_multiplier(d, settings, with_advisor=False) for d in rows])
    team_r, no_adv_r = team_m * r_base, no_adv * r_base
    out["baseline"] = series_metrics(r_base)
    out["team"] = series_metrics(team_r)
    out["team_without_advisor"] = series_metrics(no_adv_r)
    advisor_used = sum(1 for d in rows if d.get("advisor_multiplier") is not None
                       and (d.get("advisor") or {}).get("status") == "ok")
    out["advisor"] = {"reviews": advisor_used,
                      "value_added_sharpe": round(sharpe(team_r) - sharpe(no_adv_r), 5)}
    per_agent: dict[str, Any] = {}
    for agent in agents:
        involved = [i for i, d in enumerate(rows) if any(op.get("agent") == agent for op in d.get("opinions", []))]
        if not involved:
            continue
        without = np.array([replay_multiplier(d, settings, exclude=[agent]) for d in rows]) * r_base
        with_agent = np.array([replay_multiplier(d, settings, include_benched=[agent]) for d in rows]) * r_base
        # Marginal contribution: the team WITH this agent voting (even if it is benched) minus without it.
        value = sharpe(with_agent) - sharpe(without)
        lo, hi = _paired_bootstrap(with_agent, without, confidence) if len(rows) >= 20 else (float("nan"),) * 2
        per_agent[agent] = {"n": len(involved), "value_added_sharpe": round(value, 5),
                            "ci": [None if math.isnan(lo) else round(lo, 5), None if math.isnan(hi) else round(hi, 5)]}
    out["agents"] = per_agent
    if len(rows) >= min_samples:
        lo, hi = _paired_bootstrap(team_r, r_base, confidence)
        out["team_vs_baseline_ci"] = [round(lo, 5), round(hi, 5)]
        out["team_beats_baseline"] = lo > 0
        out["verdict"] = ("the full team beat the baseline out of sample" if lo > 0 else
                          "the full team did NOT beat the simple baseline out of sample")
    else:
        out["team_beats_baseline"] = False
        out["verdict"] = f"insufficient data ({len(rows)}/{min_samples} resolved decisions)"
    return out


def select_mode(configured: str, report: Optional[Mapping[str, Any]], min_samples: int) -> tuple[str, str]:
    """(effective mode, reason). ``auto`` uses the baseline until the team proves itself."""
    if configured in ("full", "baseline"):
        return configured, f"team_mode is \"{configured}\" in config"
    n = int(report.get("n", 0)) if report else 0
    if report is None or n < min_samples:
        return "baseline", f"auto: collecting evidence ({n}/{min_samples} resolved decisions); baseline meanwhile"
    if report.get("team_beats_baseline"):
        return "full", f"auto: {report.get('verdict')}"
    return "baseline", f"auto: {report.get('verdict')}; using the simpler configuration"
