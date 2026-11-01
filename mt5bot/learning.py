"""Continuous learning: drift detection, hierarchical shrinkage and the model registry.

* Recency weighting lives in ``ArmEngine.sample`` (exponential forgetting): everything since day
  one still counts, recent trades count more.
* Hierarchical shrinkage: arms are pooled by strategy family, then, inside each family, by symbol
  cluster and timeframe group, each level shrinking toward the one above.
* Drift: Page-Hinkley or CUSUM on each arm's R sequence. Only a fall is an alarm: an arm getting
  better is promoted by the ordinary evidence, slowly. An alarm demotes the arm one tier at once
  and asks for targeted research.
* Model registry: every research cycle stores the priors it produced with the data snapshot and code
  hash behind them, so any earlier set can be restored.

Detector defaults come from the classic approximation for the mean time between false alarms of
a one-sided test, about ``(exp(a) - a - 1) / (2 * delta^2 / sigma^2)`` with
``a = 2 * delta * lambda / sigma^2``. With sigma about 1.2R, delta 0.15 and lambda 15 give about 600
trades between false alarms, and a fall of 0.5R is detected in about 43 trades.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any, Hashable, Mapping, Optional, Protocol

from .evidence import ArmSample, EvidenceSettings, Posterior, fit

METALS = ("XAU", "XAG", "XPT", "XPD")
CRYPTO = ("BTC", "ETH", "LTC", "XRP", "SOL", "ADA", "DOT", "DOGE")
INDEX_HINTS = ("US30", "US500", "NAS", "DAX", "GER", "UK100", "SPX", "JPN", "HK50", "AUS200", "FRA40")
INTRADAY = ("M1", "M5", "M15", "M30")


def symbol_cluster(symbol: str) -> str:
    name = symbol.upper()
    if name.startswith(METALS):
        return "metal"
    if name.startswith(CRYPTO):
        return "crypto"
    if any(hint in name for hint in INDEX_HINTS):
        return "index"
    return "fx"


def timeframe_group(timeframe: str) -> str:
    return "intraday" if timeframe in INTRADAY else "swing"


def fit_hierarchical(samples: Mapping[Hashable, ArmSample], families: Mapping[Hashable, str],
                     groups: Mapping[Hashable, str], settings: EvidenceSettings = EvidenceSettings()
                     ) -> dict[Hashable, Posterior]:
    """Family level first; each large enough (family, cluster, timeframe) group then shrinks toward
    its family mean. A small group simply keeps the family-level posterior."""
    top = fit(samples, families, settings)
    members: dict[str, list[Hashable]] = {}
    for key in samples:
        members.setdefault(f"{families.get(key, 'default')}|{groups.get(key, '')}", []).append(key)
    out = dict(top)
    for label, keys in members.items():
        if len(keys) < settings.min_family_arms or not any(samples[k].w > 0 for k in keys):
            continue
        anchor = top[keys[0]].family_mean
        scoped = replace(settings, global_mean=anchor)
        inner = fit({k: samples[k] for k in keys}, {k: label for k in keys}, scoped)
        for key in keys:
            out[key] = replace(inner[key], family=families.get(key, "default"))
    return out


# ------------------------------------------------------------------------ drift


class Detector(Protocol):
    n: int

    def update(self, x: float) -> bool: ...


class PageHinkley:
    """Page-Hinkley test for a FALL in the mean of a sequence."""

    def __init__(self, delta: float, threshold: float, min_n: int) -> None:
        self.delta, self.threshold, self.min_n = delta, threshold, min_n
        self.reset()

    def reset(self) -> None:
        self.n, self.mean, self.cum, self.low = 0, 0.0, 0.0, 0.0

    @property
    def statistic(self) -> float:
        return self.cum - self.low

    def update(self, x: float) -> bool:
        self.n += 1
        self.mean += (x - self.mean) / self.n
        self.cum += self.mean - x - self.delta
        self.low = min(self.low, self.cum)
        if self.n >= self.min_n and self.statistic > self.threshold:
            self.reset()
            return True
        return False


class Cusum:
    """One-sided CUSUM for a fall below the mean of the first ``min_n`` observations."""

    def __init__(self, delta: float, threshold: float, min_n: int) -> None:
        self.delta, self.threshold, self.min_n = delta, threshold, min_n
        self.reset()

    def reset(self) -> None:
        self.n, self.mean, self.s = 0, 0.0, 0.0

    @property
    def statistic(self) -> float:
        return self.s

    def update(self, x: float) -> bool:
        self.n += 1
        if self.n <= self.min_n:
            self.mean += (x - self.mean) / self.n
            return False
        self.s = max(0.0, self.s + self.mean - x - self.delta)
        if self.s > self.threshold:
            self.reset()
            return True
        return False


def make_detector(kind: str, delta: float, threshold: float, min_n: int) -> Optional[Detector]:
    if kind == "page_hinkley":
        return PageHinkley(delta, threshold, min_n)
    if kind == "cusum":
        return Cusum(delta, threshold, min_n)
    return None


# ----------------------------------------------------------------- model registry


@dataclass(frozen=True)
class ModelVersion:
    id: int
    ts: float
    kind: str
    data_ref: str
    code_ref: str
    note: str
    arms: int


def snapshot_models(conn: Any, kind: str, data_ref: str, code_ref: str, note: str = "",
                    clock: Any = time.time) -> int:
    """Store every arm's prior and posterior summary. Returns the new version id."""
    rows = conn.execute("SELECT id, symbol, timeframe, strategy, param_set_id, prior_n, prior_mean, prior_var,"
                        " posterior_json, tier FROM arms").fetchall()
    payload = []
    for r in rows:
        try:
            post = json.loads(r["posterior_json"] or "{}")
        except ValueError:
            post = {}
        payload.append({"id": int(r["id"]), "symbol": r["symbol"], "timeframe": r["timeframe"],
                        "strategy": r["strategy"], "param_set_id": int(r["param_set_id"]),
                        "prior_n": int(r["prior_n"] or 0), "prior_mean": r["prior_mean"], "prior_var": r["prior_var"],
                        "mean": post.get("mean"), "sd": post.get("sd"), "p": post.get("p_positive"),
                        "n_obs": post.get("n_obs"), "live_n": post.get("live_n"), "tier": r["tier"]})
    cur = conn.execute("INSERT INTO model_versions (ts, kind, data_ref, code_ref, note, payload_json) VALUES"
                       " (?, ?, ?, ?, ?, ?)", (float(clock()), kind, data_ref, code_ref, note[:300],
                                               json.dumps(payload, sort_keys=True)))
    return int(cur.lastrowid)


def list_models(conn: Any, limit: int = 30) -> list[ModelVersion]:
    rows = conn.execute("SELECT id, ts, kind, data_ref, code_ref, note, payload_json FROM model_versions"
                        " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [ModelVersion(int(r["id"]), float(r["ts"]), r["kind"], r["data_ref"] or "", r["code_ref"] or "",
                         r["note"] or "", len(json.loads(r["payload_json"] or "[]"))) for r in rows]


def restore_models(conn: Any, version_id: int) -> int:
    """Put the priors of a stored version back on the arms. Observed trades are untouched: only what
    research produced is rolled back. Returns the number of arms restored."""
    row = conn.execute("SELECT payload_json FROM model_versions WHERE id = ?", (int(version_id),)).fetchone()
    if row is None:
        raise KeyError(f"model version {version_id} not found")
    restored = 0
    for item in json.loads(row["payload_json"] or "[]"):
        cur = conn.execute("UPDATE arms SET prior_n = ?, prior_mean = ?, prior_var = ? WHERE id = ?",
                           (item["prior_n"], item["prior_mean"], item["prior_var"], item["id"]))
        restored += max(int(cur.rowcount or 0), 0)
    return restored


def evidence_summary(conn: Any, since: float) -> list[dict[str, Any]]:
    """Live and shadow trades per arm since ``since``, for the learning report."""
    rows = conn.execute(
        "SELECT arm_id, mode, COUNT(*) AS n, AVG(r) AS mean_r FROM trades WHERE arm_id IS NOT NULL AND status ="
        " 'closed' AND r IS NOT NULL AND close_ts >= ? GROUP BY arm_id, mode", (float(since),)).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        item = out.setdefault(int(r["arm_id"]), {"arm_id": int(r["arm_id"]), "live": 0, "shadow": 0, "mean_r": None})
        item["live" if r["mode"] == "live" else "shadow"] = int(r["n"])
        item["mean_r"] = round(float(r["mean_r"]), 4) if r["mean_r"] is not None else None
    return sorted(out.values(), key=lambda i: -(i["live"] + i["shadow"]))


