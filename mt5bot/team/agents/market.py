"""Market Intelligence: data quality, regime, sessions, news calendar, correlation/exposure.

These are context agents. They run first in the decision protocol; their ``details`` are
handed to the voters as views. The Data Clerk, News Sentinel, Correlation Analyst and
Session Analyst are GATES: they can only veto or shrink an entry.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..agent import Agent, AgentState, Opinion
from ..context import DecisionContext
from ..events import RegimeChanged

from ...regime import LABEL_PT, regime_features, regime_probabilities


# ================================================================== Data Clerk


class DataClerk(Agent):
    """Gaps, stale bars or ticks, bad spreads, broken OHLC, timestamp drift. ``data_unhealthy``
    pauses entries in EVERY team mode (it is a safety gate, not an opinion)."""

    id = "data_clerk"
    MAX_INTRADAY_GAP_BARS = 8

    def analyze(self, ctx: DecisionContext) -> Opinion:
        m = ctx.market
        bars = m.closed
        problems: list[str] = []
        warnings: list[str] = []
        if len(bars) < 50:
            problems.append(f"only {len(bars)} closed bars")
        else:
            times = bars["time"].astype(np.int64)
            o, h, lo, c = (np.asarray(bars[k], dtype=float) for k in ("open", "high", "low", "close"))
            if not np.all(np.diff(times) > 0):
                problems.append("bar timestamps are not strictly increasing")
            if not (np.isfinite(o).all() and np.isfinite(c).all() and (c > 0).all()):
                problems.append("non-finite or non-positive prices")
            tail = slice(-200, None)
            if np.any(h[tail] + 1e-12 < np.maximum(o[tail], c[tail])) or np.any(lo[tail] - 1e-12 >
                                                                                  np.minimum(o[tail], c[tail])):
                problems.append("high/low inconsistent with open/close")
            tail_times = times[-200:]
            gaps = np.diff(tail_times) / m.bar_seconds
            weekend = self._weekend_gaps(tail_times)
            big = gaps[(gaps > self.MAX_INTRADAY_GAP_BARS) & ~weekend].tolist()
            if big:
                warnings.append(f"{len(big)} intraday gap(s) up to {max(big):.0f} bars")
            # Judged against the terminal's own latest tick (server time), not the PC clock:
            # fresh ticks with lagging bars is a data problem; stale ticks are checked below.
            if m.tick_time:
                lag = int(m.tick_time) - (int(times[-1]) + m.bar_seconds)
                if lag > 3 * m.bar_seconds:
                    problems.append(f"last closed bar lags the latest tick by {lag / 60:.0f} min")
                if lag < -60:
                    warnings.append("tick time is before the end of the last closed bar (clock drift)")
            spreads = np.asarray(bars["spread"][-500:], dtype=float)
            typical = float(np.median(spreads)) if spreads.size else 0.0
            if typical > 0 and m.spread_points > 4 * typical and m.spread_points > 3:
                warnings.append(f"spread {m.spread_points:.0f} pts vs typical {typical:.0f}")
        stale = float(getattr(self.cfg, "stale_tick_minutes", 5.0)) * 60
        if m.tick_age_seconds > stale:
            problems.append(f"no fresh tick for {m.tick_age_seconds / 60:.0f} min")
        healthy = not problems
        self.stats.update(healthy=healthy, problems=problems, warnings=warnings)
        if healthy:
            self.set_state(AgentState.MONITORING, task=f"{m.symbol}: dados limpos",
                           message=f"{m.symbol}: dados verificados" + (f" ({warnings[0]})" if warnings else ""))
        else:
            self.set_state(AgentState.MONITORING, task=f"{m.symbol}: dados maus",
                           message=f"{m.symbol}: dados maus, entradas em pausa: {problems[0]}")
        reasons = [("DATA_OK", "data checks passed")] if healthy else [("DATA_UNHEALTHY", p) for p in problems]
        return self.opinion(ctx, "abstain" if healthy else "veto", 1.0 if not healthy else 0.0, reasons,
                            size_hint=1.0 if healthy else 0.0, features=("time", "ohlc", "spread", "tick_age"),
                            details={"healthy": healthy, "problems": problems, "warnings": warnings})

    @staticmethod
    def _weekend_gaps(times: np.ndarray) -> np.ndarray:
        """True where the gap between consecutive bars spans a weekend (vectorised)."""
        t = np.asarray(times, dtype=np.int64)
        weekday = ((t // 86400) + 3) % 7  # 1970-01-01 was a Thursday; Monday = 0
        a, b = weekday[:-1], weekday[1:]
        return (a >= 4) & ((b <= 1) | (np.diff(t) >= 86400))


# =============================================================== Regime Detector


class RegimeDetector(Agent):
    id = "regime_detector"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._last: dict[str, str] = {}
        self._cache: dict[str, tuple[int, dict, float]] = {}

    def analyze(self, ctx: DecisionContext) -> Opinion:
        m = ctx.market
        cached = self._cache.get(m.symbol)
        if cached is not None and cached[0] == m.bar_time:
            f, typical = cached[1], cached[2]
        else:  # once per symbol and bar (the observation pass and the decision share it)
            f = regime_features(m.closed)
            spreads = np.asarray(m.closed["spread"][-500:], dtype=float)
            typical = float(np.median(spreads)) if spreads.size else 0.0
            self._cache[m.symbol] = (m.bar_time, f, typical)
        ratio = m.spread_points / typical if typical > 0 else 1.0
        probs = regime_probabilities(f, ratio)
        label = max(probs, key=probs.get)
        if self._last.get(m.symbol) != label:
            self._last[m.symbol] = label
            self.bus.publish(RegimeChanged(label, {k: round(v, 3) for k, v in probs.items()}), agent=self.id,
                             symbol=m.symbol)
        self.stats.setdefault("regimes", {})[m.symbol] = {"label": label, "probs": probs, **f}
        self.set_state(AgentState.ANALYZING, task=f"{m.symbol}: regime {LABEL_PT[label]}",
                       message=f"{m.symbol}: regime {LABEL_PT[label]} ({probs[label]:.0%})")
        return self.opinion(ctx, "abstain", probs[label], [("REGIME", f"{label} p={probs[label]:.2f}")],
                            features=("efficiency_ratio", "adx", "atr_percentile", "tick_volume"),
                            details={"regime": label, "probs": probs, **f, "spread_ratio": ratio})


# ============================================================ Session Analyst


def _last_sunday(year: int, month: int) -> datetime:
    day = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1) if month < 12 else \
        datetime(year, 12, 31, tzinfo=timezone.utc)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(days=7 * (n - 1))


def eu_summer(utc: datetime) -> bool:
    start = _last_sunday(utc.year, 3).replace(hour=1)
    end = _last_sunday(utc.year, 10).replace(hour=1)
    return start <= utc < end


def us_summer(utc: datetime) -> bool:
    start = _nth_sunday(utc.year, 3, 2).replace(hour=7)
    end = _nth_sunday(utc.year, 11, 1).replace(hour=6)
    return start <= utc < end


def session_windows(utc: datetime) -> dict[str, tuple[float, float]]:
    """UTC hour windows [start, end) for Tokyo, London and New York on this date (DST-aware)."""
    london = (7.0, 16.0) if eu_summer(utc) else (8.0, 17.0)
    new_york = (12.0, 21.0) if us_summer(utc) else (13.0, 22.0)
    return {"tokyo": (0.0, 9.0), "london": london, "new_york": new_york}


def session_view(utc_ts: float) -> dict[str, Any]:
    utc = datetime.fromtimestamp(utc_ts, tz=timezone.utc)
    hour = utc.hour + utc.minute / 60.0
    windows = session_windows(utc)
    sessions = {}
    for name, (start, end) in windows.items():
        is_open = start <= hour < end and utc.weekday() < 5
        sessions[name] = {"open": is_open, "start_utc": start, "end_utc": end,
                          "minutes_to_change": round(((end - hour) if is_open else ((start - hour) % 24)) * 60)}
    rollover_start = windows["new_york"][1] - 0.5
    rollover = rollover_start <= hour < rollover_start + 1.5
    open_now = [n for n, s in sessions.items() if s["open"]]
    return {"sessions": open_now, "overlap": sessions["london"]["open"] and sessions["new_york"]["open"],
            "rollover": rollover, "detail": sessions, "utc_hour": round(hour, 2), "weekday": utc.weekday()}


class SessionAnalyst(Agent):
    id = "session_analyst"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self._seasonality: dict[str, tuple[int, dict[str, float], float]] = {}

    def analyze(self, ctx: DecisionContext) -> Opinion:
        m = ctx.market
        view = session_view(m.utc_now)
        cached = self._seasonality.get(m.symbol)
        if cached is None or cached[0] != m.bar_time:
            spreads = np.asarray(m.closed["spread"], dtype=float)
            hours = (np.asarray(m.closed["time"], dtype=np.int64) % 86400) // 3600
            cached = (m.bar_time, self._spread_by_hour(hours, spreads),
                      float(np.median(spreads)) if spreads.size else 0.0)
            self._seasonality[m.symbol] = cached
        by_hour, overall = cached[1], cached[2]
        server_hour = int((m.server_now % 86400) // 3600)
        typical = by_hour.get(str(server_hour), overall)
        ratio = m.spread_points / typical if typical > 0 else 1.0
        size = 1.0
        reasons = []
        if view["rollover"]:
            size = min(size, 0.5)
            reasons.append(("SESSION_ILLIQUID", "rollover window: thin liquidity, wide spreads"))
        if ratio > 2.0:
            size = min(size, 0.6)
            reasons.append(("SPREAD_WIDE", f"spread {ratio:.1f}x its usual level for this hour"))
        if not view["sessions"]:
            reasons.append(("SESSION_ILLIQUID", "no major session open"))
        view.update(spread_ratio=round(ratio, 3), typical_spread_for_hour=round(typical, 2), spread_by_hour=by_hour)
        self.stats.setdefault("sessions", {})[m.symbol] = view
        names = ", ".join(view["sessions"]) or "nenhuma"
        self.set_state(AgentState.SCANNING, task=f"sessões: {names}",
                       message=f"Sessões abertas: {names}" + (" (overlap Londres/NY)" if view["overlap"] else ""))
        return self.opinion(ctx, "abstain", 0.0, reasons or [("SESSION_OK", f"open: {names}")], size_hint=size,
                            features=("utc_time", "spread_by_hour"), details=view)

    @staticmethod
    def _spread_by_hour(hours: np.ndarray, spreads: np.ndarray) -> dict[str, float]:
        """Median spread per server hour, needing at least 5 bars in an hour."""
        order = np.argsort(hours, kind="stable")
        hours, spreads = hours[order], spreads[order]
        bounds = np.searchsorted(hours, np.arange(25))
        out = {}
        for h in range(24):
            sel = spreads[bounds[h]:bounds[h + 1]]
            if sel.size >= 5:
                out[str(h)] = round(float(np.median(sel)), 2)
        return out


# ============================================================== News Sentinel


class NewsSentinel(Agent):
    """Optional local calendar (CSV: time_utc,currency,impact,title) and optional headlines
    file. Without either it simply reports "no calendar" and never blocks anything."""

    id = "news_sentinel"

    def __init__(self, services: Any) -> None:
        super().__init__(services)
        self.events: list[tuple[float, str, str, str]] = []
        self.headlines: list[str] = []
        self._mtimes: dict[str, float] = {}
        self.calendar_status = "no calendar"

    def _path(self) -> Optional[Path]:
        news = self.cfg.news
        name = news.calendar_file or (self.cfg.news_file if self.cfg.news_filter_enabled else None)
        return Path(name) if name else None

    def _reload(self) -> None:
        path = self._path()
        if path is not None:
            try:
                mtime = path.stat().st_mtime
                if self._mtimes.get("cal") != mtime:
                    self._mtimes["cal"] = mtime
                    self.events = self._read_calendar(path)
                    self.calendar_status = f"{len(self.events)} events from {path.name}"
            except OSError:
                self.events = []
                self.calendar_status = f"calendar file {path.name} not found"
        hpath = self.cfg.news.headlines_file
        if hpath:
            try:
                p = Path(hpath)
                mtime = p.stat().st_mtime
                if self._mtimes.get("head") != mtime:
                    self._mtimes["head"] = mtime
                    text = p.read_text(encoding="utf-8-sig")
                    try:
                        data = json.loads(text)
                        items = data if isinstance(data, list) else []
                    except ValueError:
                        items = text.splitlines()
                    self.headlines = [str(x) for x in items if str(x).strip()][-12:]
            except OSError:
                self.headlines = []

    def _read_calendar(self, path: Path) -> list[tuple[float, str, str, str]]:
        levels = set(self.cfg.news.impact_levels)
        out = []
        with open(path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    stamp = (row.get("time_utc") or "").strip().replace("Z", "+00:00")
                    when = datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                impact = (row.get("impact") or "high").strip().lower()  # unknown impact counts as high
                if impact not in levels:
                    continue
                out.append((when.timestamp(), (row.get("currency") or "*").strip().upper(), impact,
                            " ".join((row.get("title") or "event").split())[:120]))
        return sorted(out)

    def upcoming(self, now: float, currencies: set[str], horizon_hours: float = 24.0) -> list[dict]:
        out = []
        for ts, ccy, impact, title in self.events:
            if now - 3600 <= ts <= now + horizon_hours * 3600 and (ccy in currencies or ccy == "*" or not currencies):
                out.append({"ts": ts, "currency": ccy, "impact": impact, "title": title,
                            "minutes": round((ts - now) / 60)})
        return out

    def analyze(self, ctx: DecisionContext) -> Opinion:
        if not self.cfg.news.sentinel_enabled:
            return self.opinion(ctx, "abstain", 0.0, [("NEWS_OFF", "sentinel disabled")], details={"enabled": False})
        self._reload()
        m = ctx.market
        now = m.utc_now
        currencies = {m.currency_base, m.currency_profit} - {""}
        before = self.cfg.news.blackout_minutes_before * 60
        after = self.cfg.news.blackout_minutes_after * 60
        blocking = [e for e in self.events if (e[1] in currencies or e[1] == "*") and e[0] - before <= now <= e[0] + after]
        upcoming = self.upcoming(now, currencies)
        nxt = next((u for u in upcoming if u["minutes"] >= 0), None)
        details = {"calendar": self.calendar_status, "upcoming": upcoming[:10], "next": nxt,
                   "headlines": list(self.headlines), "blackout": bool(blocking)}
        self.stats["news"] = details
        if blocking:
            ts, ccy, impact, title = blocking[0]
            text = f"{ccy} {title} ({impact}) at {datetime.fromtimestamp(ts, tz=timezone.utc):%H:%M} UTC"
            self.set_state(AgentState.MONITORING, task="janela de notícias", message=f"Bloqueio: {text}")
            return self.opinion(ctx, "veto", 1.0, [("NEWS_RISK", text)], size_hint=0.0, details=details)
        if nxt is not None:
            self.set_state(AgentState.SCANNING, task="calendário",
                           message=f"Próximo evento {nxt['currency']} em {nxt['minutes']} min: {nxt['title']}")
        else:
            self.set_state(AgentState.SCANNING, task="calendário", message=f"Sem eventos próximos ({self.calendar_status})")
        return self.opinion(ctx, "abstain", 0.0, [("NEWS_CLEAR", self.calendar_status)], details=details)


# ===================================================== Correlation / exposure


def currency_exposure(positions: Any, extra: Optional[tuple[str, str, str, float]] = None) -> dict[str, float]:
    """Net long(+)/short(-) per currency in % of equity at risk."""
    exposure: dict[str, float] = {}

    def add(base: str, quote: str, side: str, pct: float) -> None:
        sign = 1.0 if side == "BUY" else -1.0
        if base:
            exposure[base] = exposure.get(base, 0.0) + sign * pct
        if quote:
            exposure[quote] = exposure.get(quote, 0.0) - sign * pct

    for p in positions:
        add(p.currency_base or p.symbol[:3], p.currency_profit or p.symbol[3:6], p.side, p.risk_pct)
    if extra is not None:
        add(*extra)
    return {k: round(v, 4) for k, v in exposure.items()}


def correlation_matrix(series: dict[str, np.ndarray], lookback: int) -> tuple[list[str], np.ndarray]:
    names = sorted(series)
    rets = []
    for name in names:
        closes = np.asarray(series[name], dtype=float)[-(lookback + 1):]
        rets.append(np.diff(np.log(closes)) if closes.size > 2 else np.zeros(0))
    n = min((r.size for r in rets), default=0)
    if n < 20 or len(names) < 2:
        return names, np.eye(len(names))
    mat = np.vstack([r[-n:] for r in rets])
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(mat)
    return names, np.nan_to_num(corr, nan=0.0)


class CorrelationAnalyst(Agent):
    id = "correlation_analyst"

    def analyze(self, ctx: DecisionContext) -> Opinion:
        m = ctx.market
        team = self.cfg.team
        series = {m.symbol: np.asarray(m.closed["close"], dtype=float)}
        series.update({k: v for k, v in ctx.others.items() if v is not None and len(v) > 2})
        names, corr = correlation_matrix(series, team.correlation_lookback_bars)
        details: dict[str, Any] = {"symbols": names, "corr": [[round(float(x), 3) for x in row] for row in corr]}
        positions = ctx.account.positions
        proposal = ctx.proposal
        if proposal is None:
            details["exposure"] = currency_exposure(positions)
            self.stats["correlation"] = details
            return self.opinion(ctx, "abstain", 0.0, [("EXPOSURE", "no proposal")], details=details)
        extra = (m.currency_base or m.symbol[:3], m.currency_profit or m.symbol[3:6], proposal.side,
                 ctx.account.risk_pct)
        before = currency_exposure(positions)
        after = currency_exposure(positions, extra)
        details.update(exposure=before, exposure_after=after)
        duplicates = []
        idx = {n: i for i, n in enumerate(names)}
        for p in positions:
            if p.symbol == m.symbol or p.symbol not in idx or m.symbol not in idx:
                continue
            rho = float(corr[idx[m.symbol], idx[p.symbol]])
            same_direction = (p.side == proposal.side) == (rho > 0)
            if abs(rho) >= team.correlation_threshold and same_direction:
                duplicates.append({"symbol": p.symbol, "rho": round(rho, 3), "side": p.side})
        concentration = max((abs(v) for v in after.values()), default=0.0)
        limit = 2.0 * max(ctx.account.risk_pct, 1e-9)
        details["duplicates"] = duplicates
        self.stats["correlation"] = details
        if len(duplicates) >= 2:
            self.set_state(AgentState.ANALYZING, task="exposição", message=f"{m.symbol}: risco duplicado com "
                           f"{', '.join(d['symbol'] for d in duplicates)}: veto")
            return self.opinion(ctx, "veto", 1.0, [("CORRELATION_RISK", f"duplicates {duplicates}")], size_hint=0.0,
                                details=details)
        size, reasons = 1.0, []
        if duplicates:
            size = 0.5
            d = duplicates[0]
            reasons.append(("CORRELATION_RISK", f"same risk as open {d['side']} {d['symbol']} (rho {d['rho']:+.2f})"))
        if concentration > limit + 1e-9:
            size = min(size, 0.5)
            reasons.append(("CORRELATION_RISK", f"currency concentration {concentration:.2f}% > {limit:.2f}%"))
        self.set_state(AgentState.ANALYZING, task="exposição por moeda",
                       message=f"{m.symbol}: {'risco duplicado, metade do tamanho' if size < 1 else 'exposição ok'}")
        return self.opinion(ctx, "abstain", 0.0, reasons or [("EXPOSURE_OK", "no duplicate risk")], size_hint=size,
                            details=details)
