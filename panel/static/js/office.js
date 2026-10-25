// The office scene (canvas). It only animates transitions between states that come from the
// bot: the /api/office/state snapshot and the live event stream (each event carries its
// "effects" on the characters, computed server-side by panel/office.py).

import { bubbleBelow, drawAgent, layoutBubble, paintBubble, roundRect } from "./avatars.js";
import { language, reasonText, t, tv } from "./i18n.js";
import { clear, cssVar, el, num, reducedMotion, signed, timeOnly } from "./util.js";

export const W = 1200;
export const H = 760;
const SPEED = 170;
const BUBBLE_SECONDS = 8;
const FLASH_SECONDS = 7;
const MAX_PARTICLES = 60;
const MAX_BUBBLES = 8;

export const ROOMS = {
  trading_floor: [16, 16, 444, 236], research_lab: [470, 16, 290, 236], risk_office: [770, 16, 250, 236],
  advisor_booth: [1030, 16, 154, 236], newsroom: [16, 268, 270, 236], boardroom: [296, 268, 424, 236],
  execution_desk: [730, 268, 250, 236], ops_room: [990, 268, 194, 236], analytics: [16, 520, 430, 224],
  break_room: [456, 520, 728, 224],
};
const CORRIDORS = [260, 512];

function centerOf(room) {
  const [x, y, w, h] = ROOMS[room];
  return [x + w / 2, y + h / 2];
}

export function createOffice(stage, { onAgent, onDossier }) {
  const canvas = el("canvas", { width: W, height: H, role: "img", "aria-label": t("office.title") });
  const card = el("div", { class: "meeting-card", hidden: true, "aria-live": "polite" });
  stage.append(canvas, card);
  const ctx = canvas.getContext("2d");
  const agents = new Map();
  const homes = new Map();
  const history = new Map();
  let screens = {};
  let roomNames = {};
  let advisor = { state: "DISABLED", lamp: "grey", reason: "" };
  let sessions = { sessions: [] };
  let meeting = null;
  let metrics = {};
  let particles = [];
  let cardDirty = false;
  let running = true;
  let raf = 0;
  let last = performance.now();
  let clock = 0;
  const reduced = reducedMotion();
  let colors = {};

  function readColors() {
    colors = {
      floor: cssVar("--floor"), floorAlt: cssVar("--floor-alt"), wall: cssVar("--wall"), desk: cssVar("--desk"),
      screen: cssVar("--screen"), ink: cssVar("--text-primary"), muted: cssVar("--text-muted"), surface: cssVar("--surface-1"),
      surface2: cssVar("--surface-2"), border: cssVar("--axis"), night: cssVar("--night"), s1: cssVar("--series-1"),
      s2: cssVar("--series-2"), good: cssVar("--good"), warning: cssVar("--warning"), critical: cssVar("--critical"),
    };
  }
  readColors();

  // ------------------------------------------------------------ seats

  function layoutHomes(list) {
    const byRoom = {};
    for (const a of list) (byRoom[a.home] ||= []).push(a.id);
    for (const [room, ids] of Object.entries(byRoom)) {
      const [x, y, w, h] = ROOMS[room];
      if (room === "boardroom") { homes.set(ids[0], [x + 70, y + h / 2 + 10]); continue; }
      const cols = Math.min(ids.length, room === "trading_floor" ? 3 : 2);
      const rows = Math.ceil(ids.length / cols);
      ids.forEach((id, i) => {
        const c = i % cols;
        const r = Math.floor(i / cols);
        homes.set(id, [x + ((c + 0.5) * w) / cols, y + 96 + r * ((h - 110) / Math.max(1, rows)) ]);
      });
    }
  }

  function meetingSeat(index, count) {
    const [cx, cy] = centerOf("boardroom");
    if (index === 0) return [cx - 165, cy + 18];
    const angle = Math.PI + ((index) / (count)) * Math.PI * 2;
    return [cx + Math.cos(angle) * 150, cy + 18 + Math.sin(angle) * 58];
  }

  function breakSeat(index) {
    const [x, y] = ROOMS.break_room;
    return [x + 120 + (index % 8) * 70, y + 140 - Math.floor(index / 8) * 46];
  }

  function targetFor(a) {
    if (a.room === "boardroom" && meeting && meeting.participants.includes(a.id) && a.id !== "cio") {
      const others = meeting.participants.filter((p) => p !== "cio" && agents.get(p) && agents.get(p).room === "boardroom");
      return meetingSeat(1 + Math.max(0, others.indexOf(a.id)), others.length + 1);
    }
    if (a.room === "boardroom" && a.id === "cio") return meetingSeat(0, 1);
    if (a.room === "break_room") {
      const resting = [...agents.values()].filter((b) => b.room === "break_room").map((b) => b.id);
      return breakSeat(Math.max(0, resting.indexOf(a.id)));
    }
    return homes.get(a.id) || centerOf(a.home);
  }

  function route(a) {
    const [tx, ty] = targetFor(a);
    if (Math.hypot(tx - a.tx, ty - a.ty) < 1 && a.path.length) return;
    a.tx = tx; a.ty = ty;
    if (reduced) { a.x = tx; a.y = ty; a.path = []; return; }
    const corridor = CORRIDORS.reduce((best, c) => (Math.abs(c - (a.y + ty) / 2) < Math.abs(best - (a.y + ty) / 2) ? c : best));
    const sameRoom = Math.abs(a.y - ty) < 90 && Math.abs(a.x - tx) < 200;
    a.path = sameRoom ? [[tx, ty]] : [[a.x, corridor], [tx, corridor], [tx, ty]];
  }

  // ------------------------------------------------------------ state in

  function ensure(info) {
    if (!agents.has(info.id)) {
      agents.set(info.id, { ...info, x: 0, y: 0, tx: -1, ty: -1, path: [], phase: Math.random() * 6, bubble: null,
        bubbleUntil: 0, flash: null, flashUntil: 0, ticketText: "" });
    }
    return agents.get(info.id);
  }

  function applySnapshot(state) {
    if (!state || !state.agents) return;
    if (!homes.size) layoutHomes(state.agents);
    if (state.rooms) roomNames = Object.fromEntries(state.rooms.map((r) => [r.id, language() === "en" ? r.en : r.pt]));
    const fresh = agents.size === 0;
    for (const info of state.agents) {
      const a = ensure(info);
      Object.assign(a, { name: info.name, role: info.role, department: info.department, home: info.home, visual: info.visual,
        room: info.room, benched: info.benched, task: info.task, state: info.state });
      if (info.bubble && (!a.bubble || a.bubble !== info.bubble) && info.bubble_ts && (state.now || Date.now() / 1000) - info.bubble_ts < 60) {
        a.bubble = info.bubble; a.bubbleUntil = clock + BUBBLE_SECONDS;
      }
      if (fresh) { const [hx, hy] = targetFor(a); a.x = hx; a.y = hy; }
    }
    if (state.meeting && (!meeting || meeting.id !== state.meeting.id)) startMeeting(state.meeting);
    if (state.meeting && meeting && meeting.id === state.meeting.id) Object.assign(meeting, state.meeting);
    screens = state.screens || screens;
    advisor = state.advisor || advisor;
    for (const a of agents.values()) route(a);
    cardDirty = true;
  }

  function startMeeting(m) {
    meeting = { id: m.id, symbol: m.symbol, side: m.side, strategy: m.strategy, mode: m.mode,
      participants: [...(m.participants || [])], votes: [...(m.votes || [])], advisor: m.advisor || null,
      risk: m.risk || null, result: m.result || null, endsAt: m.result ? clock + 12 : clock + 45 };
  }

  function endMeeting() {
    if (!meeting) return;
    const ids = meeting.participants;
    meeting = null;
    for (const id of ids) {
      const a = agents.get(id);
      if (a && a.room === "boardroom") { a.room = a.benched ? "break_room" : a.home; if (a.visual === "meeting") a.visual = "idle"; route(a); }
    }
    cardDirty = true;
  }

  function applyEffect(effect) {
    const a = agents.get(effect.agent);
    if (!a) return;
    if (effect.visual) {
      if (["veto", "ticket", "celebrating"].includes(effect.visual)) {
        a.flash = effect.visual; a.flashUntil = clock + FLASH_SECONDS;
        if (effect.visual === "celebrating") burst(a.x, a.y - 20);
      } else if (!(a.benched && effect.visual !== "resting")) {
        a.visual = effect.visual;
      }
    }
    if (effect.room) { a.room = effect.room; route(a); }
    if (effect.bubble) { a.bubble = effect.bubble; a.bubbleUntil = clock + BUBBLE_SECONDS; }
    if (effect.lamp) advisor = { ...advisor, lamp: effect.lamp, state: effect.advisor_state || advisor.state };
  }

  function applyEvent(event) {
    const p = event.payload || {};
    if (event.topic === "market.snapshot" && event.symbol) {
      const h = history.get(event.symbol) || [];
      h.push((Number(p.bid) + Number(p.ask)) / 2);
      if (h.length > 120) h.shift();
      history.set(event.symbol, h);
      screens.symbols = screens.symbols || {};
      screens.symbols[event.symbol] = { ...(screens.symbols[event.symbol] || {}), bid: p.bid, ask: p.ask,
        spread_points: p.spread_points };
    }
    if (event.topic === "market.regime" && event.symbol) {
      screens.symbols = screens.symbols || {};
      screens.symbols[event.symbol] = { ...(screens.symbols[event.symbol] || {}), regime: p.regime, probs: p.probs };
    }
    if (event.topic === "system.health") metrics = p.metrics || metrics;
    if (event.topic === "decision.started") {
      startMeeting({ id: event.correlation_id, symbol: event.symbol, side: p.side, strategy: p.strategy, mode: p.mode,
        participants: [...(p.participants || []), "cio"] });
    }
    if (meeting && event.correlation_id === meeting.id) {
      if (event.topic === "opinion" && p.opinion && ["long", "short", "flat"].includes(p.opinion.stance)) {
        meeting.votes = meeting.votes.filter((v) => v.agent !== event.agent).concat([{ agent: event.agent,
          stance: p.opinion.stance, confidence: p.opinion.confidence }]);
      }
      if (event.topic === "advisor.verdict") meeting.advisor = p;
      if (event.topic === "risk.decision") meeting.risk = p;
      if (event.topic === "decision.finished") { meeting.result = p; meeting.endsAt = clock + 12; }
    }
    if (event.topic === "order.executed") {
      const ex = agents.get("execution_trader");
      if (ex) ex.ticketText = `${p.side || ""} ${p.volume || ""}`.trim();
    }
    for (const effect of event.effects || []) applyEffect(effect);
    cardDirty = true;
  }

  // ------------------------------------------------------------ meeting card

  function renderCard() {
    if (!meeting) { card.hidden = true; return; }
    clear(card);
    card.hidden = false;
    const side = meeting.side === "BUY" ? 1 : -1;
    card.append(el("h4", { text: `${t("office.meeting")}: ${meeting.side || ""} ${meeting.symbol || ""} · ${meeting.strategy || ""} (${t(`mode.${meeting.mode}`)})` }));
    for (const v of meeting.votes.slice(0, 8)) {
      const a = agents.get(v.agent);
      const agrees = (v.stance === "long" ? 1 : v.stance === "short" ? -1 : 0) * side;
      const bar = el("span", { class: agrees < 0 ? "against" : "" });
      bar.style.width = `${Math.round((v.confidence || 0) * 100)}%`;
      card.append(el("div", { class: "vote-row" }, el("span", { text: a ? a.name : v.agent }),
        el("span", { class: "muted", text: t(`stance.${v.stance}`) }), el("span", { class: "vote-bar" }, bar)));
    }
    const adv = meeting.advisor;
    card.append(el("div", { class: "small" }, `Claude: ${adv ? (adv.available ? `${tv("verdict", adv.verdict)} x${num(adv.multiplier, 2)}` : t("office.advisor_away")) : t("office.advisor_away")}`));
    if (meeting.risk) card.append(el("div", { class: "small" }, `${t("office.risk")}: ${meeting.risk.allowed ? "OK" : `VETO (${meeting.risk.reason})`}`));
    if (meeting.result) {
      card.append(el("div", { class: "small" }, el("strong", { text: `${t("office.result")}: ${meeting.result.action === "enter"
        ? `${t("decisions.enter")} x${num(meeting.result.multiplier, 2)}` : t("decisions.no_trade")}` })));
    }
    const open = el("button", { class: "mini-btn", type: "button", text: t("office.open_dossier") });
    const id = meeting.id;
    open.addEventListener("click", () => onDossier(id));
    card.append(open);
  }

  // ------------------------------------------------------------ drawing

  function drawRoom(id, label) {
    const [x, y, w, h] = ROOMS[id];
    ctx.fillStyle = colors.floor;
    roundRect(ctx, x, y, w, h, 10); ctx.fill();
    ctx.fillStyle = colors.floorAlt;
    for (let i = x + 20; i < x + w - 10; i += 40) for (let j = y + 50; j < y + h - 10; j += 40) {
      if (((i + j) / 40) % 2 < 1) ctx.fillRect(i, j, 20, 20);
    }
    ctx.strokeStyle = colors.wall; ctx.lineWidth = 4;
    roundRect(ctx, x, y, w, h, 10); ctx.stroke();
    ctx.fillStyle = colors.wall; ctx.fillRect(x + 2, y + 2, w - 4, 38);
    ctx.fillStyle = colors.ink; ctx.font = "700 12px system-ui"; ctx.fillText(label, x + 12, y + 24);
  }

  function screenBox(id, lines, accent) {
    const [x, y, w] = ROOMS[id];
    const sw = Math.min(170, w - 24);
    const sx = x + w - sw - 10;
    ctx.fillStyle = colors.screen;
    roundRect(ctx, sx, y + 44, sw, 40, 5); ctx.fill();
    ctx.fillStyle = "#cfe3ff"; ctx.font = "10px system-ui";
    lines.slice(0, 2).forEach((line, i) => ctx.fillText(String(line).slice(0, 30), sx + 6, y + 58 + i * 13));
    if (accent) { ctx.fillStyle = accent; ctx.fillRect(sx, y + 82, sw, 2); }
    return [sx, y + 44, sw, 40];
  }

  function sparkOnScreen(box, values) {
    if (!values || values.length < 2) return;
    const [sx, sy, sw, sh] = box;
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    ctx.strokeStyle = "#7cb6ff"; ctx.lineWidth = 1.5; ctx.beginPath();
    values.forEach((v, i) => {
      const px = sx + sw - 70 + (i / (values.length - 1)) * 64;
      const py = sy + sh - 6 - ((v - lo) / ((hi - lo) || 1)) * (sh - 14);
      if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
    });
    ctx.stroke();
  }

  function drawFurniture() {
    for (const [id, [hx, hy]] of homes) {
      const a = agents.get(id);
      if (!a || a.home === "boardroom") continue;
      ctx.fillStyle = colors.desk; roundRect(ctx, hx - 28, hy + 4, 56, 16, 3); ctx.fill();
      ctx.fillStyle = colors.screen; roundRect(ctx, hx - 12, hy - 4, 24, 12, 2); ctx.fill();
      ctx.fillStyle = a.visual === "typing" || a.visual === "reading" ? "#4f8fdb" : "#253041";
      ctx.fillRect(hx - 10, hy - 2, 20, 8);
    }
    const [cx, cy] = centerOf("boardroom");
    ctx.fillStyle = colors.desk; ctx.beginPath(); ctx.ellipse(cx, cy + 22, 135, 44, 0, 0, Math.PI * 2); ctx.fill();
    const [bx, by, bw] = ROOMS.break_room;
    ctx.fillStyle = "#6b4f7a"; roundRect(ctx, bx + 90, by + 150, Math.min(560, bw - 140), 26, 10); ctx.fill();
    ctx.fillStyle = "#3f3f46"; roundRect(ctx, bx + bw - 60, by + 60, 34, 52, 4); ctx.fill();
    ctx.fillStyle = "#a16207"; ctx.fillRect(bx + bw - 52, by + 72, 18, 10);
    ctx.fillStyle = "#15803d"; ctx.beginPath(); ctx.arc(bx + 40, by + 90, 16, 0, Math.PI * 2); ctx.fill();
    const [ox, oy] = ROOMS.ops_room;
    for (let i = 0; i < 3; i += 1) {
      ctx.fillStyle = "#1f2937"; ctx.fillRect(ox + 14 + i * 26, oy + 150, 20, 70);
      for (let j = 0; j < 5; j += 1) {
        ctx.fillStyle = Math.floor(clock * 2 + i + j) % 3 ? "#22c55e" : "#065f46";
        ctx.fillRect(ox + 18 + i * 26, oy + 158 + j * 12, 4, 3);
      }
    }
    const [ax, ay, aw] = ROOMS.advisor_booth;
    const lamp = { green: colors.good, amber: colors.warning, grey: "#6b7280" }[advisor.lamp] || "#6b7280";
    ctx.fillStyle = lamp;
    ctx.beginPath(); ctx.arc(ax + aw / 2, ay + 66, 13, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = colors.ink; ctx.font = "600 10px system-ui"; ctx.textAlign = "center";
    ctx.fillText(t(`advisor.${advisor.state}`) || advisor.state, ax + aw / 2, ay + 96);
    ctx.textAlign = "left";
  }

  function drawScreens() {
    const symbols = (screens.symbols) || {};
    const names = Object.keys(symbols);
    const first = names[0];
    if (first) {
      const s = symbols[first];
      const box = screenBox("trading_floor", [`${first} ${num(s.bid, 5)}`, `${t("markets.regime")}: ${s.regime || "?"}`], colors.s1);
      sparkOnScreen(box, history.get(first));
    }
    const risk = screens.risk || {};
    screenBox("risk_office", [`Perda diária ${num(risk.daily_loss_used_pct_of_limit, 0)}% do limite`,
      `Risco aberto ${num(risk.open_risk_pct, 2)}% / ${num(risk.max_open_risk_pct, 1)}%`],
      (risk.daily_loss_used_pct_of_limit || 0) > 70 ? colors.warning : colors.good);
    const news = screens.next_news;
    screenBox("newsroom", [news ? `${news.currency} em ${news.minutes} min` : (reasonText(screens.calendar) || "sem calendário"),
      `Sessões: ${(sessions.sessions || []).join(", ") || "nenhuma"}`]);
    screenBox("analytics", [`P&L hoje ${signed(screens.pnl_today, 2)}`, `Capital ${num(screens.equity, 2)}`],
      (screens.pnl_today || 0) >= 0 ? colors.good : colors.critical);
    screenBox("ops_room", [`CPU ${num(metrics.cpu_pct, 0)}%  RAM ${num(metrics.rss_mb, 0)} MB`,
      `MT5 ${num(metrics.mt5_latency_ms, 1)} ms`]);
    screenBox("execution_desk", names.slice(0, 2).map((n) => `${n} spread ${num(symbols[n].spread_points, 1)}`));
    if (meeting) screenBox("boardroom", [`${meeting.side} ${meeting.symbol}`, meeting.result ? (meeting.result.action === "enter" ? t("decisions.enter") : t("decisions.no_trade")) : "em reunião"]);
  }

  function drawAmbient() {
    const open = (sessions.sessions || []).length;
    if (open === 0) {
      ctx.fillStyle = colors.night; ctx.fillRect(0, 0, W, H);
    } else if (open === 1 && (sessions.sessions || [])[0] === "tokyo") {
      ctx.fillStyle = colors.night; ctx.globalAlpha = 0.5; ctx.fillRect(0, 0, W, H); ctx.globalAlpha = 1;
    }
  }

  function burst(x, y) {
    if (reduced) return;
    const palette = [colors.s1, colors.s2, colors.good, colors.warning];
    for (let i = 0; i < 18 && particles.length < MAX_PARTICLES; i += 1) {
      particles.push({ x, y, vx: (Math.random() - 0.5) * 160, vy: -Math.random() * 160 - 40, life: 1.4,
        c: palette[i % palette.length] });
    }
  }

  function update(dt) {
    clock += dt;
    for (const a of agents.values()) {
      if (a.flash && clock > a.flashUntil) a.flash = null;
      if (a.bubble && clock > a.bubbleUntil + 1) a.bubble = null;
      a.walking = false;
      if (a.path.length) {
        const [px, py] = a.path[0];
        const d = Math.hypot(px - a.x, py - a.y);
        const stepLen = SPEED * dt;
        if (d <= stepLen) { a.x = px; a.y = py; a.path.shift(); } else { a.x += ((px - a.x) / d) * stepLen; a.y += ((py - a.y) / d) * stepLen; }
        a.walking = true;
      }
    }
    if (meeting && clock > meeting.endsAt) endMeeting();
    particles = particles.filter((p) => (p.life -= dt) > 0);
    for (const p of particles) { p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 260 * dt; }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = colors.surface; ctx.fillRect(0, 0, W, H);
    for (const id of Object.keys(ROOMS)) drawRoom(id, roomNames[id] || id);
    drawFurniture();
    drawScreens();
    const ordered = [...agents.values()].sort((a, b) => a.y - b.y);
    const opts = { ink: colors.ink, muted: colors.muted, tagBg: colors.surface2, reduced, awayText: t("office.advisor_away").toUpperCase() };
    for (const a of ordered) {
      drawAgent(ctx, a, a.x, a.y, clock, { ...opts, walking: a.walking, ticketText: a.ticketText });
      const home = homes.get(a.id);
      if (home && !a.walking && a.room === a.home && a.home !== "boardroom" && Math.hypot(a.x - home[0], a.y - home[1]) < 2) {
        ctx.fillStyle = colors.desk; roundRect(ctx, home[0] - 28, home[1] + 4, 56, 16, 3); ctx.fill();
      }
    }
    for (const p of particles) { ctx.fillStyle = p.c; ctx.globalAlpha = Math.min(1, p.life); ctx.fillRect(p.x, p.y, 4, 4); }
    ctx.globalAlpha = 1;
    drawAmbient();
    drawBubbles(ordered);
  }

  // Newest bubbles first; one that would overlap a newer one moves under its speaker or, if
  // that is taken too, waits (its text is in the event feed anyway).
  function drawBubbles(ordered) {
    const opts = { bubbleBg: colors.surface, border: colors.border, ink: colors.ink, width: W, height: H };
    const speaking = ordered.filter((a) => a.bubble).sort((a, b) => b.bubbleUntil - a.bubbleUntil).slice(0, MAX_BUBBLES);
    const placed = [];
    const free = (b) => !placed.some((p) => b.bx < p.bx + p.w + 4 && p.bx < b.bx + b.w + 4 && b.by < p.by + p.h + 4 && p.by < b.by + b.h + 4);
    for (const a of speaking) {
      const above = layoutBubble(ctx, a.x, a.y, a.bubble, opts);
      if (!above) continue;
      const b = [above, bubbleBelow(above, opts)].find(free);
      if (!b) continue;
      placed.push(b);
      paintBubble(ctx, b, Math.min(1, Math.max(0, a.bubbleUntil + 1 - clock)), opts);
    }
  }

  function frame(now) {
    if (!running) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    update(dt);
    draw();
    if (cardDirty) { cardDirty = false; renderCard(); } // DOM work at most once per frame
    raf = requestAnimationFrame(frame);
  }

  function setRunning(on) {
    if (on && !running) { running = true; last = performance.now(); raf = requestAnimationFrame(frame); }
    if (!on && running) { running = false; cancelAnimationFrame(raf); }
  }

  const onVisibility = () => setRunning(!document.hidden);
  document.addEventListener("visibilitychange", onVisibility);
  canvas.addEventListener("click", (ev) => {
    const rect = canvas.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * W;
    const y = ((ev.clientY - rect.top) / rect.height) * H;
    let best = null;
    let bestD = 30;
    for (const a of agents.values()) {
      const d = Math.hypot(a.x - x, a.y - 4 - y);
      if (d < bestD) { best = a; bestD = d; }
    }
    if (best) onAgent(best.id);
  });
  raf = requestAnimationFrame(frame);

  return {
    applySnapshot, applyEvent,
    setSessions(value) { sessions = value || sessions; },
    resetForReplay(list) {
      meeting = null; particles = []; cardDirty = true;
      for (const a of agents.values()) {
        a.visual = "idle"; a.flash = null; a.bubble = null; a.room = a.home; a.benched = false; route(a);
      }
      if (list) applySnapshot({ agents: list.map((i) => ({ ...i, visual: "idle", room: i.home, bubble: null })) });
    },
    refreshTheme: readColors,
    agentsList: () => [...agents.values()],
    destroy() { setRunning(false); document.removeEventListener("visibilitychange", onVisibility); },
    timeLabel: (ts) => timeOnly(ts),
  };
}
