"""MT5 trading bot v3: account-agnostic, supervised, self-adapting core, optional specialist team
and optional Claude advisor (both can only make it more conservative).

Nothing in this package can guarantee profit. The adaptive engine maximises
out-of-sample, cost-adjusted, risk-adjusted expectancy and chooses ``NoTrade``
whenever no candidate demonstrates an edge.
"""

__version__ = "3.0.0"
