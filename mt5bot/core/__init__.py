"""Pure core domain of v4: no terminal, no database, no network, no wall clock.

Everything here is a deterministic function of its inputs, so backtest, replay, shadow and
live trading can share it without divergence. ``tests/test_architecture.py`` enforces this.
"""
