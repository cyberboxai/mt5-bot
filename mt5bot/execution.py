"""Execution engine: zero duplicate orders, zero unknown states, zero unprotected positions.

Every order goes through a state machine persisted BEFORE it is sent (write-ahead):

    INTENT -> SUBMITTED -> ACKED | UNCERTAIN -> FILLED | PARTIALLY_FILLED | REJECTED | CANCELLED

* The client id is a deterministic hash of the decision and travels in the order comment, so
  a decision maps to one order even across restarts.
* An answer that does not prove the outcome (no answer, an exception, a timeout, a connection
  error, an unknown code) makes the order UNCERTAIN. It is reconciled against positions, orders
  and deals. It is NEVER sent again: an entry signal is stale by the time its fate is known.
* Only definite non-executions from the configured set are retried, with exponential backoff
  and a fresh price, and only after reconciliation shows nothing happened.
* After a fill the position must exist with its SL and TP. Missing stops are attached at once,
  and if that fails the position is closed. An unprotected position always raises an alert.
* A broker circuit breaker stops new entries during outages. It never blocks a close.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Optional

from .core import orders as oc
from .core.breaker import BreakerSettings, BrokerBreaker
from .core.broker import DEAL_ENTRY_IN, BrokerReply, OrderRequest, side_of_position
from .core.orders import OrderState, Outcome
from .eventlog import EventLog, canonical
from .ports import BrokerPort

log = logging.getLogger("mt5_bot")

BREAKER_KEY = "broker_breaker"
DEAL_LOOKBACK_SECONDS = 86400.0   # broker server time is ahead of UTC: search around "now"
_S = OrderState


@dataclass(frozen=True)
class ExecutionSettings:
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    confirm_polls: int = 6
    confirm_poll_seconds: float = 0.25
    uncertain_grace_seconds: float = 120.0
    protect_attempts: int = 3
    protect_retry_seconds: float = 0.5
    stop_tolerance_points: float = 2.0
    retryable: frozenset[int] = oc.DEFAULT_RETRYABLE
    breaker: BreakerSettings = BreakerSettings()


@dataclass
class OrderRecord:
    client_id: str
    kind: str
    state: OrderState
    symbol: str
    side: Optional[str]
    volume: float
    decision_id: Optional[str] = None
    filled_volume: float = 0.0
    price: Optional[float] = None
    fill_price: Optional[float] = None
    ticket: Optional[int] = None
    position_ticket: Optional[int] = None
    attempts: int = 0
    retcode: Optional[int] = None
    created_ts: float = 0.0
    updated_ts: float = 0.0
    submitted_ts: Optional[float] = None
    resolved_ts: Optional[float] = None
    protected: bool = False
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Found:
    """What reconciliation learned about an order. ``status``: position, deal, pending, absent
    (the broker answered and holds nothing with this id) or unknown (the broker did not answer)."""

    status: str
    ticket: Optional[int] = None
    position_ticket: Optional[int] = None
    volume: float = 0.0
    price: float = 0.0
    deal: Optional[int] = None


@dataclass(frozen=True)
class ExecReport:
    """Outcome of one order for the caller. ``code`` is a funnel reason code."""

    client_id: str
    state: OrderState
    ok: bool
    code: str
    reason: str
    filled_volume: float = 0.0
    fill_price: float = 0.0
    ticket: Optional[int] = None
    position_ticket: Optional[int] = None
    deal: Optional[int] = None
    request: Optional[OrderRequest] = None
    attempts: int = 0
    latency_ms: float = 0.0
    requotes: int = 0
    rejects: int = 0
    spread_submit: float = 0.0
    retcode: Optional[int] = None
    protection: str = ""


class OrderJournal:
    """The ``orders`` projection, updated in one transaction with its ledger event."""

    COLUMNS = ("kind", "state", "symbol", "side", "volume", "filled_volume", "price", "fill_price", "ticket",
               "position_ticket", "attempts", "retcode", "decision_id", "created_ts", "updated_ts", "submitted_ts",
               "resolved_ts", "protected", "detail", "meta_json")

    def __init__(self, registry: Any, events: EventLog, clock: Callable[[], float]) -> None:
        self.registry = registry
        self.conn = registry.conn
        self.events = events
        self.clock = clock

    @staticmethod
    def _record(row: Any) -> OrderRecord:
        return OrderRecord(row["client_id"], row["kind"], OrderState(row["state"]), row["symbol"], row["side"],
                           float(row["volume"]), row["decision_id"], float(row["filled_volume"]), row["price"],
                           row["fill_price"], row["ticket"], row["position_ticket"], int(row["attempts"]),
                           row["retcode"], float(row["created_ts"]), float(row["updated_ts"]), row["submitted_ts"],
                           row["resolved_ts"], bool(row["protected"]), row["detail"] or "",
                           json.loads(row["meta_json"] or "{}"))

    def get(self, cid: str) -> Optional[OrderRecord]:
        row = self.conn.execute("SELECT * FROM orders WHERE client_id = ?", (cid,)).fetchone()
        return self._record(row) if row else None

    def unresolved(self, symbol: Optional[str] = None) -> list[OrderRecord]:
        marks = ",".join("?" * len(oc.UNRESOLVED))
        args: list[Any] = [s.value for s in sorted(oc.UNRESOLVED, key=lambda x: x.value)]
        sql = f"SELECT * FROM orders WHERE state IN ({marks})"
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        return [self._record(r) for r in self.conn.execute(sql + " ORDER BY created_ts", args)]

    def in_state(self, state: OrderState) -> list[OrderRecord]:
        rows = self.conn.execute("SELECT * FROM orders WHERE state = ? ORDER BY created_ts", (state.value,))
        return [self._record(r) for r in rows]

    def count_for_position(self, kind: str, position: int) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM orders WHERE kind = ? AND position_ticket = ?",
                                (kind, int(position))).fetchone()
        return int(row["n"])

    def _write(self, rec: OrderRecord) -> None:
        values = (rec.client_id, rec.kind, rec.state.value, rec.symbol, rec.side, rec.volume, rec.filled_volume,
                  rec.price, rec.fill_price, rec.ticket, rec.position_ticket, rec.attempts, rec.retcode,
                  rec.decision_id, rec.created_ts, rec.updated_ts, rec.submitted_ts, rec.resolved_ts,
                  int(rec.protected), rec.detail, canonical(rec.meta))
        marks = ",".join("?" * len(values))
        self.conn.execute(f"INSERT OR REPLACE INTO orders (client_id, {', '.join(self.COLUMNS)}) VALUES ({marks})",
                          values)

    def create(self, rec: OrderRecord) -> OrderRecord:
        """Write-ahead: durable before anything is sent."""
        now = float(self.clock())
        rec.created_ts = rec.updated_ts = now
        payload = {"state": rec.state.value, "kind": rec.kind, "symbol": rec.symbol, "side": rec.side,
                   "volume": rec.volume, "decision_id": rec.decision_id, "created_ts": now, "updated_ts": now,
                   "meta": rec.meta}
        with self.registry.durable_tx():
            self.events.append(f"order.{rec.state.value.lower()}", rec.client_id, payload, rec.decision_id)
            self._write(rec)
        return rec

    def transition(self, rec: OrderRecord, state: OrderState, **changes: Any) -> OrderRecord:
        """Apply a legal state change. An illegal one raises: it would mean a logic bug."""
        oc.advance(rec.state, state)
        now = float(self.clock())
        rec = replace(rec, state=state, updated_ts=now, **changes)
        if state in oc.TERMINAL:
            rec.resolved_ts = now
        payload: dict[str, Any] = {"state": state.value, "updated_ts": now, **changes}
        if rec.resolved_ts == now:
            payload["resolved_ts"] = now
        with self.registry.durable_tx():
            self.events.append(f"order.{state.value.lower()}", rec.client_id, payload, rec.decision_id)
            self._write(rec)
        return rec

    def annotate(self, rec: OrderRecord, kind: str, **changes: Any) -> OrderRecord:
        """A fact about an order that does not change its state (protection results)."""
        now = float(self.clock())
        rec = replace(rec, updated_ts=now, **changes)
        with self.registry.tx():
            self.events.append(kind, rec.client_id, {"state": rec.state.value, "updated_ts": now, **changes},
                               rec.decision_id)
            self._write(rec)
        return rec


def project_orders(conn: Any) -> dict[str, dict[str, Any]]:
    """Rebuild the order table by folding the ledger. Raises if a stream breaks the state machine."""
    out: dict[str, dict[str, Any]] = {}
    rows = conn.execute("SELECT stream, kind, payload_json FROM ledger WHERE kind LIKE 'order.%' ORDER BY seq")
    for row in rows:
        payload = json.loads(row["payload_json"])
        if "state" not in payload:
            continue  # order.stops: a stop change on a position, not an order state
        current = out.get(row["stream"])
        state = OrderState(payload["state"])
        if current is None:
            current = out[row["stream"]] = {"state": state.value}
        elif not row["kind"].startswith("order.note"):
            oc.advance(OrderState(current["state"]), state)
        current.update(payload)
    return out


class ExecutionEngine:
    def __init__(self, broker: BrokerPort, journal: OrderJournal, registry: Any, magic: int,
                 settings: ExecutionSettings, clock: Callable[[], float], monotonic: Callable[[], float],
                 sleep: Callable[[float], None], notify: Callable[..., Any] = lambda *a, **k: None) -> None:
        self.broker = broker
        self.journal = journal
        self.registry = registry
        self.magic = magic
        self.settings = settings
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.notify = notify
        self.breaker = BrokerBreaker.from_json(registry.get_kv(BREAKER_KEY))
        self.duplicates_blocked = 0
        self.unprotected_events = 0
        self._absent_since: dict[str, float] = {}   # monotonic: a PC clock jump proves nothing

    # ---------------------------------------------------------------- breaker

    def _save_breaker(self) -> None:
        self.registry.set_kv(BREAKER_KEY, self.breaker.to_json())

    def entries_allowed(self) -> bool:
        before = (self.breaker.state, self.breaker.probes)
        allowed = self.breaker.allow(float(self.clock()), self.settings.breaker)
        if (self.breaker.state, self.breaker.probes) != before:
            self._save_breaker()
        return allowed

    def _infrastructure_failure(self, detail: str) -> None:
        if self.breaker.on_failure(float(self.clock()), self.settings.breaker):
            log.critical("Broker circuit breaker OPEN after %d failures: %s", self.breaker.failures, detail)
            self.notify(f"Disjuntor do corretor ABERTO: novas entradas paradas ({detail})", key="broker-breaker")
            self.journal.events.append("breaker.open", "broker", {"failures": self.breaker.failures, "detail": detail})
        self._save_breaker()

    def _infrastructure_ok(self) -> None:
        if self.breaker.state != "CLOSED":
            log.info("Broker circuit breaker closed again")
            self.journal.events.append("breaker.closed", "broker", {})
        if self.breaker.state != "CLOSED" or self.breaker.failures:
            self.breaker.on_success()
            self._save_breaker()

    # ---------------------------------------------------------- reconciliation

    def locate(self, rec: OrderRecord) -> Found:
        """Search the broker for this order: by client id, by order ticket, and (conservatively)
        any position of ours on the symbol that no journal row explains yet."""
        comment = oc.order_comment(rec.client_id)
        positions = self.broker.positions(rec.symbol)
        pending = self.broker.pending_orders()
        now = float(self.clock())
        deals = self.broker.deals_between(rec.created_ts - DEAL_LOOKBACK_SECONDS, now + DEAL_LOOKBACK_SECONDS)
        for p in positions or ():
            if getattr(p, "magic", None) == self.magic and (getattr(p, "comment", "") == comment
                                                            or (rec.ticket and p.ticket == rec.ticket)):
                return Found("position", rec.ticket or int(p.ticket), int(p.ticket), float(p.volume),
                             float(p.price_open))
        for d in deals or ():
            if getattr(d, "magic", None) == self.magic and getattr(d, "entry", None) == DEAL_ENTRY_IN \
                    and (getattr(d, "comment", "") == comment or (rec.ticket and getattr(d, "order", 0) == rec.ticket)):
                return Found("deal", int(getattr(d, "order", 0) or 0) or rec.ticket,
                             int(getattr(d, "position_id", 0) or 0) or None, float(d.volume), float(d.price),
                             int(d.ticket))
        for o in pending or ():
            if getattr(o, "magic", None) == self.magic and getattr(o, "comment", "") == comment:
                return Found("pending", int(o.ticket))
        if positions is None or pending is None or deals is None:
            return Found("unknown")
        known = self._known_tickets()
        for p in positions:
            if getattr(p, "magic", None) == self.magic and side_of_position(p.type) == rec.side \
                    and int(p.ticket) not in known:
                # Comment rewritten by the broker? Same symbol, side and magic, explained by nothing:
                # this is what the uncertain order most plausibly became. Adopt it, never add to it.
                return Found("position", int(p.ticket), int(p.ticket), float(p.volume), float(p.price_open))
        return Found("absent")

    def _known_tickets(self) -> set[int]:
        rows = self.registry.conn.execute(
            "SELECT ticket FROM trades WHERE mode = 'live' AND status = 'open' AND ticket IS NOT NULL")
        return {int(r["ticket"]) for r in rows}

    def _resolve_found(self, rec: OrderRecord, found: Found) -> OrderRecord:
        changes: dict[str, Any] = {"ticket": found.ticket, "position_ticket": found.position_ticket}
        if found.volume:
            changes["filled_volume"] = found.volume
        if found.price:
            changes["fill_price"] = found.price
        partial = found.volume and found.volume < rec.volume - 1e-9
        return self.journal.transition(rec, _S.PARTIALLY_FILLED if partial else _S.FILLED, **changes)

    def reconcile_startup(self) -> dict[str, int]:
        """After a restart: INTENT was never sent; SUBMITTED may have been, so it is uncertain."""
        counts = {"cancelled": 0, "uncertain": 0}
        for rec in self.journal.in_state(_S.INTENT):
            self.journal.transition(rec, _S.CANCELLED, detail="process stopped before sending")
            counts["cancelled"] += 1
        for rec in self.journal.in_state(_S.SUBMITTED):
            self.journal.transition(rec, _S.UNCERTAIN, detail="process stopped after submit; outcome unknown")
            counts["uncertain"] += 1
        return counts

    def reconcile(self, on_fill: Optional[Callable[[OrderRecord], None]] = None) -> int:
        """Resolve every unresolved order that the broker's records can settle. Returns the count."""
        resolved = 0
        for rec in self.journal.unresolved():
            if rec.kind == "open":
                done = self._reconcile_open(rec, on_fill)
            else:
                done = self._reconcile_close(rec)
            resolved += int(done)
        return resolved

    def _reconcile_open(self, rec: OrderRecord, on_fill: Optional[Callable[[OrderRecord], None]]) -> bool:
        found = self.locate(rec)
        if found.status in ("position", "deal"):
            rec = self._resolve_found(rec, found)
            log.warning("%s: order %s resolved by reconciliation: %s (ticket %s)", rec.symbol, rec.client_id,
                        rec.state.value, found.position_ticket)
            self.journal.events.append("order.note.reconciled", rec.client_id, {"state": rec.state.value,
                                                                               "found": found.status}, rec.decision_id)
            if on_fill is not None:
                on_fill(rec)
            return True
        if found.status == "absent":
            since = self._absent_since.setdefault(rec.client_id, float(self.monotonic()))
            age = float(self.monotonic()) - since
            if age >= self.settings.uncertain_grace_seconds:
                if rec.state == _S.SUBMITTED:
                    rec = self.journal.transition(rec, _S.UNCERTAIN, detail="no answer recorded")
                self.journal.transition(rec, _S.CANCELLED, detail="proved not executed: the broker has held nothing "
                                                                  f"with this id for {age:.0f}s")
                self._absent_since.pop(rec.client_id, None)
                return True
        else:
            self._absent_since.pop(rec.client_id, None)   # "unknown" restarts it: only answers prove absence
        return False

    def _reconcile_close(self, rec: OrderRecord) -> bool:
        positions = self.broker.positions(rec.symbol)
        if positions is None:
            return False
        ticket = rec.position_ticket
        current = next((p for p in positions if int(p.ticket) == ticket), None)
        expected_left = float(rec.meta.get("volume_before", rec.volume)) - rec.volume
        if current is None or float(current.volume) <= expected_left + 1e-9:
            self.journal.transition(rec, _S.FILLED, filled_volume=rec.volume, detail="confirmed by reconciliation")
            self._absent_since.pop(rec.client_id, None)
            return True
        since = self._absent_since.setdefault(rec.client_id, float(self.monotonic()))
        if float(self.monotonic()) - since >= self.settings.uncertain_grace_seconds:
            if rec.state == _S.SUBMITTED:
                rec = self.journal.transition(rec, _S.UNCERTAIN, detail="no answer recorded")
            self.journal.transition(rec, _S.CANCELLED, detail="position unchanged: close did not execute")
            self._absent_since.pop(rec.client_id, None)
            return True
        return False

    def _poll(self, rec: OrderRecord) -> Found:
        """Bounded wait for a late confirmation (MT5 can confirm late, especially around news)."""
        found = Found("unknown")
        for _ in range(max(1, self.settings.confirm_polls)):
            found = self.locate(rec)
            if found.status in ("position", "deal"):
                return found
            self.sleep(self.settings.confirm_poll_seconds)
        return found

    # ---------------------------------------------------------------- entries

    def blocked(self, symbol: str, cid: str) -> Optional[tuple[str, str]]:
        """(code, reason) when a new order must not be sent. Read-only except for the probe count."""
        existing = self.journal.get(cid)
        if existing is not None:
            self.duplicates_blocked += 1
            return "DUPLICATE_BLOCKED", f"decision already has order {cid} ({existing.state.value})"
        if self.journal.unresolved(symbol):
            return "ORDER_UNCERTAIN", "an earlier order on this symbol is unresolved; waiting for reconciliation"
        return None

    def open(self, cid: str, build: Callable[[Any], OrderRequest], tick: Any, fresh_tick: Callable[[], Any],
             validate: Callable[[OrderRequest], None], meta: Mapping[str, Any],
             decision_id: Optional[str] = None) -> ExecReport:
        """Send one entry. Never raises for broker trouble: the report says what is known."""
        request = build(tick)
        block = self.blocked_entry(request.symbol, cid)
        if block is not None:
            return ExecReport(cid, _S.CANCELLED, False, block[0], block[1])
        validate(request)
        rec = self.journal.create(OrderRecord(cid, "open", _S.INTENT, request.symbol, request.side, request.volume,
                                              decision_id, price=request.price, meta=dict(meta)))
        requotes = rejects = 0
        delay = self.settings.backoff_seconds
        started = self.monotonic()
        attempt = 0
        spread_submit = float(tick.ask) - float(tick.bid)
        while True:
            attempt += 1
            if attempt > 1:
                # Before ANY resubmission: prove the previous attempt left nothing at the broker.
                found = self.locate(rec)
                if found.status in ("position", "deal"):
                    rec = self._resolve_found(rec, found)
                    return self._filled(rec, request, started, attempt - 1, requotes, rejects, spread_submit)
                if found.status != "absent":
                    rec = self.journal.transition(rec, _S.UNCERTAIN, detail="could not verify before a retry")
                    return self._unresolved(rec, request, attempt - 1, requotes, rejects, spread_submit)
            rec = self.journal.transition(rec, _S.SUBMITTED, attempts=attempt, price=request.price,
                                          submitted_ts=rec.submitted_ts or float(self.clock()))
            reply = self.broker.send(request)
            outcome = oc.classify(reply.retcode, self.settings.retryable)
            if outcome in (Outcome.FILLED, Outcome.PARTIAL, Outcome.PLACED):
                self._infrastructure_ok()
                return self._after_success(rec, request, reply, outcome, started, attempt, requotes, rejects,
                                           spread_submit)
            if outcome == Outcome.UNCERTAIN:
                detail = f"{oc.retcode_name(reply.retcode)} {reply.error or reply.comment}".strip()
                log.error("%s: order %s outcome UNKNOWN (%s); reconciling, never resending", rec.symbol, cid, detail)
                self._infrastructure_failure(detail)
                rec = self.journal.transition(rec, _S.UNCERTAIN, retcode=reply.retcode, detail=detail)
                found = self._poll(rec)
                if found.status in ("position", "deal"):
                    rec = self._resolve_found(rec, found)
                    return self._filled(rec, request, started, attempt, requotes, rejects, spread_submit)
                return self._unresolved(rec, request, attempt, requotes, rejects, spread_submit)
            rejects += 1
            transient = outcome == Outcome.TRANSIENT
            if transient:
                requotes += 1
            if not transient or attempt >= self.settings.max_attempts:
                detail = f"{oc.retcode_name(reply.retcode)} {reply.comment}".strip()
                rec = self.journal.transition(rec, _S.REJECTED, retcode=reply.retcode, detail=detail)
                log.error("%s: order rejected: %s", rec.symbol, detail)
                return ExecReport(cid, rec.state, False, "ORDER_SEND_FAIL", detail, request=request, attempts=attempt,
                                  requotes=requotes, rejects=rejects, retcode=reply.retcode,
                                  spread_submit=spread_submit)
            log.warning("%s: %s on attempt %d; retrying with a fresh price", rec.symbol,
                        oc.retcode_name(reply.retcode), attempt)
            self.sleep(min(delay, self.settings.backoff_max_seconds))
            delay *= 2.0
            quote = fresh_tick()
            if quote is None:
                rec = self.journal.transition(rec, _S.REJECTED, retcode=reply.retcode, detail="no fresh tick for retry")
                return ExecReport(cid, rec.state, False, "ORDER_SEND_FAIL", "no fresh tick for retry", request=request,
                                  attempts=attempt, requotes=requotes, rejects=rejects, retcode=reply.retcode)
            spread_submit = float(quote.ask) - float(quote.bid)
            request = build(quote)
            try:
                validate(request)
            except ValueError as exc:
                rec = self.journal.transition(rec, _S.REJECTED, retcode=reply.retcode, detail=f"retry blocked: {exc}")
                return ExecReport(cid, rec.state, False, "ORDER_SEND_FAIL", str(exc), request=request,
                                  attempts=attempt, requotes=requotes, rejects=rejects)

    def blocked_entry(self, symbol: str, cid: str) -> Optional[tuple[str, str]]:
        block = self.blocked(symbol, cid)
        if block is not None:
            return block
        if not self.entries_allowed():
            return "BROKER_BREAKER_OPEN", "broker circuit breaker is open: no new entries during the outage"
        return None

    def _after_success(self, rec: OrderRecord, request: OrderRequest, reply: BrokerReply, outcome: Outcome,
                       started: float, attempt: int, requotes: int, rejects: int, spread: float) -> ExecReport:
        common: dict[str, Any] = {"ticket": reply.order or None, "retcode": reply.retcode}
        if outcome == Outcome.PLACED:
            # 10008: the order exists; its fill still has to be seen at the broker.
            rec = self.journal.transition(rec, _S.ACKED, **common)
            found = self._poll(rec)
            if found.status in ("position", "deal"):
                rec = self._resolve_found(rec, found)
                return self._filled(rec, request, started, attempt, requotes, rejects, spread)
            return self._unresolved(rec, request, attempt, requotes, rejects, spread)
        filled = reply.volume or request.volume
        state = _S.PARTIALLY_FILLED if outcome == Outcome.PARTIAL or filled < request.volume - 1e-9 else _S.FILLED
        rec = self.journal.transition(rec, state, filled_volume=filled, fill_price=reply.price or request.price,
                                      position_ticket=self._position_ticket(rec, reply), **common)
        return self._filled(rec, request, started, attempt, requotes, rejects, spread, reply.deal or None)

    def _position_ticket(self, rec: OrderRecord, reply: BrokerReply) -> Optional[int]:
        """The ticket of the POSITION, which only equals the order's on a hedging account."""
        comment = oc.order_comment(rec.client_id)
        positions = self.broker.positions(rec.symbol) or ()
        for p in positions:
            if getattr(p, "magic", None) == self.magic and (getattr(p, "comment", "") == comment
                                                            or int(p.ticket) == reply.order):
                return int(p.ticket)
        ours = [p for p in positions if getattr(p, "magic", None) == self.magic]
        if len(ours) == 1 and int(ours[0].ticket) not in self._known_tickets():
            return int(ours[0].ticket)
        return reply.order or None

    def _filled(self, rec: OrderRecord, request: OrderRequest, started: float, attempt: int, requotes: int,
                rejects: int, spread: float, deal: Optional[int] = None) -> ExecReport:
        latency = (self.monotonic() - started) * 1000.0
        reason = "partially filled" if rec.state == _S.PARTIALLY_FILLED else "opened"
        return ExecReport(rec.client_id, rec.state, True, "ORDER_OK", reason, rec.filled_volume or request.volume,
                          rec.fill_price or request.price, rec.ticket, rec.position_ticket, deal, request, attempt,
                          latency, requotes, rejects, spread, rec.retcode)

    @staticmethod
    def _unresolved(rec: OrderRecord, request: OrderRequest, attempt: int, requotes: int, rejects: int,
                    spread: float) -> ExecReport:
        return ExecReport(rec.client_id, rec.state, False, "ORDER_UNCERTAIN", "order outcome unknown; reconciling",
                          request=request, attempts=attempt, requotes=requotes, rejects=rejects, retcode=rec.retcode,
                          spread_submit=spread)

    # ------------------------------------------------------------ protection

    def _stops_match(self, want: float, have: float, point: float) -> bool:
        return bool(have) and abs(want - have) <= self.settings.stop_tolerance_points * point + 1e-12

    def verify_protection(self, rec: OrderRecord, position_ticket: int, sl: float, tp: float, point: float,
                          close: Callable[[Any, str], bool]) -> str:
        """Confirm the position carries its stops. Returns ok, attached, closed or failed."""
        for attempt in range(1, max(1, self.settings.protect_attempts) + 1):
            positions = self.broker.positions(rec.symbol)
            pos = next((p for p in positions or () if int(p.ticket) == int(position_ticket)), None)
            if pos is None:
                if positions is not None:
                    return "ok"   # already closed at the broker: SL/TP or a stop-out did the job
            elif self._stops_match(sl, float(pos.sl or 0.0), point) and self._stops_match(tp, float(pos.tp or 0.0),
                                                                                         point):
                if attempt == 1:
                    self.journal.annotate(rec, "order.note.protected", protected=True)
                    return "ok"
                self._alert_unprotected(rec, "stops attached after the fill")
                self.journal.annotate(rec, "order.note.protected", protected=True)
                return "attached"
            elif float(pos.sl or 0.0) and float(pos.tp or 0.0) and attempt == 1:
                # The broker moved the stops (price grid, slippage). They exist: never fight them.
                self.journal.annotate(rec, "order.note.protected", protected=True,
                                      detail=f"broker stops {pos.sl}/{pos.tp}")
                return "ok"
            elif pos is not None:
                self.modify_stops(pos, sl if not pos.sl else float(pos.sl), tp if not pos.tp else float(pos.tp),
                                  "post-fill protection")
            self.sleep(self.settings.protect_retry_seconds)
        positions = self.broker.positions(rec.symbol)
        pos = next((p for p in positions or () if int(p.ticket) == int(position_ticket)), None)
        if pos is not None and pos.sl and pos.tp:
            self._alert_unprotected(rec, "stops attached after the fill")
            self.journal.annotate(rec, "order.note.protected", protected=True)
            return "attached"
        self._alert_unprotected(rec, "stops could not be attached: closing the position")
        if pos is not None and close(pos, "unprotected after fill and SL/TP could not be attached"):
            self.journal.annotate(rec, "order.note.protection_failed", detail="closed: stops could not be attached")
            return "closed"
        self.journal.annotate(rec, "order.note.protection_failed", detail="unprotected and close failed; retrying")
        return "failed"

    def _alert_unprotected(self, rec: OrderRecord, detail: str) -> None:
        self.unprotected_events += 1
        log.critical("%s: position of order %s was UNPROTECTED: %s", rec.symbol, rec.client_id, detail)
        self.notify(f"{rec.symbol}: posição sem SL/TP: {detail}", key=f"unprotected-{rec.client_id}")

    def modify_stops(self, position: Any, sl: float, tp: float, reason: str) -> bool:
        """Attach or tighten stops. Idempotent, so an uncertain reply is settled by re-reading."""
        request = OrderRequest("sltp", symbol=position.symbol, sl=sl, tp=tp, magic=self.magic,
                               comment=reason[:24], position=int(position.ticket))
        reply = self.broker.send(request)
        outcome = oc.classify(reply.retcode, self.settings.retryable)
        self.journal.events.append("order.stops", str(position.ticket),
                                   {"sl": sl, "tp": tp, "reason": reason, "retcode": reply.retcode})
        if outcome in (Outcome.FILLED, Outcome.NO_CHANGE, Outcome.PLACED, Outcome.PARTIAL):
            return True
        if outcome == Outcome.UNCERTAIN:
            self._infrastructure_failure(f"stop change: {oc.retcode_name(reply.retcode)}")
            positions = self.broker.positions(position.symbol) or ()
            current = next((p for p in positions if int(p.ticket) == int(position.ticket)), None)
            return current is not None and math.isclose(float(current.sl or 0.0), sl, abs_tol=1e-12) \
                and math.isclose(float(current.tp or 0.0), tp, abs_tol=1e-12)
        log.error("%s: stop change on %s failed: %s %s", position.symbol, position.ticket,
                  oc.retcode_name(reply.retcode), reply.comment)
        return False

    # ----------------------------------------------------------------- closing

    def close(self, position: Any, volume: float, build: Callable[[Any], OrderRequest], tick: Any,
              fresh_tick: Callable[[], Any], reason: str) -> ExecReport:
        """Close (part of) a position. Always allowed. Re-reads the position before every attempt:
        on a netting account a repeated close would open a new position the other way."""
        ticket = int(position.ticket)
        for rec in self.journal.unresolved(position.symbol):
            if rec.kind == "close" and rec.position_ticket == ticket:
                self._reconcile_close(rec)
                current = self.journal.get(rec.client_id)
                if current is not None and current.state == _S.FILLED:
                    return ExecReport(rec.client_id, current.state, True, "CLOSE_OK", "closed (reconciled)")
                if current is not None and current.state != _S.CANCELLED:
                    return ExecReport(rec.client_id, current.state, False, "ORDER_UNCERTAIN",
                                      "an earlier close is unresolved")
        n = self.journal.count_for_position("close", ticket)
        cid = oc.client_id("close", self.magic, ticket, round(volume, 6), n)
        request = build(tick)
        rec = self.journal.create(OrderRecord(cid, "close", _S.INTENT, position.symbol, request.side, volume, None,
                                              price=request.price, position_ticket=ticket,
                                              meta={"reason": reason, "volume_before": float(position.volume)}))
        delay = self.settings.backoff_seconds
        attempt = 0
        requotes = 0
        started = self.monotonic()
        while True:
            attempt += 1
            live = self.broker.positions(position.symbol)
            if attempt > 1 and live is not None:
                current = next((p for p in live if int(p.ticket) == ticket), None)
                if current is None or float(current.volume) < float(position.volume) - 1e-9:
                    rec = self.journal.transition(rec, _S.FILLED, filled_volume=volume, detail="already closed")
                    return ExecReport(cid, rec.state, True, "CLOSE_OK", "already closed", volume, attempts=attempt)
            rec = self.journal.transition(rec, _S.SUBMITTED, attempts=attempt, price=request.price,
                                          submitted_ts=rec.submitted_ts or float(self.clock()))
            reply = self.broker.send(request)
            outcome = oc.classify(reply.retcode, self.settings.retryable)
            if outcome in (Outcome.FILLED, Outcome.PARTIAL, Outcome.PLACED, Outcome.GONE):
                self._infrastructure_ok()
                state = _S.PARTIALLY_FILLED if outcome == Outcome.PARTIAL else _S.FILLED
                if outcome == Outcome.PLACED:
                    rec = self.journal.transition(rec, _S.ACKED, ticket=reply.order or None, retcode=reply.retcode)
                    self._reconcile_close(rec)
                    current = self.journal.get(cid) or rec
                    return ExecReport(cid, current.state, current.state == _S.FILLED,
                                      "CLOSE_OK" if current.state == _S.FILLED else "ORDER_UNCERTAIN", reason,
                                      volume, reply.price, reply.order, ticket, reply.deal, request, attempt,
                                      requotes=requotes, retcode=reply.retcode)
                rec = self.journal.transition(rec, state, filled_volume=reply.volume or volume,
                                              fill_price=reply.price or request.price, ticket=reply.order or None,
                                              retcode=reply.retcode)
                return ExecReport(cid, rec.state, True, "CLOSE_OK", reason, rec.filled_volume, rec.fill_price or 0.0,
                                  reply.order, ticket, reply.deal, request, attempt,
                                  (self.monotonic() - started) * 1000.0, requotes, retcode=reply.retcode)
            if outcome == Outcome.UNCERTAIN:
                detail = f"{oc.retcode_name(reply.retcode)} {reply.error or reply.comment}".strip()
                self._infrastructure_failure(detail)
                rec = self.journal.transition(rec, _S.UNCERTAIN, retcode=reply.retcode, detail=detail)
                for _ in range(max(1, self.settings.confirm_polls)):
                    if self._reconcile_close(rec):
                        break
                    self.sleep(self.settings.confirm_poll_seconds)
                current = self.journal.get(cid) or rec
                done = current.state == _S.FILLED
                return ExecReport(cid, current.state, done, "CLOSE_OK" if done else "ORDER_UNCERTAIN", detail,
                                  attempts=attempt, request=request, retcode=reply.retcode)
            if outcome == Outcome.TRANSIENT:
                requotes += 1
            if outcome != Outcome.TRANSIENT or attempt >= self.settings.max_attempts:
                detail = f"{oc.retcode_name(reply.retcode)} {reply.comment}".strip()
                rec = self.journal.transition(rec, _S.REJECTED, retcode=reply.retcode, detail=detail)
                log.error("%s: close of %s failed: %s", position.symbol, ticket, detail)
                return ExecReport(cid, rec.state, False, "ORDER_SEND_FAIL", detail, request=request, attempts=attempt,
                                  requotes=requotes, retcode=reply.retcode)
            self.sleep(min(delay, self.settings.backoff_max_seconds))
            delay *= 2.0
            quote = fresh_tick()
            if quote is None:
                rec = self.journal.transition(rec, _S.REJECTED, detail="no fresh tick for retry")
                return ExecReport(cid, rec.state, False, "ORDER_SEND_FAIL", "no fresh tick", request=request)
            request = build(quote)

    def remove_pending(self, order: Any) -> bool:
        reply = self.broker.send(OrderRequest("remove", magic=self.magic, comment="cancel", order=int(order.ticket)))
        return oc.classify(reply.retcode, self.settings.retryable) in (Outcome.FILLED, Outcome.PLACED,
                                                                       Outcome.NO_CHANGE)

    def snapshot(self) -> dict[str, Any]:
        return {"breaker": self.breaker.state, "breaker_failures": self.breaker.failures,
                "unresolved": len(self.journal.unresolved()), "duplicates_blocked": self.duplicates_blocked,
                "unprotected_events": self.unprotected_events}
