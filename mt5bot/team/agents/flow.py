"""Flow Doctor (the adaptive activity governor), as a member of the team.

The decisions are made by ``mt5bot.flow`` inside the trading loop (so they run even when the
team is down); this agent is the office character that explains them: it publishes the
plain-language status and every bounded adjustment, and visits the Server Room when the
system is idle for too long.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..agent import Agent, AgentState
from ..events import FlowAdjusted

VERB_PT = {"relax": "aliviei", "tighten": "apertei", "revert": "reverti"}


class FlowDoctor(Agent):
    id = "flow_doctor"

    def report(self, status: str, adjustments: Sequence[Any], top: Optional[Mapping[str, Any]]) -> None:
        self.stats["status"] = status
        self.stats["top_blocker"] = dict(top) if top else None
        for adj in adjustments:
            self.bus.publish(FlowAdjusted(adj.knob, float(adj.old), float(adj.new), adj.direction, adj.reason[:240]),
                             agent=self.id, severity="notice" if adj.direction == "relax" else "warning")
            self.say(f"{VERB_PT.get(adj.direction, adj.direction).capitalize()} {adj.knob}: {adj.old:g} -> "
                     f"{adj.new:g}")
        if adjustments:
            self.set_state(AgentState.REVIEWING, task="ajuste limitado do funil", message=status[:200])
        else:
            self.set_state(AgentState.MONITORING, task="a vigiar o funil", message=status[:200])
