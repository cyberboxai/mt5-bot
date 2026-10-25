#!/usr/bin/env python3
"""MT5 trading bot v2 entry point. See README.md (PT-PT) for setup and operation.

Usage examples:
    python mt5_bot.py --dry-run
    python mt5_bot.py --confirm-real-money
    python mt5_bot.py --status | --history | --rollback ID | --resume
    python mt5_bot.py --backtest | --optimize-once
"""

import os
import sys

# Run from the project folder so relative paths in config.json resolve the same way
# whether started by hand, by start_bot.bat or by the Task Scheduler.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mt5bot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
