"""The office simulation's state, derived ONLY from real data: the agents' persisted state
and the event stream. No animation is invented here; the browser only animates the
transitions between the states this module computes.

Visuals: idle, typing, reading, talking, meeting, ticket (handing a trade ticket), veto
(red stop sign), celebrating, resting (break room), error (warning light), away (the
advisor booth when the advisor is not ONLINE).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from mt5bot.team.roster import BY_ID, DEPARTMENTS, ROOMS, ROSTER

STATE_VISUAL = {
    "idle": "idle", "scanning": "reading", "analyzing": "typing", "debating": "talking", "deciding": "talking",
    "executing": "ticket", "monitoring": "reading", "reviewing": "reading", "resting": "resting", "error": "error",
}
MEETING_SECONDS = 45.0
FLASH_SECONDS = 25.0
BUBBLE_SECONDS = 90.0
LAMP = {"ONLINE": "green", "DEGRADED": "amber", "BUDGET_CAPPED": "amber", "OFFLINE_NO_CREDIT": "grey",
        "OFFLINE_AUTH": "grey", "NO_KEY": "grey", "DISABLED": "grey"}


def event_effects(event: Mapping[str, Any], advisor_state: str = "DISABLED") -> list[dict[str, Any]]:
    """What a single event does to the characters (used live, and by ``derive``)."""
    topic, agent, payload = event.get("topic"), event.get("agent"), event.get("payload") or {}
    out: list[dict[str, Any]] = []
    if topic == "agent.state" and agent in BY_ID:
        effect: dict[str, Any] = {"agent": agent, "visual": STATE_VISUAL.get(payload.get("state"), "idle"),
                                  "task": payload.get("task", "")}
        if payload.get("message"):
            effect["bubble"] = payload["message"]
        out.append(effect)
    elif topic == "agent.message" and agent in BY_ID:
        out.append({"agent": agent, "bubble": payload.get("text", ""), "thought": payload.get("kind") == "thought"})
    elif topic == "agent.error" and agent in BY_ID:
        out.append({"agent": agent, "visual": "error", "bubble": f"Erro: {payload.get('error', '')}"[:160]})
    elif topic == "agent.benched" and agent in BY_ID:
        out.append({"agent": agent, "visual": "resting", "room": "break_room",
                    "bubble": "Vou para o banco." if payload.get("kind") == "performance" else "A reiniciar..."})
    elif topic == "agent.restored" and agent in BY_ID:
        out.append({"agent": agent, "visual": "idle", "room": BY_ID[agent].room, "bubble": "De volta!"})
    elif topic == "decision.started":
        for participant in list(payload.get("participants") or []) + ["cio"]:
            if participant in BY_ID:
                out.append({"agent": participant, "visual": "meeting", "room": "boardroom"})
    elif topic == "decision.finished":
        verdict = (f"Entrada {payload.get('side', '')} x{float(payload.get('multiplier') or 0):.2f}"
                   if payload.get("action") == "enter" else "Sem trade")
        out.append({"agent": "cio", "visual": "talking", "bubble": verdict, "meeting_over": True})
    elif topic == "risk.decision" and payload.get("allowed") is False:
        out.append({"agent": "risk_manager", "visual": "veto", "bubble": f"Veto: {payload.get('reason', '')}"[:160]})
    elif topic == "order.executed":
        out.append({"agent": "execution_trader", "visual": "ticket",
                    "bubble": f"{payload.get('side', '')} {payload.get('volume', '')} lotes com SL/TP"})
        out.append({"agent": "cio", "visual": "ticket"})
    elif topic == "trade.closed" and payload.get("mode") == "live":
        win = bool(payload.get("win"))
        text = f"{'Ganho' if win else 'Perda'} {float(payload.get('r') or 0):+.2f}R"
        out.append({"agent": "performance_analyst", "visual": "celebrating" if win else "reading", "bubble": text})
        if win:
            out.append({"agent": "execution_trader", "visual": "celebrating"})
    elif topic == "guardian.alert":
        action = payload.get("action")
        out.append({"agent": "guardian", "visual": "idle" if action == "clear" else "veto",
                    "bubble": f"{'Kill switch' if action == 'kill' else 'Congelado' if action == 'freeze' else 'Ok'}: "
                              f"{payload.get('detail', '')}"[:160]})
    elif topic == "advisor.state":
        new = payload.get("new", advisor_state)
        out.append({"agent": "claude_strategist", "visual": "idle" if new == "ONLINE" else "away",
                    "lamp": LAMP.get(new, "grey"), "advisor_state": new})
    return out


def derive(agent_rows: Mapping[str, Mapping[str, Any]], events: Sequence[Mapping[str, Any]], now: float,
           advisor_state: str, advisor_reason: str = "") -> dict[str, Any]:
    """Office state at ``now`` from persisted agent rows plus the recent events (oldest first)."""
    started = next((e for e in reversed(events) if e.get("topic") == "decision.started"), None)
    meeting = None
    if started is not None:
        finished = next((e for e in reversed(events) if e.get("topic") == "decision.finished"
                         and e.get("correlation_id") == started.get("correlation_id")), None)
        live = now - float(started["ts"]) <= MEETING_SECONDS
        recently_done = finished is not None and now - float(finished["ts"]) <= FLASH_SECONDS
        if (finished is None and live) or recently_done:
            meeting = {"id": started.get("correlation_id"), "symbol": started.get("symbol"),
                       "side": started["payload"].get("side"), "strategy": started["payload"].get("strategy"),
                       "mode": started["payload"].get("mode"),
                       "participants": list(started["payload"].get("participants") or []) + ["cio"],
                       "started": started["ts"], "finished": finished["ts"] if finished else None,
                       "result": finished["payload"] if finished else None,
                       "votes": [{"agent": e.get("agent"), "stance": e["payload"].get("opinion", {}).get("stance"),
                                  "confidence": e["payload"].get("opinion", {}).get("confidence")}
                                 for e in events if e.get("topic") == "opinion"
                                 and e.get("correlation_id") == started.get("correlation_id")],
                       "advisor": next((e["payload"] for e in reversed(events) if e.get("topic") == "advisor.verdict"
                                        and e.get("correlation_id") == started.get("correlation_id")), None),
                       "risk": next((e["payload"] for e in reversed(events) if e.get("topic") == "risk.decision"
                                     and e.get("correlation_id") == started.get("correlation_id")), None)}
    agents = []
    for info in ROSTER:
        row = agent_rows.get(info.id) or {}
        visual = STATE_VISUAL.get(str(row.get("state") or "idle"), "idle")
        room = info.room
        bubble: Optional[str] = None
        bubble_ts = 0.0
        for e in events:
            if now - float(e["ts"]) > BUBBLE_SECONDS:
                continue
            for effect in event_effects(e, advisor_state):
                if effect.get("agent") != info.id:
                    continue
                if effect.get("bubble"):
                    bubble, bubble_ts = effect["bubble"], float(e["ts"])
                flash = effect.get("visual")
                if flash in ("veto", "ticket", "celebrating", "error") and now - float(e["ts"]) <= FLASH_SECONDS:
                    visual = flash
        if meeting is not None and info.id in meeting["participants"] and visual not in ("veto", "error"):
            visual, room = "meeting", "boardroom"
        if info.id == "claude_strategist" and advisor_state != "ONLINE":
            visual = "away"
        if row.get("benched"):
            visual, room = "resting", "break_room"
        elif row.get("state") == "error":
            visual = "error"
        agents.append({
            "id": info.id, "name": info.name, "role": info.role_pt, "role_en": info.role_en,
            "department": info.department, "home": info.room, "room": room, "icon": info.icon, "visual": visual,
            "state": row.get("state", "idle"), "task": row.get("task") or "", "bubble": bubble,
            "bubble_ts": bubble_ts or None, "benched": bool(row.get("benched")), "bench_reason": row.get("bench_reason"),
            "errors": int(row.get("errors") or 0), "can": info.can_decide, "cannot": info.cannot_decide,
        })
    return {"agents": agents, "meeting": meeting, "rooms": [{"id": k, "pt": v[0], "en": v[1]} for k, v in ROOMS.items()],
            "departments": [{"id": k, "pt": v[0], "en": v[1]} for k, v in DEPARTMENTS.items()],
            "advisor": {"state": advisor_state, "lamp": LAMP.get(advisor_state, "grey"), "reason": advisor_reason,
                        "away": advisor_state != "ONLINE"}}


def annotate(event: Mapping[str, Any], advisor_state: str) -> dict[str, Any]:
    """Event as sent on the stream, with its effects on the characters."""
    out = dict(event)
    out["effects"] = event_effects(event, advisor_state)
    return out


def agents_in(events: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(e.get("agent")) for e in events if e.get("agent")}
