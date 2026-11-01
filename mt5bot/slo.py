"""Time-to-first-trade objective.

The clock counts hours of market activity while trading is actually possible: equity above the
feasibility floor and at least one feasible arm passing the entry gates. A weekend, or a day with no
arm that deserves a trade, is not a failure of the objective: there the honest answer is not to trade,
and the panel says why. Past the target, the Flow Doctor must find the blocker and act within an hour.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass
class FirstTradeClock:
    eligible_seconds: float = 0.0
    last_ts: Optional[float] = None
    first_order_ts: Optional[float] = None
    missed_ts: Optional[float] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: Optional[str]) -> "FirstTradeClock":
        if not text:
            return cls()
        try:
            data = json.loads(text)
            return cls(float(data.get("eligible_seconds", 0.0)), data.get("last_ts"), data.get("first_order_ts"),
                       data.get("missed_ts"))
        except (ValueError, TypeError, AttributeError):
            return cls()

    def update(self, now: float, market_open: bool, eligible: bool, target_hours: float,
               last_order_ts: Optional[float] = None, max_gap_seconds: float = 900.0) -> None:
        if last_order_ts is not None and self.first_order_ts is None:
            self.first_order_ts = last_order_ts
        previous, self.last_ts = self.last_ts, now
        if self.first_order_ts is not None or not market_open or not eligible or previous is None:
            return
        self.eligible_seconds += min(max(now - previous, 0.0), max_gap_seconds)   # a stopped bot is not elapsed time
        if self.eligible_seconds >= target_hours * 3600.0 and self.missed_ts is None:
            self.missed_ts = now

    @property
    def met(self) -> bool:
        return self.first_order_ts is not None

    @property
    def missed(self) -> bool:
        return self.first_order_ts is None and self.missed_ts is not None

    def remaining_hours(self, target_hours: float) -> Optional[float]:
        if self.met:
            return None
        return max(target_hours - self.eligible_seconds / 3600.0, 0.0)

    def snapshot(self, target_hours: float) -> dict[str, Any]:
        return {"target_hours": target_hours, "elapsed_hours": round(self.eligible_seconds / 3600.0, 2),
                "remaining_hours": None if self.met else round(self.remaining_hours(target_hours) or 0.0, 2),
                "met": self.met, "missed": self.missed, "first_order_ts": self.first_order_ts}
