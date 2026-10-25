"""CYBERBOX Trading Office: the read-only live web panel of the MT5 bot.

A separate process (``python -m panel``). It reads the bot's SQLite databases and health
file and never imports the trading loop, so a panel crash cannot affect trading.
"""
