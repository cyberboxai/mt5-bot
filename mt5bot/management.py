"""Trade-management policies, scored with evidence like the arms themselves.

Every closed shadow trade of an arm (placed with fixed SL/TP) is replayed under each policy
on the same bars, so policies are compared on PAIRED outcomes: same signals, same fills,
same costs. An arm switches policy only when the paired difference says the new policy
beats the current one with probability >= ``management.policy_min_p_better`` over at least
``management.policy_min_trades`` trades (falling back to the evidence of the same strategy
and timeframe on every symbol while the arm alone has too few). Every policy only ever
tightens a stop or closes early: none of them can add risk.

The replay mirrors live management: decisions are taken when a bar closes and act from the
next bar, the unrealised R uses the price a close would get (bid for BUY, ask for SELL),
and SL/TP inside a bar use the pessimistic order of the backtester.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from .backtest import CostModel, Trade, VirtualPosition, close_position, exit_for_bar, market_exit_price
from .risk import SymbolSpec

POLICIES: tuple[str, ...] = ("fixed", "breakeven", "trail_atr", "time_stop")
POLICY_PT = {"fixed": "SL/TP fixos", "breakeven": "stop no preço de entrada a partir de +1R",
             "trail_atr": "stop móvel a 2 ATR a partir de +1R", "time_stop": "saída ao fim de 24 barras"}
ATR_PERIOD = 14


@dataclass(frozen=True)
class PolicyRules:
    breakeven_after_r: Optional[float] = None
    trail_after_r: Optional[float] = None
    trail_atr_mult: Optional[float] = None
    time_stop_bars: Optional[int] = None


RULES: dict[str, PolicyRules] = {
    "fixed": PolicyRules(),
    "breakeven": PolicyRules(breakeven_after_r=1.0),
    "trail_atr": PolicyRules(trail_after_r=1.0, trail_atr_mult=2.0),
    "time_stop": PolicyRules(time_stop_bars=24),
}


def rules_for(policy: Optional[str]) -> PolicyRules:
    return RULES.get(policy or "fixed", RULES["fixed"])


def improves(side: int, current_sl: float, new_sl: float) -> bool:
    """True when ``new_sl`` reduces risk (moves toward profit). A missing SL is never "improved"
    here: restoring it is the repair path's job."""
    if not current_sl or not math.isfinite(new_sl):
        return False
    return new_sl > current_sl if side > 0 else new_sl < current_sl


def stop_update(rules: PolicyRules, side: int, entry: float, sl: float, unrealized_r: float, close: float,
                atr: float, point: float) -> Optional[tuple[float, str]]:
    """Tighter SL (and why) after a bar closed at ``close``, or None when the policy keeps the stop."""
    candidates: list[tuple[float, str]] = []
    if rules.breakeven_after_r is not None and unrealized_r >= rules.breakeven_after_r:
        candidates.append((entry + side * point, f"break-even after {unrealized_r:.2f}R"))
    if rules.trail_atr_mult is not None and unrealized_r >= (rules.trail_after_r or 0.0) \
            and math.isfinite(atr) and atr > 0:
        candidates.append((close - side * rules.trail_atr_mult * atr, f"trailing {rules.trail_atr_mult} ATR"))
    best: Optional[tuple[float, str]] = None
    for level, why in candidates:
        if improves(side, sl if best is None else best[0], level):
            best = (level, why)
    return best


def time_stop_due(rules: PolicyRules, bars_open: int) -> bool:
    return rules.time_stop_bars is not None and bars_open >= rules.time_stop_bars


def replay(pos: VirtualPosition, bars: np.ndarray, start: int, end: int, exit_price: float, exit_reason: str,
           rules: PolicyRules, atr: np.ndarray, spec: SymbolSpec, costs: CostModel) -> Trade:
    """The trade ``pos`` (entered at the open of ``bars[start]`` and closed by the fixed policy on
    ``bars[end]`` at ``exit_price``) as it would have ended under ``rules``.

    Every policy exits at or before the fixed exit (stops only tighten and the TP is kept), so
    the bars up to ``end`` are all it needs.
    """
    point, slip = spec.point, costs.slippage_points
    risk_dist = abs(pos.entry_price - pos.sl)
    current = pos
    close_next = False
    for i in range(start, end + 1):
        bar = bars[i]
        t = int(bar["time"])
        o, h, lo, spr = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["spread"])
        if close_next:
            return close_position(current, i, t, market_exit_price(current, o, spr, point, slip), "time_stop", spec,
                                  costs)
        if i == end and exit_reason == "opposite":
            return close_position(current, i, t, exit_price, exit_reason, spec, costs)
        hit = exit_for_bar(current, o, h, lo, spr, point, slip, i == start)
        if hit is not None:
            reason = hit[1] if current.sl == pos.sl or hit[1] == "tp" else "managed_sl"
            return close_position(current, i, t, hit[0], reason, spec, costs)
        if i == end:
            break
        # The bar closed: management decides now and acts from the next bar. Bars open counts
        # from the signal bar, as live trades do (their open_bar_time is the signal bar).
        if time_stop_due(rules, i - start + 1):
            close_next = True
            continue
        price = float(bar["close"]) + (spr * point if pos.side < 0 else 0.0)
        unrealized_r = pos.side * (price - pos.entry_price) / risk_dist if risk_dist > 0 else 0.0
        update = stop_update(rules, pos.side, pos.entry_price, current.sl, unrealized_r, float(bar["close"]),
                             float(atr[i]), point)
        if update is not None:
            current = replace(current, sl=update[0])
    bar = bars[end]
    return close_position(current, end, int(bar["time"]), exit_price, exit_reason, spec, costs)


def replay_all(pos: VirtualPosition, bars: np.ndarray, start: int, end: int, exit_price: float, exit_reason: str,
               atr: np.ndarray, spec: SymbolSpec, costs: CostModel) -> dict[str, float]:
    """R of the trade under every policy (``fixed`` replays to the recorded outcome)."""
    return {name: replay(pos, bars, start, end, exit_price, exit_reason, rules, atr, spec, costs).r
            for name, rules in RULES.items()}


# ------------------------------------------------------------------ choice


@dataclass(frozen=True)
class PolicyChoice:
    policy: str
    changed: bool
    reason: str
    n: int
    level: str  # arm | strategy | none


def p_better(a: Sequence[float], b: Sequence[float]) -> float:
    """P(mean(a - b) > 0) from paired outcomes (normal approximation)."""
    d = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    if len(d) < 2:
        return 0.5
    mean = float(d.mean())
    se = float(d.std(ddof=1)) / math.sqrt(len(d))
    if se <= 1e-12:
        return 1.0 if mean > 1e-12 else 0.0 if mean < -1e-12 else 0.5
    return 0.5 * (1.0 + math.erf(mean / se / math.sqrt(2.0)))


def choose(current: str, paired: Mapping[str, Sequence[float]], min_trades: int, min_p: float,
           level: str = "arm") -> PolicyChoice:
    """Keep ``current`` unless another policy beats it with probability >= ``min_p`` (hysteresis:
    a change needs evidence against the incumbent, not just a better point estimate)."""
    current = current if current in POLICIES else "fixed"
    n = len(paired.get("fixed", ()))
    if n < min_trades or any(len(paired.get(p, ())) != n for p in POLICIES):
        return PolicyChoice(current, False, f"{n} paired trades (< {min_trades})", n, "none")
    means = {p: float(np.mean(paired[p])) for p in POLICIES}
    best, why = current, f"{current} kept ({means[current]:+.3f}R per trade over {n})"
    for candidate in sorted(POLICIES, key=lambda p: -means[p]):
        if candidate == best or means[candidate] <= means[best]:
            continue
        p = p_better(paired[candidate], paired[best])
        if p >= min_p:
            why = (f"{candidate} {means[candidate]:+.3f}R vs {best} {means[best]:+.3f}R per trade, "
                   f"P(better) {p:.2f} over {n} {level} trades")
            best = candidate
    return PolicyChoice(best, best != current, why, n, level)


def pooled(paired: Mapping[int, Mapping[str, Sequence[float]]], arm_ids: Iterable[int]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {p: [] for p in POLICIES}
    for arm_id in arm_ids:
        for p in POLICIES:
            out[p].extend(paired.get(arm_id, {}).get(p, ()))
    return out


def choose_for_arms(arms: Sequence[Any], paired: Mapping[int, Mapping[str, Sequence[float]]], min_trades: int,
                    min_p: float) -> dict[int, PolicyChoice]:
    """Policy for every arm: its own paired evidence when it has enough, else the evidence of
    the same strategy and timeframe across symbols (hierarchical fallback)."""
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for arm in arms:
        groups[(arm.strategy, arm.timeframe)].append(arm.id)
    out: dict[int, PolicyChoice] = {}
    for arm in arms:
        own = paired.get(arm.id, {p: [] for p in POLICIES})
        if len(own.get("fixed", ())) >= min_trades:
            out[arm.id] = choose(arm.policy, own, min_trades, min_p, "arm")
        else:
            out[arm.id] = choose(arm.policy, pooled(paired, groups[(arm.strategy, arm.timeframe)]), min_trades,
                                 min_p, "strategy")
    return out
