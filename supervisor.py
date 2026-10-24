#!/usr/bin/env python3
"""Supervisor entry point: keeps mt5_bot.py running, restarting it after crashes.

Usage: python supervisor.py [bot options, e.g. --confirm-real-money]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mt5bot.supervisor import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
