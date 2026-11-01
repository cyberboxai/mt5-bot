"""Market-derived assets shared between accounts: ``state_root/_shared``.

Only what does not depend on any account lives here: downloaded bars and the count of research
trials on market data, so a second account does not repeat the download and the deflated Sharpe
still counts every configuration ever tried on the same data. No fill, tier, budget, posterior from
live trades or cost calibration is ever written here: those stay per account, so demo results
cannot influence a real account.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

from .persistence import atomic_write_json, atomic_write_text

log = logging.getLogger("mt5_bot")

SHARED_DIR = "_shared"
NOTICE = ("Dados de mercado partilhados entre contas. Nenhum dado de conta vive aqui: sem preenchimentos, "
          "níveis, orçamentos nem calibração de custos.\n")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


class SharedMarket:
    def __init__(self, state_root: str | os.PathLike[str]) -> None:
        self.root = Path(state_root) / SHARED_DIR
        self.bars_dir = self.root / "bars"
        self.bars_dir.mkdir(parents=True, exist_ok=True)
        notice = self.root / "LEIA-ME.txt"
        if not notice.exists():
            atomic_write_text(notice, NOTICE, durable=False)

    def _bars_path(self, symbol: str, timeframe: str) -> Path:
        return self.bars_dir / f"{_safe(symbol)}_{_safe(timeframe)}.npy"

    def store_bars(self, symbol: str, timeframe: str, rates: np.ndarray) -> None:
        """Merge by bar time, newest wins, so the shared history only grows."""
        if rates is None or not len(rates):
            return
        path = self._bars_path(symbol, timeframe)
        known = self.load_bars(symbol, timeframe)
        merged = rates if known is None or known.dtype != rates.dtype else np.concatenate([known, rates])
        _times, last = np.unique(merged["time"][::-1], return_index=True)
        merged = merged[::-1][last]
        tmp = path.with_suffix(".tmp.npy")
        np.save(tmp, merged)
        os.replace(tmp, path)

    def load_bars(self, symbol: str, timeframe: str) -> Optional[np.ndarray]:
        path = self._bars_path(symbol, timeframe)
        if not path.exists():
            return None
        try:
            return np.load(path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            log.warning("shared bars %s unreadable (%s); ignored", path.name, exc)
            return None

    def publish_trials(self, account: str, count: int) -> None:
        path = self.root / "trials.json"
        data = self._read_trials()
        data[account] = int(count)
        atomic_write_json(path, data, durable=False)

    def other_trials(self, account: str) -> int:
        return sum(v for k, v in self._read_trials().items() if k != account)

    def _read_trials(self) -> dict[str, int]:
        try:
            data = json.loads((self.root / "trials.json").read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def summary(self) -> dict[str, int]:
        return {"bar_files": len(list(self.bars_dir.glob("*.npy"))), "trials_all_accounts": sum(self._read_trials().values())}
