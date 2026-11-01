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

# The bot never holds more than one entry per symbol: this is what forbids grids,
# pyramiding and averaging down. An entry larger than the broker's volume_max may be split
# into a few broker positions that share one entry group (same arm, same SL/TP, one plan).
MAX_POSITIONS_PER_SYMBOL = 1
MAX_SPLIT_ORDERS = 4

# Notional cap for a trade at exactly the broker's minimum lot, as a multiple of equity. Above the
# minimum lot MAX_SYMBOL_EXPOSURE_X applies. Without this separate cap no account below roughly
# 12 times the minimum lot's notional could trade at all (ADR-019). It is a ratio derived from
# broker specifications, not a balance; the margin guard and the risk ceilings still apply.
MAX_MIN_LOT_EXPOSURE_X = 30.0

# ---- v3.1: evidence-scaled risk. Exploration (tier T1) is the explicit price of
# information; its percent budget can never exceed these ceilings, whatever the config says.
# v4.2: the budget is also measured in trades (tiers.exploration_room), and that part is
# bounded by the daily loss limit and the weekly soft limit of the profile.
MAX_EXPLORATION_DAY_PCT = 0.5
MAX_EXPLORATION_WEEK_PCT = 1.5
MAX_EXPLORATION_TRADE_PCT = 0.5
MAX_WEEKLY_SOFT_LOSS_PCT = 10.0
MAX_EXPLORATION_TRADES_PER_DAY = 20
MAX_EXPLORATION_TRADES_PER_WEEK = 60
# Fixed floors of the entry gates: the Flow Doctor may lower the gates down to these, never below.
ENTRY_MIN_P_FLOOR = 0.52
ENTRY_MIN_COST_TO_EDGE_FLOOR = 1.0
ENTRY_MIN_OOS_TRADES_FLOOR = 20
# The profit ratchet locks at least nothing and at most 90 percent of new highs.
MAX_LOCK_FRACTION = 0.9
MAX_HOUSE_MONEY = 1.5
MAX_KELLY_FRACTION = 0.5
# Trading-hours window the Flow Doctor may widen to (server hours, [start, end)). The
# rollover hour around server midnight stays closed: spreads are widest there.
WIDEST_TRADING_HOURS = (1, 23)

# ---- v3: optional Claude advisor. Cost and latency ceilings; no config can exceed them.
MAX_ADVISOR_LATENCY_BUDGET_MS = 5000
MAX_ADVISOR_TIMEOUT_SECONDS = 60.0
MAX_CLAUDE_CALLS_PER_HOUR = 600
MAX_CLAUDE_DAILY_COST_USD = 50.0
MAX_CLAUDE_MONTHLY_COST_USD = 500.0
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
