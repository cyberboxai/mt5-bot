"""Bayesian evidence for arms (numpy + stdlib only).

An arm's true expectancy per trade, in R, is ``theta``. Its evidence is a weighted sample of
R-multiples:

* walk-forward out-of-sample trades from research, each weighted ``prior_discount``
  (default 0.3): history is informative but optimistic, so it counts less than live data;
* shadow trades (every signal, paper traded with the backtester's fill and cost rules);
* live trades (replacing the shadow trade of the same signal when one exists).

Arms are pooled by strategy family. Within a family the model is hierarchical:

    theta_i ~ N(mu_family, tau^2)        mu_family ~ N(global_mean, global_mean_sd^2)
    sample mean of arm i ~ N(theta_i, sigma_i^2 / w_i)

``tau`` is estimated from the data (DerSimonian-Laird) but never below ``tau_min``, and the
family mean is itself shrunk toward a slightly negative global mean (costs make the average
trading idea lose a little). With little data every arm sits at the family mean; an arm only
separates from the pack when its own evidence is strong. This is the protection against the
winner's curse: with hundreds of arms, some look good by luck.

The family prior of each arm is estimated LEAVE-ONE-OUT, from the other arms of its family:
otherwise a lucky arm would inflate the family's dispersion with its own outlier and so
reduce its own shrinkage (the classic way empirical Bayes lets noise through).

The posterior of ``theta_i`` is normal; ``P(E > 0)``, the credible interval and Thompson
samples come from it. Nothing here can promise a profit: these are estimates from past data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Hashable, Iterable, Mapping, Optional, Sequence

import numpy as np

_N = NormalDist()


@dataclass(frozen=True)
class EvidenceSettings:
    prior_discount: float = 0.3
    shadow_weight: float = 1.0
    live_weight: float = 1.0
    global_mean: float = -0.03
    global_mean_sd: float = 0.05
    tau_min: float = 0.05
    tau_max: float = 0.50
    sigma2_prior: float = 1.2
    sigma2_strength: float = 10.0
    credible: float = 0.90
    min_family_arms: int = 3
    min_arm_weight: float = 10.0     # arms with less evidence do not inform the dispersion estimate
    tau_pseudo_arms: float = 10.0    # the tau estimate is blended with tau_min as if this many arms said so


@dataclass(frozen=True)
class ArmSample:
    """Sufficient statistics of one arm's weighted R observations."""

    n_obs: int = 0           # shadow + live trades actually observed (the prior is not counted)
    n_prior: int = 0         # walk-forward trades behind the prior
    live_n: int = 0
    w: float = 0.0           # effective sample size (sum of weights)
    s1: float = 0.0          # sum w * r
    s2: float = 0.0          # sum w * r^2
    dd_r: float = 0.0        # max drawdown in R of the observed (shadow/live) sequence
    last_r: Optional[float] = None

    @property
    def mean(self) -> float:
        return self.s1 / self.w if self.w > 0 else 0.0

    @property
    def var(self) -> float:
        if self.w <= 0:
            return 0.0
        return max(self.s2 / self.w - self.mean ** 2, 0.0)


def max_drawdown_r(rs: Sequence[float]) -> float:
    if not len(rs):
        return 0.0
    curve = np.cumsum(np.asarray(rs, dtype=float))
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return float(np.max(peak - curve))


def build_sample(prior_n: int, prior_mean: Optional[float], prior_var: Optional[float], observed: Sequence[float],
                 live_flags: Optional[Sequence[bool]] = None,
                 settings: EvidenceSettings = EvidenceSettings(),
                 weights: Optional[Sequence[float]] = None) -> ArmSample:
    """Combine the discounted walk-forward prior with the observed R sequence (oldest first).

    ``weights`` (one per observation) replace the default live/shadow weights, e.g. to let
    old evidence fade; the drawdown and counts always use the raw sequence."""
    w = s1 = s2 = 0.0
    if prior_n > 0 and prior_mean is not None and math.isfinite(prior_mean):
        wp = prior_n * settings.prior_discount
        var = prior_var if prior_var is not None and math.isfinite(prior_var) and prior_var >= 0 else \
            settings.sigma2_prior
        w, s1, s2 = wp, wp * prior_mean, wp * (var + prior_mean ** 2)
    rs = [float(r) for r in observed if r is not None and math.isfinite(float(r))]
    flags = list(live_flags) if live_flags is not None else [False] * len(rs)
    live_n = 0
    custom = list(weights) if weights is not None else None
    for i, (r, live) in enumerate(zip(rs, flags + [False] * (len(rs) - len(flags)))):
        if custom is not None and i < len(custom):
            weight = max(float(custom[i]), 0.0)
        else:
            weight = settings.live_weight if live else settings.shadow_weight
        live_n += int(bool(live))
        w += weight
        s1 += weight * r
        s2 += weight * r * r
    return ArmSample(len(rs), int(prior_n), live_n, w, s1, s2, max_drawdown_r(rs), rs[-1] if rs else None)


@dataclass(frozen=True)
class Posterior:
    mean: float
    sd: float
    p_positive: float
    lo: float
    hi: float
    shrinkage: float        # 1 = fully pooled to the family mean, 0 = own evidence only
    family_mean: float
    tau: float
    n_obs: int
    n_prior: int
    n_eff: float
    sample_mean: float
    sigma: float            # per-trade R standard deviation estimate
    dd_r: float = 0.0
    live_n: int = 0
    family: str = ""

    def to_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        return {k: (round(v, 6) if isinstance(v, float) and math.isfinite(v) else v) for k, v in out.items()}

    def thompson(self, rng: np.random.Generator) -> float:
        return float(rng.normal(self.mean, self.sd))

    @property
    def kelly_fraction(self) -> float:
        """Full-Kelly bet in units of R as a fraction of capital (0 when the mean is not positive)."""
        if self.mean <= 0 or self.sigma <= 0:
            return 0.0
        return self.mean / (self.sigma ** 2 + self.mean ** 2)


@dataclass
class _Family:
    mean: float
    var: float
    tau: float
    arms: int
    q: float = 0.0
    notes: list[str] = field(default_factory=list)


def _sigma2(sample: ArmSample, settings: EvidenceSettings, pooled: float) -> float:
    """Per-arm R variance, shrunk toward the pooled variance (few trades say little about spread)."""
    k = settings.sigma2_strength
    return (sample.w * sample.var + k * pooled) / (sample.w + k) if sample.w + k > 0 else pooled


def _family_fit(xs: np.ndarray, ses2: np.ndarray, settings: EvidenceSettings,
                weights: Optional[np.ndarray] = None) -> _Family:
    """DerSimonian-Laird random-effects fit, mean shrunk toward the global prior.

    The between-arm dispersion tau is estimated only from arms with at least
    ``min_arm_weight`` of evidence, then blended with ``tau_min`` as if ``tau_pseudo_arms``
    arms reported it: with thin data tau stays near its floor instead of jumping around (an
    unstable tau would make tiers flap)."""
    k = xs.size
    g_mean, g_var = settings.global_mean, settings.global_mean_sd ** 2
    if k == 0:
        return _Family(g_mean, g_var, settings.tau_min, 0)
    tau2 = settings.tau_min ** 2
    informative = np.ones(k, dtype=bool) if weights is None else weights >= settings.min_arm_weight
    ki = int(informative.sum())
    if ki >= settings.min_family_arms:
        xi, si = xs[informative], ses2[informative]
        w = 1.0 / si
        mu_fe = float(np.sum(w * xi) / np.sum(w))
        q = float(np.sum(w * (xi - mu_fe) ** 2))
        denom = float(np.sum(w) - np.sum(w * w) / np.sum(w))
        tau2_dl = max(0.0, (q - (ki - 1)) / denom) if denom > 0 else 0.0
        blended = (ki * tau2_dl + settings.tau_pseudo_arms * settings.tau_min ** 2) / (ki + settings.tau_pseudo_arms)
        tau2 = min(max(blended, settings.tau_min ** 2), settings.tau_max ** 2)
    wr = 1.0 / (ses2 + tau2)
    mu_re = float(np.sum(wr * xs) / np.sum(wr))
    var_re = float(1.0 / np.sum(wr))
    precision = 1.0 / var_re + 1.0 / g_var
    mean = (mu_re / var_re + g_mean / g_var) / precision
    return _Family(mean, 1.0 / precision, math.sqrt(tau2), k)


def fit(samples: Mapping[Hashable, ArmSample], families: Mapping[Hashable, str],
        settings: EvidenceSettings = EvidenceSettings()) -> dict[Hashable, Posterior]:
    """Posterior of every arm's expectancy. Deterministic for the same inputs."""
    z = _N.inv_cdf(0.5 + settings.credible / 2)
    with_data = [s for s in samples.values() if s.w > 0]
    pooled_var = settings.sigma2_prior
    if with_data:
        total = sum(s.w for s in with_data)
        pooled_var = (sum(s.w * s.var for s in with_data) + settings.sigma2_strength * settings.sigma2_prior) / \
            (total + settings.sigma2_strength)
    by_family: dict[str, list[Hashable]] = {}
    for key in samples:
        by_family.setdefault(families.get(key, "default"), []).append(key)
    out: dict[Hashable, Posterior] = {}
    for family, keys in by_family.items():
        data_keys = [k for k in keys if samples[k].w > 0]
        sig2 = {k: _sigma2(samples[k], settings, pooled_var) for k in keys}
        xs = np.array([samples[k].mean for k in data_keys], dtype=float)
        ses2 = np.array([sig2[k] / samples[k].w for k in data_keys], dtype=float)
        ws = np.array([samples[k].w for k in data_keys], dtype=float)
        full = _family_fit(xs, ses2, settings, ws)
        index = {k: i for i, k in enumerate(data_keys)}
        for key in keys:
            s = samples[key]
            fam = full
            if key in index:
                keep = np.arange(len(data_keys)) != index[key]
                fam = _family_fit(xs[keep], ses2[keep], settings, ws[keep])
            tau2 = fam.tau ** 2
            if s.w > 0:
                se2 = sig2[key] / s.w
                b = se2 / (se2 + tau2)
                mean = b * fam.mean + (1.0 - b) * s.mean
                var = 1.0 / (1.0 / se2 + 1.0 / tau2) + b * b * fam.var
            else:
                b, mean, var = 1.0, fam.mean, tau2 + fam.var
            sd = math.sqrt(max(var, 1e-12))
            p = _N.cdf(mean / sd)
            out[key] = Posterior(mean, sd, p, mean - z * sd, mean + z * sd, b, fam.mean, fam.tau, s.n_obs, s.n_prior,
                                 s.w, s.mean, math.sqrt(sig2[key]), s.dd_r, s.live_n, family)
    return out


def family_of(strategy: str) -> str:
    """Arms are pooled by base strategy (a volatility-filtered variant pools with its base)."""
    return strategy[len("volfilter_"):] if strategy.startswith("volfilter_") else strategy


def summarize(posteriors: Iterable[Posterior]) -> dict[str, float]:
    items = list(posteriors)
    if not items:
        return {"arms": 0}
    ps = np.array([p.p_positive for p in items])
    return {"arms": len(items), "p_positive_mean": float(ps.mean()), "p_positive_max": float(ps.max()),
            "with_data": sum(1 for p in items if p.n_obs > 0)}
