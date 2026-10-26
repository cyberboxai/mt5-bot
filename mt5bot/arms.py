"""Arms: strategy + parameter set + symbol + timeframe, each with an evidence tier.

The system keeps many arms at once. Every arm is paper traded on every bar by its shadow
book (same fill and cost rules as the backtester, sized at a fixed reference equity because
R-multiples do not depend on account size). Its evidence (discounted walk-forward prior,
shadow trades, live trades) feeds a hierarchical Bayesian posterior (``evidence``) and the
tier rules (``tiers``). The engine returns, for each closed bar, the signals of the arms that
fired; the allocator and the decision protocol decide what, if anything, is traded.

Every tier change is written to the audit log with the metrics that justified it.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np

from . import indicators, management, regime
from . import strategies as strategy_lib
from .backtest import CostModel, VirtualPosition, close_position, exit_for_bar, market_exit_price, open_position
from .evidence import ArmSample, EvidenceSettings, Posterior, build_sample, family_of, fit
from .registry import Registry, TradeRow, _json
from .risk import SymbolSpec
from .strategies.base import Signal, SignalArrays
from .tiers import TIER_INDEX, TierDecision, TierSettings, decide_tier

log = logging.getLogger("mt5_bot")

REGIME_SHRINK = 20.0   # pseudo-trades pulling an arm's regime mean toward its overall mean
REGIME_MIN_VAR = 0.25  # floor on the R variance of a regime sample (few trades understate it)
POLICY_REFRESH_SECONDS = 900.0
POLICY_MAX_TRADES = 300  # most recent paired trades per arm used to choose its policy

MAX_CATCH_UP_BARS = 2000
DAY = 86400.0


@dataclass(frozen=True)
class EntryIntent:
    """A proposal to enter. Built from an arm's signal (or, in tests, from an external source)."""

    signal: Signal
    param_set_id: Optional[int]
    strategy: str
    description: str
    arm_id: Optional[int] = None
    tier: str = "EXT"
    timeframe: Optional[str] = None
    exploration: bool = False
    posterior: Optional[Posterior] = None
    thompson: Optional[float] = None
    bar_time: Optional[int] = None
    risk_pct: Optional[float] = None
    sizing: Optional[Mapping[str, Any]] = None
    min_lot_exception: bool = False
    policy: str = "fixed"
    regime: Optional[str] = None


@dataclass
class Arm:
    id: int
    symbol: str
    timeframe: str
    strategy: str
    params: dict
    param_set_id: int
    source: str
    tier: str = "T0"
    tier_since: float = 0.0
    last_change: Optional[float] = None
    pinned_until: Optional[float] = None
    active: bool = True
    prior_n: int = 0
    prior_mean: Optional[float] = None
    prior_var: Optional[float] = None
    policy: str = "fixed"
    posterior: Optional[Posterior] = None

    @property
    def family(self) -> str:
        return family_of(self.strategy)

    def describe(self) -> str:
        try:
            text = strategy_lib.describe(strategy_lib.get(self.strategy), self.params)
        except KeyError:
            text = self.strategy
        return f"{text} {self.symbol} {self.timeframe}"

    def short(self) -> str:
        return f"#{self.id} {self.strategy} {self.symbol} {self.timeframe}"

    def to_dict(self) -> dict[str, Any]:
        post = self.posterior.to_dict() if self.posterior else None
        return {"id": self.id, "symbol": self.symbol, "timeframe": self.timeframe, "strategy": self.strategy,
                "params": self.params, "param_set_id": self.param_set_id, "source": self.source, "tier": self.tier,
                "tier_since": self.tier_since, "last_change": self.last_change, "pinned_until": self.pinned_until,
                "active": self.active, "prior_n": self.prior_n, "prior_mean": self.prior_mean, "policy": self.policy,
                "family": self.family, "description": self.describe(), "posterior": post}


@dataclass(frozen=True)
class ArmSignal:
    arm: Arm
    signal: Signal
    bar_time: int


# ------------------------------------------------------------------- storage


class ArmStore:
    """Persistence of arms on the account database (via the registry's connection)."""

    def __init__(self, registry: Registry, clock: Callable[[], float] = time.time) -> None:
        self.registry = registry
        self.conn = registry.conn
        self.clock = clock

    def _arm(self, row: Any) -> Arm:
        strategy, params = self.registry.param_set(int(row["param_set_id"]))
        post = None
        try:
            data = json.loads(row["posterior_json"] or "{}")
            if data.get("mean") is not None:
                post = Posterior(**{k: data[k] for k in Posterior.__dataclass_fields__ if k in data})
        except (ValueError, TypeError):
            post = None
        return Arm(int(row["id"]), row["symbol"], row["timeframe"], strategy, params, int(row["param_set_id"]),
                   row["source"], row["tier"], float(row["tier_since"]), row["last_change"], row["pinned_until"],
                   bool(row["active"]), int(row["prior_n"] or 0), row["prior_mean"], row["prior_var"],
                   row["policy"] or "fixed", post)

    def load(self, active_only: bool = True) -> list[Arm]:
        sql = "SELECT * FROM arms" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        out = []
        for row in self.conn.execute(sql):
            try:
                out.append(self._arm(row))
            except KeyError:
                log.warning("arm %s references an unknown strategy; skipped", row["id"])
        return out

    def get(self, arm_id: int) -> Optional[Arm]:
        row = self.conn.execute("SELECT * FROM arms WHERE id = ?", (int(arm_id),)).fetchone()
        return self._arm(row) if row else None

    def upsert(self, symbol: str, timeframe: str, strategy: str, params: Mapping[str, Any], source: str) -> Arm:
        canon = strategy_lib.get(strategy).validate_params(params)
        ps = self.registry.param_set_id(strategy, canon)
        now = self.clock()
        self.conn.execute("INSERT OR IGNORE INTO arms (symbol, timeframe, strategy, param_set_id, source, tier,"
                          " tier_since, created) VALUES (?, ?, ?, ?, ?, 'T0', ?, ?)",
                          (symbol, timeframe, strategy, ps, source, now, now))
        self.conn.execute("UPDATE arms SET active = 1 WHERE symbol = ? AND timeframe = ? AND param_set_id = ?",
                          (symbol, timeframe, ps))
        row = self.conn.execute("SELECT * FROM arms WHERE symbol = ? AND timeframe = ? AND param_set_id = ?",
                                (symbol, timeframe, ps)).fetchone()
        return self._arm(row)

    def set_prior(self, arm_id: int, n: int, mean: Optional[float], var: Optional[float]) -> None:
        self.conn.execute("UPDATE arms SET prior_n = ?, prior_mean = ?, prior_var = ?, prior_ts = ? WHERE id = ?",
                          (int(n), mean, var, self.clock(), int(arm_id)))

    def set_active(self, arm_id: int, active: bool) -> None:
        self.conn.execute("UPDATE arms SET active = ? WHERE id = ?", (int(active), int(arm_id)))

    def save_posteriors(self, arms: Iterable[Arm]) -> None:
        rows = [(_json(a.posterior.to_dict()) if a.posterior else "{}", a.id) for a in arms]
        if rows:
            with self.registry.tx():
                self.conn.executemany("UPDATE arms SET posterior_json = ? WHERE id = ?", rows)

    def set_policy(self, arm: Arm, policy: str, reason: str, now: float) -> int:
        """Switch an arm's management policy and write the audit row in one transaction."""
        with self.registry.tx():
            self.conn.execute("UPDATE arms SET policy = ? WHERE id = ?", (policy, arm.id))
            cur = self.conn.execute(
                "INSERT INTO promotions (ts, symbol, action, old_param_set_id, new_param_set_id, new_candidate_id,"
                " old_desc, new_desc, reason, metrics_json) VALUES (?, ?, 'policy', ?, ?, NULL, ?, ?, ?, ?)",
                (now, arm.symbol, arm.param_set_id, arm.param_set_id, f"{arm.policy} {arm.short()}",
                 f"{policy} {arm.short()}", reason[:1000], _json({"arm_id": arm.id})))
        arm.policy = policy
        return int(cur.lastrowid)

    def change_tier(self, arm: Arm, decision: TierDecision, now: float, pinned_until: Optional[float] = None,
                    action: Optional[str] = None) -> int:
        """Move an arm to a new tier and write the audit row in one transaction."""
        metrics = arm.posterior.to_dict() if arm.posterior else {}
        action = action or f"tier_{decision.direction or 'set'}"
        with self.registry.tx():
            self.conn.execute("UPDATE arms SET tier = ?, tier_since = ?, last_change = ?, pinned_until = ? WHERE id = ?",
                              (decision.tier, now, now, pinned_until, arm.id))
            cur = self.conn.execute(
                "INSERT INTO promotions (ts, symbol, action, old_param_set_id, new_param_set_id, new_candidate_id,"
                " old_desc, new_desc, reason, metrics_json) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (now, arm.symbol, action, arm.param_set_id, arm.param_set_id, f"{decision.previous} {arm.short()}",
                 f"{decision.tier} {arm.short()}", decision.reason[:1000], _json(dict(metrics, arm_id=arm.id))))
        arm.tier, arm.tier_since, arm.last_change, arm.pinned_until = decision.tier, now, now, pinned_until
        return int(cur.lastrowid)


# --------------------------------------------------------------- shadow book


def _regime_at(closed: np.ndarray, index: int) -> str:
    """Regime of the signal bar (memoised per bar series, so every arm shares one computation)."""
    try:
        return regime.label_at(closed, index)
    except Exception:  # noqa: BLE001 - the tag is optional evidence; the trade is not
        log.debug("regime tag failed", exc_info=True)
        return ""


class ShadowBook:
    """Paper-trades one arm. Never sends orders.

    Bar processing order is identical to ``backtest.simulate``: pending exit at the open,
    pending entry at the open, SL/TP inside the bar, then the signal of the bar that just
    closed becomes pending for the next one. State survives restarts via the registry.
    Sized at a fixed reference equity: R-multiples are independent of the account size, so
    a small account collects exactly the same evidence as a large one.
    """

    def __init__(self, registry: Registry, arm: Arm, equity: float, risk_pct: float) -> None:
        self.registry = registry
        self.arm = arm
        self.strategy = strategy_lib.get(arm.strategy)
        self.key = f"arm_shadow:{arm.id}"
        self.equity = equity
        self.risk_pct = risk_pct
        self.pos: Optional[VirtualPosition] = None
        self.trade_id: Optional[int] = None
        self.pending = 0
        self.pending_atr = 0.0
        self.pending_bar = 0
        self.pending_exit = False
        self.pending_regime = ""
        self.last_bar_time: Optional[int] = None
        self.track_policies = True  # replay each closed trade under every management policy
        self._load()

    def _load(self) -> None:
        raw = self.registry.get_kv(self.key)
        if raw:
            data = json.loads(raw)
            self.pending = int(data.get("pending", 0))
            self.pending_atr = float(data.get("pending_atr", 0.0))
            self.pending_bar = int(data.get("pending_bar", 0))
            self.pending_exit = bool(data.get("pending_exit", False))
            self.pending_regime = str(data.get("pending_regime") or "")
            self.last_bar_time = data.get("last_bar_time")
        rows = self.registry.trades(mode="shadow", status="open", arm_id=self.arm.id)
        if rows:
            row = rows[-1]
            self.trade_id = row.id
            self.pos = VirtualPosition(1 if row.side == "BUY" else -1, int(row.extra.get("entry_index", -1)),
                                       int(row.open_bar_time or 0), row.entry, row.sl, row.tp, row.volume,
                                       row.risk_money)

    def _save(self) -> None:
        self.registry.set_kv(self.key, json.dumps({
            "pending": self.pending, "pending_atr": self.pending_atr, "pending_bar": self.pending_bar,
            "pending_exit": self.pending_exit, "pending_regime": self.pending_regime,
            "last_bar_time": self.last_bar_time}))

    def _close(self, closed: np.ndarray, index: int, bar_time: int, price: float, reason: str, spec: SymbolSpec,
               costs: CostModel) -> TradeRow | None:
        assert self.pos is not None
        trade = close_position(self.pos, index, bar_time, price, reason, spec, costs)
        if self.trade_id is not None:
            self.registry.close_trade(self.trade_id, price, trade.pnl, trade.r, reason, close_bar_time=bar_time)
            if self.track_policies:
                self._record_policies(closed, index, price, reason, spec, costs)
        self.pos, self.trade_id = None, None
        return trade  # type: ignore[return-value]

    def _record_policies(self, closed: np.ndarray, index: int, price: float, reason: str, spec: SymbolSpec,
                         costs: CostModel) -> None:
        """Paired evidence for the management policies: this trade replayed under each of them."""
        assert self.pos is not None and self.trade_id is not None
        times = closed["time"].astype(np.int64)
        start = int(np.searchsorted(times, int(self.pos.entry_time)))
        if start >= len(times) or int(times[start]) != int(self.pos.entry_time) or start > index:
            return  # the entry bar left the window (a very long trade): no replay
        try:
            atr = indicators.bars_atr(closed, management.ATR_PERIOD)
            outcomes = management.replay_all(self.pos, closed, start, index, price, reason, atr, spec, costs)
        except Exception:  # noqa: BLE001 - policy evidence is optional; the trade itself is recorded
            log.debug("policy replay failed for arm %s", self.arm.id, exc_info=True)
            return
        self.registry.record_policy_outcomes(self.arm.id, self.trade_id, outcomes)

    def on_bars(self, closed: np.ndarray, sig: SignalArrays, spec: SymbolSpec, costs: CostModel) -> int:
        """Process every closed bar not seen yet. Returns the number of shadow trades closed."""
        if len(closed) < 2:
            return 0
        times = closed["time"].astype(np.int64)
        if self.last_bar_time is None:
            start = len(closed) - 1  # first sight: only arm the signal of the latest closed bar
        else:
            start = int(np.searchsorted(times, int(self.last_bar_time), side="right"))
            start = max(start, len(closed) - MAX_CATCH_UP_BARS)
        if start >= len(closed):
            return 0
        closed_count = 0
        first_sight = self.last_bar_time is None
        for i in range(start, len(closed)):
            bar = closed[i]
            t = int(bar["time"])
            o, h, lo, spr = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["spread"])
            if not first_sight:
                if self.pos is not None and self.pending_exit:
                    price = market_exit_price(self.pos, o, spr, spec.point, costs.slippage_points)
                    closed_count += self._close(closed, i, t, price, "opposite", spec, costs) is not None
                    self.pending_exit = False
                if self.pos is None and self.pending:
                    self.pos = open_position(self.pending, i, t, o, spr, self.pending_atr, sig.sl_mult, sig.tp_mult,
                                             spec, costs, self.equity, self.risk_pct)
                    side = self.pending
                    self.pending = 0
                    if self.pos is not None:
                        self.trade_id = self.registry.open_trade(
                            "shadow", self.arm.symbol, self.arm.param_set_id, "BUY" if side > 0 else "SELL",
                            self.pos.entry_price, self.pos.sl, self.pos.tp, self.pos.volume, self.pos.risk_money,
                            open_bar_time=t, arm_id=self.arm.id, timeframe=self.arm.timeframe, tier="T0",
                            extra={"entry_index": i, "strategy": self.arm.strategy, "signal_bar": self.pending_bar,
                                   "regime": self.pending_regime or None})
                if self.pos is not None:
                    hit = exit_for_bar(self.pos, o, h, lo, spr, spec.point, costs.slippage_points,
                                       self.pos.entry_time == t)
                    if hit is not None:
                        closed_count += self._close(closed, i, t, hit[0], hit[1], spec, costs) is not None
            s = int(sig.side[i])
            if s != 0:
                if self.pos is None:
                    self.pending, self.pending_atr, self.pending_bar = s, float(sig.atr[i]), t
                    self.pending_regime = _regime_at(closed, i)
                elif costs.close_on_opposite and s == -self.pos.side:
                    self.pending_exit = True
                    self.pending, self.pending_atr, self.pending_bar = s, float(sig.atr[i]), t
                    self.pending_regime = _regime_at(closed, i)
            elif self.pos is None:
                self.pending = 0
            self.last_bar_time = t
        self._save()
        return closed_count


# -------------------------------------------------------------------- engine


@dataclass(frozen=True)
class RegimeView:
    regime: str
    n: int
    raw_mean: float
    mean: float        # shrunk toward the arm's overall posterior mean
    p_positive: float


@dataclass
class _Evidence:
    observations: list[tuple[float, int, float, bool]] = field(default_factory=list)  # (close_ts, signal_bar, r, live)
    regimes: dict[int, str] = field(default_factory=dict)  # signal key -> regime of the signal bar


class ArmEngine:
    """Owns every arm: shadow books, posteriors, tiers and the per-bar signal scan."""

    def __init__(self, registry: Registry, tiers_cfg: Any, research_cfg: Any, clock: Callable[[], float] = time.time,
                 notify: Callable[[str], None] = lambda text: None,
                 on_tier_change: Optional[Callable[[Arm, TierDecision, int], None]] = None,
                 half_life_days: float = 120.0, management_cfg: Any = None) -> None:
        self.registry = registry
        self.store = ArmStore(registry, clock)
        self.clock = clock
        self.notify = notify
        self.on_tier_change = on_tier_change
        self.half_life_days = half_life_days
        self.management_cfg = management_cfg
        self.update_settings(tiers_cfg, research_cfg)
        self.arms: dict[int, Arm] = {a.id: a for a in self.store.load()}
        self.books: dict[int, ShadowBook] = {}
        self._signals_cache: dict[tuple[int, int], SignalArrays] = {}
        self._evidence: dict[int, _Evidence] = defaultdict(_Evidence)
        self._evidence_until = 0.0
        self._boundary_ids: set[int] = set()
        self._last_refresh = -math.inf
        self._last_saved: dict[int, float] = {}
        self._last_save_ts = -math.inf
        self._policy_rows: dict[int, dict[int, dict[str, float]]] = defaultdict(dict)
        self._policy_after = 0
        self._last_policy_refresh = -math.inf
        self.rng = np.random.default_rng(int(getattr(tiers_cfg, "seed", 20261026)))
        self._load_evidence()

    def update_settings(self, tiers_cfg: Any, research_cfg: Any, t1_enter_override: Optional[float] = None) -> None:
        self.tiers_cfg = tiers_cfg
        self.tier_settings = TierSettings.from_config(tiers_cfg, t1_enter_override)
        self.evidence_settings = EvidenceSettings(prior_discount=tiers_cfg.prior_discount, tau_min=tiers_cfg.tau_min,
                                                  global_mean=tiers_cfg.global_mean)
        self.ref_equity = float(research_cfg.reference_equity)
        self.ref_risk = float(research_cfg.reference_risk_pct)
        for book in getattr(self, "books", {}).values():
            book.equity, book.risk_pct = self.ref_equity, self.ref_risk

    # ------------------------------------------------------------- arm set

    def reload(self) -> None:
        fresh = {a.id: a for a in self.store.load()}
        for arm_id, arm in fresh.items():
            old = self.arms.get(arm_id)
            if old is not None and old.posterior is not None and arm.posterior is None:
                arm.posterior = old.posterior
        self.arms = fresh
        for arm_id in [k for k in self.books if k not in self.arms]:
            del self.books[arm_id]

    def ensure_arm(self, symbol: str, timeframe: str, strategy: str, params: Mapping[str, Any], source: str) -> Arm:
        arm = self.store.upsert(symbol, timeframe, strategy, params, source)
        if arm.id not in self.arms:
            self.arms[arm.id] = arm
        return self.arms[arm.id]

    def ensure_default_arms(self, symbols: Sequence[str], timeframes: Sequence[str], strategies: Sequence[str],
                            max_arms: int) -> int:
        """One default-parameter arm per symbol x timeframe x strategy (bounded by ``max_arms``)."""
        created = 0
        for symbol in symbols:
            for tf in timeframes:
                for name in strategies:
                    if len(self.arms) >= max_arms:
                        return created
                    before = len(self.arms)
                    self.ensure_arm(symbol, tf, name, strategy_lib.get(name).default_params(), "default")
                    created += len(self.arms) - before
        return created

    def arms_for(self, symbol: str, timeframe: str) -> list[Arm]:
        return [a for a in self.arms.values() if a.active and a.symbol == symbol and a.timeframe == timeframe]

    def symbols_timeframes(self) -> list[tuple[str, str]]:
        return sorted({(a.symbol, a.timeframe) for a in self.arms.values() if a.active})

    def _book(self, arm: Arm) -> ShadowBook:
        book = self.books.get(arm.id)
        if book is None:
            book = self.books[arm.id] = ShadowBook(self.registry, arm, self.ref_equity, self.ref_risk)
            book.track_policies = self.policies_enabled
        return book

    # -------------------------------------------------------------- per bar

    def on_bar(self, symbol: str, timeframe: str, closed: np.ndarray, spec: SymbolSpec,
               costs: CostModel) -> list[ArmSignal]:
        """Update every arm's shadow book on this symbol/timeframe; return the arms that signal now."""
        out: list[ArmSignal] = []
        if len(closed) < 2:
            return out
        bar_time = int(closed[-1]["time"])
        # One transaction for every shadow book of this bar: one commit instead of one per arm.
        with self.registry.tx():
            for arm in self.arms_for(symbol, timeframe):
                try:
                    sig = arm_signals(arm, closed)
                    self._book(arm).on_bars(closed, sig, spec, costs)
                except Exception:  # noqa: BLE001 - one arm must never stop the others
                    log.exception("%s: arm %s failed on this bar", symbol, arm.short())
                    continue
                side = int(sig.side[-1])
                if side != 0 and math.isfinite(float(sig.atr[-1])) and float(sig.atr[-1]) > 0:
                    out.append(ArmSignal(arm, Signal("BUY" if side > 0 else "SELL", float(sig.atr[-1]), sig.sl_mult,
                                                     sig.tp_mult), bar_time))
        return out

    def exit_signal(self, arm_id: Optional[int], param_set_id: Optional[int], side: str, closed: np.ndarray) -> bool:
        """True if the arm (or v3.0 parameter set) that OPENED a position now signals the opposite side."""
        try:
            if arm_id is not None and int(arm_id) in self.arms:
                arm = self.arms[int(arm_id)]
                sig = strategy_lib.get(arm.strategy).signal(closed, arm.params)
            elif param_set_id is not None:
                name, params = self.registry.param_set(int(param_set_id))
                sig = strategy_lib.get(name).signal(closed, params)
            else:
                return False
        except Exception:  # noqa: BLE001 - unknown origin: SL/TP keep protecting the position
            log.exception("cannot evaluate exit logic for arm %s / parameter set %s", arm_id, param_set_id)
            return False
        return sig is not None and sig.side != side

    # ------------------------------------------------------------- evidence

    def _load_evidence(self) -> None:
        rows = self.registry.conn.execute(
            "SELECT id, arm_id, mode, close_ts, open_bar_time, r, extra_json FROM trades WHERE arm_id IS NOT NULL AND"
            " status = 'closed' AND r IS NOT NULL ORDER BY close_ts")
        self._ingest(rows)

    def _ingest(self, rows: Iterable[Any]) -> int:
        count = 0
        for row in rows:
            if int(row["id"]) in self._boundary_ids:
                continue  # already read: closed at the same instant as the previous high-water mark
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except ValueError:
                extra = {}
            signal_bar = int(extra.get("signal_bar") or row["open_bar_time"] or 0)
            close_ts = float(row["close_ts"] or 0.0)
            ev = self._evidence[int(row["arm_id"])]
            ev.observations.append((close_ts, signal_bar, float(row["r"]), row["mode"] == "live"))
            if extra.get("regime"):
                ev.regimes[signal_bar or int(close_ts)] = str(extra["regime"])
            if close_ts > self._evidence_until:
                self._evidence_until, self._boundary_ids = close_ts, set()
            if close_ts == self._evidence_until:
                self._boundary_ids.add(int(row["id"]))
            count += 1
        return count

    def _ingest_new(self) -> int:
        # ">=" plus the ids seen at the high-water mark: trades closed at the same clock value as the
        # last read (a virtual clock in simulation, coarse clocks) are neither lost nor read twice.
        rows = self.registry.conn.execute(
            "SELECT id, arm_id, mode, close_ts, open_bar_time, r, extra_json FROM trades WHERE arm_id IS NOT NULL AND"
            " status = 'closed' AND r IS NOT NULL AND close_ts >= ? ORDER BY close_ts", (self._evidence_until,))
        return self._ingest(rows)

    def regime_view(self, arm: Arm, label: str) -> Optional[RegimeView]:
        """The arm's evidence in one regime, shrunk toward its overall posterior mean."""
        ev = self._evidence.get(arm.id)
        if ev is None or not label:
            return None
        by_signal: dict[int, tuple[float, bool]] = {}
        for close_ts, signal_bar, r, live in ev.observations:
            key = signal_bar or int(close_ts)
            if ev.regimes.get(key) == label and (live or key not in by_signal or not by_signal[key][1]):
                by_signal[key] = (r, live)
        rs = np.array([r for r, _live in by_signal.values()], dtype=float)
        if rs.size == 0:
            return None
        overall = arm.posterior.mean if arm.posterior is not None else self.evidence_settings.global_mean
        var = max(float(rs.var(ddof=1)) if rs.size > 1 else 1.0, REGIME_MIN_VAR)
        mean = (rs.sum() + REGIME_SHRINK * overall) / (rs.size + REGIME_SHRINK)
        sd = math.sqrt(var / (rs.size + REGIME_SHRINK))
        p = 0.5 * (1.0 + math.erf(mean / sd / math.sqrt(2.0)))
        return RegimeView(label, int(rs.size), float(rs.mean()), float(mean), p)

    def sample(self, arm: Arm, now: float) -> tuple[ArmSample, list[tuple[float, float, bool]]]:
        """The arm's evidence: one observation per signal (live replaces its shadow twin)."""
        by_signal: dict[int, tuple[float, float, bool]] = {}
        for close_ts, signal_bar, r, live in self._evidence.get(arm.id, _Evidence()).observations:
            key = signal_bar or int(close_ts)
            if key in by_signal and by_signal[key][2] and not live:
                continue
            by_signal[key] = (close_ts, r, live)
        seq = sorted(by_signal.values())
        decay = math.log(2) / (self.half_life_days * DAY) if self.half_life_days > 0 else 0.0
        es = self.evidence_settings
        weights = [math.exp(-decay * max(0.0, now - ts)) * (es.live_weight if lv else es.shadow_weight)
                   for ts, _r, lv in seq]
        sample = build_sample(arm.prior_n, arm.prior_mean, arm.prior_var, [r for _, r, _ in seq],
                              [lv for _, _, lv in seq], es, weights)
        return sample, seq

    def posteriors(self, now: Optional[float] = None) -> dict[int, Posterior]:
        now = self.clock() if now is None else now
        self._ingest_new()
        samples: dict[int, ArmSample] = {}
        families: dict[int, str] = {}
        for arm in self.arms.values():
            samples[arm.id], _seq = self.sample(arm, now)
            families[arm.id] = arm.family
        return fit(samples, families, self.evidence_settings)

    def refresh(self, force: bool = False) -> list[tuple[Arm, TierDecision]]:
        """Recompute posteriors and apply tier changes (audited). Returns the changes."""
        now = self.clock()
        if not force and now - self._last_refresh < self.tiers_cfg.evidence_refresh_seconds:
            return []
        self._last_refresh = now
        posts = self.posteriors(now)
        changes: list[tuple[Arm, TierDecision]] = []
        for arm in self.arms.values():
            arm.posterior = posts.get(arm.id)
            if arm.posterior is None or not arm.active:
                continue
            dd_since = self._drawdown_since(arm, arm.last_change or arm.tier_since)
            decision = decide_tier(arm.tier, arm.posterior, self.tier_settings, now, arm.last_change,
                                   arm.pinned_until, dd_since)
            if decision.changed:
                audit_id = self.store.change_tier(arm, decision, now)
                changes.append((arm, decision))
                text = f"{arm.symbol}: {arm.short()} {decision.previous} -> {decision.tier}: {decision.reason}"
                (log.warning if decision.direction == "down" else log.info)(text)
                if self.on_tier_change is not None:
                    try:
                        self.on_tier_change(arm, decision, audit_id)
                    except Exception:  # noqa: BLE001 - observers must never break the engine
                        log.debug("tier observer failed", exc_info=True)
        self._save_posteriors(now, force=bool(changes))
        self.refresh_policies(now)
        return changes

    # --------------------------------------------------------- management

    @property
    def policies_enabled(self) -> bool:
        return self.management_cfg is None or bool(self.management_cfg.policies_enabled)

    def refresh_policies(self, now: Optional[float] = None,
                         force: bool = False) -> list[tuple[Arm, management.PolicyChoice]]:
        """Re-choose every arm's management policy from the paired replays (audited)."""
        now = self.clock() if now is None else now
        if self.management_cfg is None or not self.policies_enabled:
            return []
        if not force and now - self._last_policy_refresh < POLICY_REFRESH_SECONDS:
            return []
        self._last_policy_refresh = now
        for row_id, arm_id, trade_id, policy, r in self.registry.policy_outcomes(self._policy_after):
            self._policy_rows[arm_id].setdefault(trade_id, {})[policy] = r
            self._policy_after = row_id
        paired: dict[int, dict[str, list[float]]] = {}
        for arm_id, trades in self._policy_rows.items():
            for old in sorted(trades)[:-POLICY_MAX_TRADES]:
                del trades[old]  # only the most recent trades of each arm count
            complete = [trades[t] for t in sorted(trades) if all(p in trades[t] for p in management.POLICIES)]
            paired[arm_id] = {p: [rs[p] for rs in complete] for p in management.POLICIES}
        m = self.management_cfg
        active = [a for a in self.arms.values() if a.active]
        choices = management.choose_for_arms(active, paired, int(m.policy_min_trades), float(m.policy_min_p_better))
        changed: list[tuple[Arm, management.PolicyChoice]] = []
        for arm in active:
            choice = choices.get(arm.id)
            if choice is None or not choice.changed:
                continue
            self.store.set_policy(arm, choice.policy, choice.reason, now)
            log.info("%s: %s management policy -> %s (%s)", arm.symbol, arm.short(), choice.policy, choice.reason)
            changed.append((arm, choice))
        return changed

    def _save_posteriors(self, now: float, force: bool = False, every: float = 600.0) -> None:
        """Persist posteriors that moved (for the panel and --arms) at most every ``every`` seconds."""
        if not force and now - self._last_save_ts < every:
            return
        self._last_save_ts = now
        moved = [a for a in self.arms.values() if a.posterior is not None and
                 abs(a.posterior.p_positive - self._last_saved.get(a.id, -1.0)) > 0.002]
        self.store.save_posteriors(moved)
        for arm in moved:
            self._last_saved[arm.id] = arm.posterior.p_positive  # type: ignore[union-attr]

    def _drawdown_since(self, arm: Arm, since: float) -> float:
        rs = [r for ts, _bar, r, _live in self._evidence.get(arm.id, _Evidence()).observations if ts >= since]
        if not rs:
            return 0.0
        curve = np.cumsum(rs)
        peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
        return float(np.max(peak - curve))

    def pin(self, arm_id: int, tier: str, hours: float, reason: str) -> int:
        """Manual override (``--rollback``): set an arm's tier and pin it against promotions."""
        arm = self.arms.get(int(arm_id)) or self.store.get(int(arm_id))
        if arm is None:
            raise KeyError(f"arm {arm_id} not found")
        if tier not in TIER_INDEX:
            raise ValueError(f"tier must be one of T0..T3, got {tier!r}")
        now = self.clock()
        decision = TierDecision(tier, arm.tier, f"manual: {reason}")
        return self.store.change_tier(arm, decision, now, pinned_until=now + hours * 3600, action="manual")

    # --------------------------------------------------------------- queries

    def counts(self) -> dict[str, int]:
        out = {t: 0 for t in TIER_INDEX}
        for arm in self.arms.values():
            if arm.active:
                out[arm.tier] = out.get(arm.tier, 0) + 1
        return out

    def ranked(self, limit: int = 50) -> list[Arm]:
        def key(a: Arm) -> tuple:
            p = a.posterior.p_positive if a.posterior else 0.0
            return (-TIER_INDEX.get(a.tier, 0), -p, a.id)

        return sorted((a for a in self.arms.values() if a.active), key=key)[:limit]


def arm_signals(arm: Arm, closed: np.ndarray) -> SignalArrays:
    return strategy_lib.get(arm.strategy).signals(closed, arm.params)
