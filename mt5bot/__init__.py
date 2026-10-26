"""MT5 trading bot v3: one supervised, account-agnostic system. Arms (strategy + parameters +
symbol + timeframe) earn risk tiers from evidence, exploration trades at capped micro risk,
an optional specialist team and an optional Claude advisor can only make it more
conservative, and every decision leaves a reason code in the funnel.

Nothing in this package can guarantee profit. The bot sizes risk by the evidence it has
and stands aside when the evidence turns against an arm.
"""

__version__ = "3.1.0"
