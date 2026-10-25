"""Hard safety ceilings. Code constants: no config file, CLI flag or strategy can exceed them.

Changing any value here is a code change that must be reviewed, tested and deployed
with a restart. ``config.Config.validate`` rejects values above these ceilings and
``risk.RiskManager`` clamps to them again (defence in depth).
"""

from __future__ import annotations

MAX_RISK_PER_TRADE_PCT = 2.0
MAX_DAILY_LOSS_PCT = 5.0
MAX_OPEN_POSITIONS = 5
MAX_TOTAL_OPEN_RISK_PCT = 4.0
# Notional exposure per symbol as a multiple of equity (volume * contract * price / equity).
MAX_SYMBOL_EXPOSURE_X = 10.0
MAX_DRAWDOWN_FROM_PEAK_PCT = 20.0

# Bounds on the SL/TP ATR multiples a strategy may suggest. A signal outside them is rejected.
MIN_SL_ATR_MULT = 0.5
MAX_SL_ATR_MULT = 6.0
MIN_TP_ATR_MULT = 0.5
MAX_TP_ATR_MULT = 10.0

# The bot never uses more than one position per symbol: this is what forbids grids,
# pyramiding and averaging down.
MAX_POSITIONS_PER_SYMBOL = 1

# ---- v3: optional Claude advisor. Cost and latency ceilings; no config can exceed them.
MAX_ADVISOR_LATENCY_BUDGET_MS = 5000
MAX_ADVISOR_TIMEOUT_SECONDS = 60.0
MAX_CLAUDE_CALLS_PER_HOUR = 600
MAX_CLAUDE_DAILY_COST_USD = 50.0
MAX_CLAUDE_MAX_TOKENS = 16000

# ---- v3: specialist team. The ensemble and the advisor may only shrink the deterministic
# base size: every multiplier they produce is clamped to [0, MAX_SIZE_MULTIPLIER].
MAX_SIZE_MULTIPLIER = 1.0
MIN_AGENT_WEIGHT = 0.05
MAX_AGENT_WEIGHT = 5.0
MAX_LEARNING_RATE = 0.5
# Advisor weight tilts: at most +/-20 percent, never on the Risk Manager or the Guardian.
MAX_WEIGHT_TILT = 0.20
MAX_TILT_HOURS = 24.0
MAX_AGENT_TIMEOUT_MS = 5000
