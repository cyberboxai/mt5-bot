"""The 22 specialist agents, in roster order. Adding one: see README, "Acrescentar um agente"."""

from __future__ import annotations

from .cio import ChiefInvestmentOfficer
from .control import Guardian, RiskManagerAgent
from .desk import BreakoutAnalyst, MeanReversionAnalyst, MultiTimeframeAnalyst, TrendAnalyst
from .execution import ExecutionTrader, PositionManager
from .flow import FlowDoctor
from .market import CorrelationAnalyst, DataClerk, NewsSentinel, RegimeDetector, SessionAnalyst
from .metalabel import MetaLabeler
from .ops import ClaudeStrategist, OpsEngineer
from .research import PerformanceAnalyst, QuantResearcher, Reporter, ValidationAuditor

AGENT_CLASSES = (
    DataClerk, RegimeDetector, SessionAnalyst, NewsSentinel, CorrelationAnalyst,
    TrendAnalyst, MeanReversionAnalyst, BreakoutAnalyst, MultiTimeframeAnalyst, MetaLabeler,
    RiskManagerAgent, Guardian, ExecutionTrader, PositionManager,
    QuantResearcher, ValidationAuditor, PerformanceAnalyst, Reporter,
    OpsEngineer, ClaudeStrategist, FlowDoctor, ChiefInvestmentOfficer,
)

__all__ = ["AGENT_CLASSES"] + [cls.__name__ for cls in AGENT_CLASSES]
