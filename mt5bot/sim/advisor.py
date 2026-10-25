"""A deterministic, rule-based stand-in for the Claude advisor, for SIMULATION ONLY.

It lets ``--simulate`` and ``python -m panel --demo`` show the advisor booth, its state
machine and its audit log with no API key and no cost. It follows the same contract as
the real advisor (it can only confirm, reduce or veto) and labels itself "simulated" in
every record. It also cycles through DEGRADED and OFFLINE_NO_CREDIT periods so the panel
can be seen handling outages.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from typing import Any, Callable, Mapping, Optional, Sequence

from ..advisor.base import Advisor
from ..advisor.types import AdvisorOpinion, AdvisorState, Briefing, Reason, WeightTilt, unavailable

OUTAGE_PLAN = ((180, 200, AdvisorState.DEGRADED, "simulated transient outage (overloaded)"),
               (420, 470, AdvisorState.OFFLINE_NO_CREDIT, "simulated: credit balance too low"))
CYCLE = 600


class SimulatedAdvisor(Advisor):
    name = "simulated"

    def __init__(self, clock: Callable[[], float] = time.time, store: Any = None) -> None:
        self.clock = clock
        self.store = store
        self.calls = 0
        self._state = AdvisorState.ONLINE
        self._reason = "simulated advisor (no API, no cost)"
        self._verdicts: Counter[str] = Counter()
        self._briefing: Optional[Briefing] = None
        self._history: list[dict] = []

    @property
    def state(self) -> AdvisorState:
        return self._state

    def tick(self) -> None:
        phase = self.calls % CYCLE
        state, reason = AdvisorState.ONLINE, "simulated advisor (no API, no cost)"
        for start, end, s, r in OUTAGE_PLAN:
            if start <= phase < end:
                state, reason = s, r
        if state != self._state:
            self._history.append({"ts": self.clock(), "from": self._state.value, "to": state.value, "reason": reason})
            if self.store is not None:
                self.store.log_advisor_state(self.clock(), self._state.value, state.value, reason)
            self._state, self._reason = state, reason
        self.calls += 1  # advances the outage plan on every housekeeping tick

    def review_trade(self, features: Mapping[str, Any], untrusted: Sequence[str] = ()) -> AdvisorOpinion:
        if self._state != AdvisorState.ONLINE:
            return unavailable(f"advisor {self._state.value}: {self._reason}")
        spread_ratio = float(features.get("spread_vs_typical") or 1.0)
        disagreement = float(features.get("disagreement") or 0.0)
        regime = str(features.get("regime") or "")
        reasons, flags = [], []
        verdict, size = "confirm", 1.0
        if spread_ratio > 2.5:
            verdict, size = "veto", 0.0
            reasons.append(Reason("SPREAD_WIDE", f"spread {spread_ratio:.1f}x its usual level"))
            flags.append("wide_spread")
        elif disagreement > 0.3:
            verdict, size = "reduce", round(1.0 - disagreement, 2)
            reasons.append(Reason("AGENT_DISAGREEMENT", f"specialists disagree ({disagreement:.2f})"))
        elif regime == "high_vol":
            verdict, size = "reduce", 0.6
            reasons.append(Reason("VOLATILITY_HIGH", "volatility regime: smaller size"))
            flags.append("high_volatility")
        else:
            reasons.append(Reason("TREND_ALIGNED", "no concrete risk found"))
        self._verdicts[verdict] += 1
        digest = hashlib.sha256(json.dumps(features, sort_keys=True, default=str).encode()).hexdigest()[:24]
        call_id = None
        if self.store is not None:
            call_id = self.store.log_advisor_call({
                "ts": self.clock(), "purpose": "trade_review", "model": "simulated", "prompt_hash": digest,
                "redacted_prompt": json.dumps(features, sort_keys=True, default=str)[:2000],
                "response": json.dumps({"verdict": verdict, "size_multiplier": size}), "valid": 1,
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "cost_usd": 0.0, "latency_ms": 5.0, "outcome": "ok", "error_kind": None,
                "detail": "simulated advisor", "state_after": self._state.value})
        return AdvisorOpinion(True, verdict, size, 0.6, tuple(reasons), tuple(flags), model="simulated",
                              latency_ms=5.0, call_id=call_id)

    def request_briefing(self, features: Mapping[str, Any], untrusted: Sequence[str] = ()) -> bool:
        if self._state != AdvisorState.ONLINE:
            return False
        regime = str(features.get("regime") or "misto")
        self._briefing = Briefing(True, f"Regime dominante: {regime}",
                                  "Briefing SIMULADO: mercado sintético, sem significado para mercados reais. "
                                  "A equipa mantém o tamanho base apenas quando os especialistas concordam.",
                                  (WeightTilt("trend_analyst", 1.1, self.clock() + 86400),), ())
        return True

    def take_briefing(self) -> Optional[Briefing]:
        out, self._briefing = self._briefing, None
        return out

    def snapshot(self) -> dict[str, Any]:
        return {"state": self._state.value, "reason": self._reason, "provider": "simulated",
                "model_fast": "simulated", "model_deep": "simulated", "verdicts": dict(self._verdicts),
                "budget": {"calls_last_hour": 0, "max_calls_per_hour": 0, "spent_today_usd": 0.0,
                           "max_daily_cost_usd": 0.0},
                "latency_ms": {"p50": 5.0, "p95": 5.0}, "history": self._history[-50:]}
