#!/usr/bin/env python3
"""MT5 trading bot v3.1: the single entry point. See README.md (PT-PT) for setup and operation.

Usage examples:
    python mt5_bot.py --dry-run
    python mt5_bot.py --confirm-real-money
    python mt5_bot.py --status | --diagnose --hours 24 | --arms | --risk-report
    python mt5_bot.py --history | --rollback ID | --resume | --rebase-floor
    python mt5_bot.py --migrate | --rollback-upgrade
    python mt5_bot.py --backtest | --research-once | --simulate
"""

import os
import sys

# Run from the project folder so relative paths in config.json resolve the same way
# whether started by hand, by start_bot.bat or by the Task Scheduler.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mt5bot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
