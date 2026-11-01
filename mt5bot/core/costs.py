"""Transaction costs: calibration from own fills, the cost-to-edge gate, schedule hygiene.

Costs are the main enemy of a retail FX system. Nothing here estimates an edge: it answers
"after what this account really pays, is the evidence for an edge strong enough to trade?".

Cost and edge are compared per lot, so the ratio does not depend on the position size:

    all-in cost per lot = (spread + 2 x slippage) x value of a point + commission + swap
    edge per lot        = posterior lower quantile (in R) x loss per lot at the stop
    ratio               = edge / cost, and an arm may trade only when ratio >= k
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Mapping, Optional, Sequence

DAY = 86400
SESSIONS: tuple[tuple[str, int, int], ...] = (("asia", 0, 7), ("london", 7, 12), ("overlap", 12, 16),
                                              ("new_york", 16, 21), ("off", 21, 24))
PRIOR_STRENGTH = 20.0   # fills an own estimate needs to weigh as much as the prior


def session_of(utc_ts: float) -> str:
    hour = int(utc_ts % DAY) // 3600
    return next(name for name, start, end in SESSIONS if start <= hour < end)


def hour_of(ts: float) -> int:
    return int(ts % DAY) // 3600


@dataclass(frozen=True)
class FillObservation:
    symbol: str
    session: str
    hour: int
    spread_points: float
    slippage_points: float
    latency_ms: float
    rejected: bool = False


@dataclass(frozen=True)
class CostCell:
    """Calibrated friction of one symbol, session and server hour."""

    n: int
    spread_points: float
    spread_p90: float
    slippage_points: float
    slippage_p90: float
    latency_ms: float
    reject_rate: float
    source: str


@dataclass(frozen=True)
class CostPrior:
    spread_points: float
    slippage_points: float
    latency_ms: float = 100.0


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def shrink(own: float, n: int, prior: float, strength: float = PRIOR_STRENGTH) -> float:
    """Blend an own estimate with its prior: no fills means the prior, many fills mean the data."""
    return (n * own + strength * prior) / (n + strength)


def calibrate_cell(observations: Sequence[FillObservation], prior: CostPrior) -> CostCell:
    fills = [o for o in observations if not o.rejected]
    n = len(fills)
    if n == 0:
        rate = shrink(1.0, len(observations), 0.0) if observations else 0.0
        return CostCell(0, prior.spread_points, prior.spread_points * 1.5, prior.slippage_points,
                        prior.slippage_points * 2.0, prior.latency_ms, rate, "prior")
    spreads = [o.spread_points for o in fills]
    slips = [max(o.slippage_points, 0.0) for o in fills]
    return CostCell(n, shrink(median(spreads), n, prior.spread_points),
                    shrink(percentile(spreads, 0.9), n, prior.spread_points * 1.5),
                    shrink(sum(slips) / n, n, prior.slippage_points),
                    shrink(percentile(slips, 0.9), n, prior.slippage_points * 2.0),
                    shrink(median(o.latency_ms for o in fills), n, prior.latency_ms),
                    shrink(1.0 - n / len(observations), len(observations), 0.0),
                    "fills" if n >= PRIOR_STRENGTH else "fills+prior")


def all_in_cost_per_lot(spread_points: float, slippage_points: float, point: float, value_per_price: float,
                        commission_per_lot: float, swap_cost_per_lot_night: float = 0.0,
                        expected_nights: float = 0.0) -> float:
    """Expected money cost of one round turn of 1.0 lot. Slippage is paid on the way in and out."""
    friction = max(spread_points, 0.0) + 2.0 * max(slippage_points, 0.0)
    return (friction * point * value_per_price + max(commission_per_lot, 0.0)
            + max(swap_cost_per_lot_night, 0.0) * max(expected_nights, 0.0))


def cost_to_edge_ratio(edge_lower_r: float, loss_per_lot: float, cost_per_lot: float) -> float:
    """Edge (posterior lower quantile, in money per lot) over all-in cost. <= 0: no proven edge."""
    if cost_per_lot <= 0:
        return math.inf if edge_lower_r > 0 else 0.0
    return edge_lower_r * loss_per_lot / cost_per_lot


def passes_gate(edge_lower_r: float, loss_per_lot: float, cost_per_lot: float, k: float) -> bool:
    return cost_to_edge_ratio(edge_lower_r, loss_per_lot, cost_per_lot) >= k


# ---------------------------------------------------------------- schedule hygiene


@dataclass(frozen=True)
class ScheduleRules:
    rollover_start_minute: int = 23 * 60 + 50     # server minutes after midnight
    rollover_end_minute: int = 20
    friday_cutoff_hour: float = 21.0              # server hour; no new entries from here to the close
    flat_before_weekend: bool = True


def minute_of_day(server_ts: float) -> int:
    return int(server_ts % DAY) // 60


def weekday_of(server_ts: float) -> int:
    return (int(server_ts // DAY) + 3) % 7   # 1970-01-01 was a Thursday; Monday = 0


def in_rollover(server_ts: float, rules: ScheduleRules) -> bool:
    minute = minute_of_day(server_ts)
    start, end = rules.rollover_start_minute, rules.rollover_end_minute
    return start <= minute or minute < end if start > end else start <= minute < end


def pre_weekend(server_ts: float, rules: ScheduleRules) -> bool:
    day = weekday_of(server_ts)
    return day >= 5 or (day == 4 and (server_ts % DAY) / 3600.0 >= rules.friday_cutoff_hour)


def schedule_block(server_ts: float, rules: ScheduleRules) -> Optional[str]:
    """Reason code when no new entry may open now, else None."""
    if in_rollover(server_ts, rules):
        return "ROLLOVER_GUARD"
    if pre_weekend(server_ts, rules):
        return "PRE_WEEKEND"
    return None


def learn_rollover(times: Iterable[int], spreads: Iterable[float], default: ScheduleRules, ratio: float = 2.0,
                   bucket_minutes: int = 10, min_samples: int = 20) -> ScheduleRules:
    """The window around server midnight where spreads run above ``ratio`` x the daily median.

    It can only WIDEN the default guard: a quiet sample must never remove the protection."""
    buckets: dict[int, list[float]] = {}
    for t, s in zip(times, spreads):
        buckets.setdefault(minute_of_day(t) // bucket_minutes, []).append(float(s))
    if not buckets or sum(len(v) for v in buckets.values()) < min_samples:
        return default
    medians = {k: median(v) for k, v in buckets.items() if len(v) >= 3}
    if len(medians) < 6:
        return default
    day_median = median(medians.values())
    wide = {k for k, v in medians.items() if v > ratio * day_median}
    per_day = (24 * 60) // bucket_minutes
    k = 0
    while k in wide and k < per_day // 4:
        k += 1
    end = max(default.rollover_end_minute, k * bucket_minutes)
    k = per_day - 1
    while k in wide and k > 3 * per_day // 4:
        k -= 1
    start = min(default.rollover_start_minute, (k + 1) * bucket_minutes)
    return ScheduleRules(start, end, default.friday_cutoff_hour, default.flat_before_weekend)



def lookup_cell(cells: Mapping[tuple[str, str, int], CostCell], symbol: str, session: str,
                hour: int) -> Optional[CostCell]:
    """The exact cell, else the best-supported cell of the symbol in that hour or session."""
    exact = cells.get((symbol, session, hour))
    if exact is not None:
        return exact
    near = [c for (s, sess, h), c in cells.items() if s == symbol and (h == hour or sess == session)]
    return max(near, key=lambda c: c.n) if near else None
