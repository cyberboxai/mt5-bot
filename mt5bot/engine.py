"""Champion/challenger engine with shadow trading, promotion and degradation demotion.

* The champion (per symbol) is the only candidate allowed to place orders.
* Challengers are the top optimiser survivors; they trade in SHADOW mode on live bars with
  the backtester's fill and cost rules, and their virtual trades go to the journal.
* Promotion needs: gates passed + shadow evidence consistent with the backtest + a margin
  over the champion's recent score (hysteresis) + a cooldown since the last switch.
* Demotion is immediate when the champion degrades; it triggers a re-optimisation.
* With no champion, the symbol stays in observation mode (``NoTrade``).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

import numpy as np

from . import strategies as strategy_lib
from .backtest import (CostModel, VirtualPosition, close_position, exit_for_bar, market_exit_price,
                       open_position)
from .registry import NOTRADE_CANDIDATE, Candidate, Registry, TradeRow
from .risk import SymbolSpec
from .stats import composite_score, streak_limit
from .strategies.base import Signal

log = logging.getLogger("mt5_bot")

DAY = 86400.0
WEEK = 7 * DAY
MAX_CATCH_UP_BARS = 2000


@dataclass(frozen=True)
class EntryIntent:
    """What the champion wants to do on the latest bar. The risk layer decides the rest."""

    signal: Signal
    param_set_id: Optional[int]
    strategy: str
    description: str


@dataclass(frozen=True)
class EngineSettings:
    max_challengers: int = 3
    min_shadow_trades: int = 30
    min_shadow_days: float = 14
    min_shadow_trades_floor: int = 10
    promotion_margin: float = 0.15
    switch_cooldown_days: float = 7
    bootstrap_days: Optional[float] = None
    degrade_window_trades: int = 30
    degrade_min_trades: int = 10
    degrade_ucb_z: float = 1.645
    degrade_dd_multiple: float = 2.0
    degrade_max_dd_r: float = 10.0
    degrade_streak_alpha: float = 0.01
    shadow_consistency_z: float = 1.645

    @classmethod
    def from_config(cls, cfg: Any) -> "EngineSettings":
        names = [f for f in cls.__dataclass_fields__ if f != "shadow_consistency_z"]
        return cls(**{n: getattr(cfg, n) for n in names})


# --------------------------------------------------------------------- shadow book


class ShadowBook:
    """Paper-trades one candidate on one symbol. Never sends orders.

    Bar processing order is identical to ``backtest.simulate``: pending exit at the open,
    pending entry at the open, SL/TP inside the bar, then the signal of the bar that just
    closed becomes pending for the next one. State survives restarts via the registry.
    """

    def __init__(self, registry: Registry, symbol: str, candidate: Candidate) -> None:
        self.registry = registry
        self.symbol = symbol
        self.candidate = candidate
        self.strategy = strategy_lib.get(candidate.strategy)
        self.key = f"shadow:{symbol}:{candidate.param_set_id}"
        self.pos: Optional[VirtualPosition] = None
        self.trade_id: Optional[int] = None
        self.pending = 0
        self.pending_atr = 0.0
        self.pending_exit = False
        self.last_bar_time: Optional[int] = None
        self._load()

    def _load(self) -> None:
        raw = self.registry.get_kv(self.key)
        if raw:
            data = json.loads(raw)
            self.pending = int(data.get("pending", 0))
            self.pending_atr = float(data.get("pending_atr", 0.0))
            self.pending_exit = bool(data.get("pending_exit", False))
            self.last_bar_time = data.get("last_bar_time")
        open_rows = self.registry.trades(mode="shadow", param_set_id=self.candidate.param_set_id, status="open",
                                         symbol=self.symbol)
        if open_rows:
            row = open_rows[-1]
            self.trade_id = row.id
            self.pos = VirtualPosition(1 if row.side == "BUY" else -1, int(row.extra.get("entry_index", -1)),
                                       int(row.open_bar_time or 0), row.entry, row.sl, row.tp, row.volume,
                                       row.risk_money)

    def _save(self) -> None:
        self.registry.set_kv(self.key, json.dumps({
            "pending": self.pending, "pending_atr": self.pending_atr, "pending_exit": self.pending_exit,
            "last_bar_time": self.last_bar_time,
        }))

    def _close(self, index: int, bar_time: int, price: float, reason: str, spec: SymbolSpec,
               costs: CostModel) -> Optional[TradeRow]:
        assert self.pos is not None
        trade = close_position(self.pos, index, bar_time, price, reason, spec, costs)
        if self.trade_id is not None:
            self.registry.close_trade(self.trade_id, price, trade.pnl, trade.r, reason, close_bar_time=bar_time)
        self.pos, self.trade_id = None, None
        return trade

    def on_bars(self, closed: np.ndarray, spec: SymbolSpec, costs: CostModel, equity: float,
                risk_pct: float) -> int:
        """Process every closed bar not seen yet. Returns the number of shadow trades closed."""
        if len(closed) < 2:
            return 0
        times = closed["time"].astype(np.int64)
        if self.last_bar_time is None:
            # First sight: only arm the signal of the latest closed bar; never trade history.
            start = len(closed) - 1
        else:
            start = int(np.searchsorted(times, int(self.last_bar_time), side="right"))
            start = max(start, len(closed) - MAX_CATCH_UP_BARS)
        if start >= len(closed):
            return 0
        sig = self.strategy.signals(closed, self.candidate.params)
        closed_count = 0
        first_sight = self.last_bar_time is None
        for i in range(start, len(closed)):
            bar = closed[i]
            t = int(bar["time"])
            o, h, lo, spr = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["spread"])
            if not first_sight:
                if self.pos is not None and self.pending_exit:
                    price = market_exit_price(self.pos, o, spr, spec.point, costs.slippage_points)
                    closed_count += self._close(i, t, price, "opposite", spec, costs) is not None
                    self.pending_exit = False
                if self.pos is None and self.pending:
                    self.pos = open_position(self.pending, i, t, o, spr, self.pending_atr, sig.sl_mult, sig.tp_mult,
                                             spec, costs, equity, risk_pct)
                    self.pending = 0
                    if self.pos is not None:
                        self.trade_id = self.registry.open_trade(
                            "shadow", self.symbol, self.candidate.param_set_id, "BUY" if self.pos.side > 0 else "SELL",
                            self.pos.entry_price, self.pos.sl, self.pos.tp, self.pos.volume, self.pos.risk_money,
                            open_bar_time=t, extra={"entry_index": i, "strategy": self.candidate.strategy})
                if self.pos is not None:
                    hit = exit_for_bar(self.pos, o, h, lo, spr, spec.point, costs.slippage_points,
                                       self.pos.entry_time == t)
                    if hit is not None:
                        closed_count += self._close(i, t, hit[0], hit[1], spec, costs) is not None
            s = int(sig.side[i])
            if s != 0:
                if self.pos is None:
                    self.pending, self.pending_atr = s, float(sig.atr[i])
                elif costs.close_on_opposite and s == -self.pos.side:
                    self.pending_exit = True
                    self.pending, self.pending_atr = s, float(sig.atr[i])
            elif self.pos is None:
                self.pending = 0
            self.last_bar_time = t
        self._save()
        return closed_count


# ------------------------------------------------------------------ evidence math


def _rs(rows: Sequence[TradeRow]) -> np.ndarray:
    return np.array([r.r for r in rows if r.r is not None], dtype=float)


def weekly_score(rows: Sequence[TradeRow], now: float) -> float:
    """Composite score over weekly buckets of closed trades (no multiple-testing penalty)."""
    closed = [r for r in rows if r.r is not None and r.close_ts is not None]
    if not closed:
        return 0.0
    start = min(r.open_ts for r in closed)
    weeks = max(1, int(math.ceil((now - start) / WEEK)))
    sums = [0.0] * weeks
    for r in closed:
        sums[min(weeks - 1, int((r.close_ts - start) // WEEK))] += r.r
    rs = _rs(closed)
    curve = np.cumsum(rs)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:] - curve))
    return composite_score(sums, [7 / 30.4375] * weeks, dd, dsr=1.0)


@dataclass(frozen=True)
class Evidence:
    trades: int
    days: float
    expectancy_r: float
    std_r: float
    score: float
    enough: bool
    consistent: bool
    detail: str


def shadow_evidence(rows: Sequence[TradeRow], candidate: Candidate, settings: EngineSettings,
                    now: float) -> Evidence:
    closed = [r for r in rows if r.status == "closed" and r.r is not None]
    rs = _rs(closed)
    n = int(rs.size)
    first = min((r.open_ts for r in rows), default=now)
    days = (now - first) / DAY
    mean = float(rs.mean()) if n else 0.0
    std = float(rs.std(ddof=1)) if n > 1 else 0.0
    enough = n >= settings.min_shadow_trades or (days >= settings.min_shadow_days
                                                 and n >= settings.min_shadow_trades_floor)
    expected = float(candidate.metrics.get("expectancy_r", 0.0))
    tolerance = settings.shadow_consistency_z * std / math.sqrt(n) if n > 1 else math.inf
    consistent = n > 0 and mean > 0 and mean >= expected - tolerance
    detail = (f"{n} shadow trades over {days:.1f} days, mean {mean:+.3f}R (backtest {expected:+.3f}R, "
              f"tolerance {tolerance:.3f}R)")
    return Evidence(n, days, mean, std, weekly_score(closed, now), enough, consistent, detail)


def degradation_reason(rows: Sequence[TradeRow], candidate: Candidate, settings: EngineSettings,
                       label: str) -> Optional[str]:
    """Why the champion must be demoted, or None. ``rows`` are its closed trades since promotion."""
    rs = _rs(rows)
    n = int(rs.size)
    if n == 0:
        return None
    oos_dd = float(candidate.metrics.get("max_drawdown_r", settings.degrade_max_dd_r))
    dd_limit = min(settings.degrade_dd_multiple * max(oos_dd, 1.0), settings.degrade_max_dd_r)
    curve = np.cumsum(rs)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:] - curve))
    if dd > dd_limit:
        return f"{label} drawdown {dd:.1f}R exceeds its budget {dd_limit:.1f}R"
    if n >= settings.degrade_min_trades:
        recent = rs[-settings.degrade_window_trades:]
        ucb = float(recent.mean() + settings.degrade_ucb_z * recent.std(ddof=1) / math.sqrt(recent.size))
        if ucb < 0:
            return (f"{label} expectancy upper confidence bound {ucb:+.3f}R < 0 over the last "
                    f"{recent.size} trades")
    win_rate = float(candidate.metrics.get("win_rate", 0.5)) or 0.5
    limit = streak_limit(max(n, 1), 1.0 - win_rate, settings.degrade_streak_alpha)
    streak = best = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        best = max(best, streak)
    if best >= limit:
        return (f"{label} losing streak of {best} trades; with win rate {win_rate:.0%} a streak of {limit} "
                f"over {n} trades has probability < {settings.degrade_streak_alpha:.1%}")
    return None


# ------------------------------------------------------------------------- engine


class AdaptiveEngine:
    """Owns champion selection for every symbol. All decisions go to the audit log."""

    def __init__(self, registry: Registry, settings: EngineSettings,
                 notify: Callable[[str], None] = lambda text: None,
                 request_reoptimization: Callable[[str, str], None] = lambda symbol, reason: None,
                 clock: Callable[[], float] = time.time) -> None:
        self.registry = registry
        self.settings = settings
        self.notify = notify
        self.request_reoptimization = request_reoptimization
        self.clock = clock
        self.books: dict[tuple[str, int], ShadowBook] = {}
        if self.registry.get_kv("bootstrap_started") is None:
            self.registry.set_kv("bootstrap_started", str(self.clock()))

    def update_settings(self, settings: EngineSettings) -> None:
        self.settings = settings

    # ---- candidates

    def adopt_latest_run(self, symbol: str) -> bool:
        """Refresh the challenger set from the newest finished optimisation run."""
        run = self.registry.latest_run(symbol)
        if run is None:
            return False
        key = f"adopted_run:{symbol}"
        if self.registry.get_kv(key) == str(run["id"]):
            return False
        champion = self.registry.champion(symbol)
        survivors = [c for c in self.registry.run_candidates(int(run["id"])) if c.passed]
        challengers = [c for c in survivors if c.param_set_id != champion.param_set_id][:self.settings.max_challengers]
        self.registry.set_challengers(symbol, challengers)
        self.registry.set_kv(key, str(run["id"]))
        names = ", ".join(c.describe() for c in challengers) or "none (NoTrade remains the default)"
        log.info("%s: optimisation run %s adopted; challengers: %s", symbol, run["id"], names)
        self.registry.log_event(symbol, "optimization", f"run {run['id']}: {len(survivors)} survivor(s); "
                                f"challengers: {names}", {"run_id": int(run["id"]), "trials": int(run["trials"])})
        return True

    def _book(self, symbol: str, candidate: Candidate) -> Optional[ShadowBook]:
        if candidate.is_notrade or candidate.param_set_id is None:
            return None
        key = (symbol, candidate.param_set_id)
        if key not in self.books:
            self.books[key] = ShadowBook(self.registry, symbol, candidate)
        return self.books[key]

    def tracked(self, symbol: str) -> list[Candidate]:
        champion = self.registry.champion(symbol)
        out = [] if champion.is_notrade else [champion]
        out.extend(c for c, _ in self.registry.challengers(symbol) if c.param_set_id != champion.param_set_id)
        return out

    # ---- per bar

    def on_bar(self, symbol: str, closed: np.ndarray, spec: SymbolSpec, costs: CostModel, equity: float,
               risk_pct: float) -> Optional[EntryIntent]:
        """Update shadow books and decisions, then return the champion's intent for the latest bar."""
        self.adopt_latest_run(symbol)
        tracked = self.tracked(symbol)
        live_keys = {(symbol, c.param_set_id) for c in tracked}
        for key in [k for k in self.books if k[0] == symbol and k not in live_keys]:
            del self.books[key]
        for cand in tracked:
            book = self._book(symbol, cand)
            if book is not None:
                try:
                    book.on_bars(closed, spec, costs, equity, risk_pct)
                except Exception:  # noqa: BLE001 - one candidate must never stop the others
                    log.exception("%s: shadow book for %s failed", symbol, cand.describe())
        self.check_degradation(symbol)
        self.check_promotion(symbol)
        champion = self.registry.champion(symbol)
        if champion.is_notrade:
            return None
        sig = strategy_lib.get(champion.strategy).signal(closed, champion.params)
        if sig is None:
            return None
        return EntryIntent(sig, champion.param_set_id, champion.strategy, champion.describe())

    def exit_signal(self, symbol: str, param_set_id: Optional[int], side: str, closed: np.ndarray) -> bool:
        """True if the strategy that OPENED a position now signals the opposite side.

        Positions are always managed by their original logic, even after a switch.
        """
        if param_set_id is None:
            return False
        try:
            strategy_name, params = self.registry.param_set(int(param_set_id))
            sig = strategy_lib.get(strategy_name).signal(closed, params)
        except Exception:  # noqa: BLE001 - unknown origin: SL/TP keep protecting the position
            log.exception("%s: cannot evaluate exit logic for param set %s", symbol, param_set_id)
            return False
        return sig is not None and sig.side != side

    # ---- degradation

    def champion_evidence(self, symbol: str) -> tuple[Candidate, list[TradeRow], list[TradeRow]]:
        champion = self.registry.champion(symbol)
        since = self.registry.champion_since(symbol) or 0.0
        if champion.is_notrade:
            return champion, [], []
        live = [r for r in self.registry.trades(mode="live", param_set_id=champion.param_set_id, status="closed",
                                                symbol=symbol) if (r.close_ts or 0) >= since]
        shadow = [r for r in self.registry.trades(mode="shadow", param_set_id=champion.param_set_id,
                                                  status="closed", symbol=symbol) if (r.close_ts or 0) >= since]
        return champion, live, shadow

    def check_degradation(self, symbol: str) -> Optional[str]:
        champion, live, shadow = self.champion_evidence(symbol)
        if champion.is_notrade:
            return None
        reason = (degradation_reason(live, champion, self.settings, "live")
                  or degradation_reason(shadow, champion, self.settings, "shadow"))
        if reason is None:
            return None
        replacement, evidence = self._best_validated(symbol, exclude=champion.param_set_id)
        new = replacement or NOTRADE_CANDIDATE
        metrics = {"live_trades": len(live), "shadow_trades": len(shadow),
                   "live_r": float(_rs(live).sum()) if live else 0.0,
                   "shadow_r": float(_rs(shadow).sum()) if shadow else 0.0,
                   "replacement_evidence": evidence.detail if evidence else None}
        promo = self.registry.record_switch(symbol, "demote", champion, new, reason, metrics)
        text = f"{symbol}: DEMOTED {champion.describe()} -> {new.describe()} (#{promo}): {reason}"
        log.warning(text)
        self.notify(text)
        self.request_reoptimization(symbol, f"demotion: {reason}")
        return reason

    def _best_validated(self, symbol: str, exclude: Optional[int]) -> tuple[Optional[Candidate], Optional[Evidence]]:
        now = self.clock()
        best: tuple[Optional[Candidate], Optional[Evidence]] = (None, None)
        best_score = 0.0
        for cand, _added in self.registry.challengers(symbol):
            if cand.param_set_id == exclude or not cand.passed:
                continue
            ev = shadow_evidence(self.registry.trades(mode="shadow", param_set_id=cand.param_set_id, symbol=symbol),
                                 cand, self.settings, now)
            if not (ev.enough and ev.consistent):
                continue
            score = min(cand.score, ev.score)
            if score > best_score:
                best, best_score = (cand, ev), score
        return best

    # ---- promotion

    def champion_report(self, symbol: str) -> dict:
        """Live and shadow performance of the champion since promotion vs its out-of-sample expectation."""
        champion, live, shadow = self.champion_evidence(symbol)
        if champion.is_notrade:
            return {}
        oos = float(champion.metrics.get("expectancy_r", 0.0))
        live_rs, shadow_rs = _rs(live), _rs(shadow)
        live_mean = float(live_rs.mean()) if live_rs.size else None
        return {
            "oos_expectancy_r": round(oos, 4),
            "live_trades": int(live_rs.size),
            "live_expectancy_r": None if live_mean is None else round(live_mean, 4),
            "live_r_total": round(float(live_rs.sum()), 3) if live_rs.size else 0.0,
            "shadow_trades": int(shadow_rs.size),
            "shadow_expectancy_r": round(float(shadow_rs.mean()), 4) if shadow_rs.size else None,
            "live_minus_oos_r": None if live_mean is None else round(live_mean - oos, 4),
        }

    def champion_score(self, symbol: str) -> float:
        champion, live, shadow = self.champion_evidence(symbol)
        if champion.is_notrade:
            return 0.0
        rows = live if len(live) >= self.settings.min_shadow_trades_floor else shadow
        if len(rows) >= self.settings.min_shadow_trades_floor:
            return weekly_score(rows, self.clock())
        return champion.score

    def promotion_status(self, symbol: str) -> list[dict]:
        """Progress of every challenger towards promotion (for --status and the status line)."""
        now = self.clock()
        out = []
        for cand, _added in self.registry.challengers(symbol):
            ev = shadow_evidence(self.registry.trades(mode="shadow", param_set_id=cand.param_set_id, symbol=symbol),
                                 cand, self.settings, now)
            out.append({"candidate": cand.describe(), "candidate_id": cand.candidate_id, "gates_passed": cand.passed,
                        "oos_score": cand.score, "shadow_trades": ev.trades, "shadow_days": round(ev.days, 1),
                        "shadow_expectancy_r": round(ev.expectancy_r, 4), "enough_evidence": ev.enough,
                        "consistent": ev.consistent, "shadow_score": ev.score})
        return out

    def cooldown_left(self, symbol: str) -> float:
        last = self.registry.last_switch_ts(symbol)
        if last is None:
            return 0.0
        return max(0.0, self.settings.switch_cooldown_days * DAY - (self.clock() - last))

    def bootstrap_left(self) -> float:
        if self.settings.bootstrap_days is None:
            return 0.0
        started = float(self.registry.get_kv("bootstrap_started") or self.clock())
        return max(0.0, self.settings.bootstrap_days * DAY - (self.clock() - started))

    def check_promotion(self, symbol: str) -> Optional[int]:
        now = self.clock()
        champion = self.registry.champion(symbol)
        ever_switched = self.registry.last_switch_ts(symbol) is not None
        if ever_switched and self.cooldown_left(symbol) > 0:
            return None
        if not ever_switched and self.bootstrap_left() > 0:
            return None
        champ_score = self.champion_score(symbol)
        required = champ_score + self.settings.promotion_margin * max(abs(champ_score), 1e-9)
        best: Optional[tuple[float, Candidate, Evidence]] = None
        for cand, _added in self.registry.challengers(symbol):
            if cand.param_set_id == champion.param_set_id or not cand.passed:
                continue
            ev = shadow_evidence(self.registry.trades(mode="shadow", param_set_id=cand.param_set_id, symbol=symbol),
                                 cand, self.settings, now)
            if not (ev.enough and ev.consistent):
                continue
            score = min(cand.score, ev.score)
            if score <= 0 or score <= required:
                continue
            if best is None or score > best[0]:
                best = (score, cand, ev)
        if best is None:
            return None
        score, cand, ev = best
        reason = (f"gates passed (OOS score {cand.score:.3f}); {ev.detail}; challenger score {score:.3f} beats "
                  f"champion {champ_score:.3f} by more than {self.settings.promotion_margin:.0%}")
        metrics = {"challenger_score": score, "champion_score": champ_score, "oos": _summary(cand.metrics),
                   "shadow_trades": ev.trades, "shadow_days": ev.days, "shadow_expectancy_r": ev.expectancy_r}
        promo = self.registry.record_switch(symbol, "promote", champion, cand, reason, metrics)
        text = f"{symbol}: PROMOTED {cand.describe()} (replacing {champion.describe()}, #{promo}): {reason}"
        log.warning(text)
        self.notify(text)
        challengers = [c for c, _ in self.registry.challengers(symbol) if c.param_set_id != cand.param_set_id]
        self.registry.set_challengers(symbol, challengers)
        return promo

    # ---- manual override

    def rollback(self, promotion_id: int) -> int:
        """Restore the champion that a past decision installed (manual action, audited)."""
        row = self.registry.promotion(promotion_id)
        if row is None:
            raise KeyError(f"decision #{promotion_id} not found; see --history")
        symbol = row["symbol"]
        old = self.registry.champion(symbol)
        if row["new_param_set_id"] is None:
            new = NOTRADE_CANDIDATE
        elif row["new_candidate_id"] is not None:
            new = self.registry.candidate(int(row["new_candidate_id"]))
        else:
            strategy, params = self.registry.param_set(int(row["new_param_set_id"]))
            new = Candidate(None, int(row["new_param_set_id"]), strategy, params, {}, 0.0, False)
        return self.registry.record_switch(symbol, "rollback", old, new, f"manual rollback to decision #{promotion_id}",
                                           {"rolled_back_to": promotion_id})


def _summary(metrics: dict) -> dict:
    keys = ("trades", "expectancy_r", "profit_factor", "max_drawdown_r", "win_rate", "deflated_sharpe",
            "bootstrap_low", "profitable_window_ratio", "neighbor_pass_ratio", "total_trials")
    return {k: metrics.get(k) for k in keys}
