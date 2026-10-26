// The office scene (Canvas 2D). It only animates transitions between states that come from
// the bot: the /api/office/state snapshot and the live event stream (each event carries its
// "effects" on the characters, computed server side by panel/office.py). Nothing here
// invents activity: a character walks because the bot moved it (a meeting, the bench).
//
// Layers: a cached background (floor, walls, furniture, room names) redrawn only when the
// theme changes; per frame: live screens, characters (depth sorted), effects, speech bubbles
// in screen space, the ambient light of the market sessions, the minimap and the perf HUD.

import { clampCamera, fitZoom, followStep, screenToWorld, visibleRect, worldToScreen, zoomAt } from "./camera.js";
import { CIO_SEAT, ROOMS, TILE, buildMap, roomCenter, worldSize } from "./map.js";
import { compress, findPath } from "./pathfind.js";
import { UNIT, UNITS_H, UNITS_W, frameFor, lookFor } from "./sprites.js";

const SPEED = 3.4 * TILE;          // world px per second
const BUBBLE_SECONDS = 8;
const FLASH_SECONDS = 7;
const MEETING_SECONDS = 45;
const MAX_PARTICLES = { off: 0, low: 40, high: 160 };
const MAX_BUBBLES = 7;
const BG_SCALE = 2;                // background backing resolution (crisp when zoomed in)
const SPRITE_W = UNITS_W * UNIT;
const SPRITE_H = UNITS_H * UNIT;
const BRAND = ["#00C9A7", "#1874CD", "#00CFCF", "#F47C20", "#FFE600", "#E63030"];

function makeCanvas(w, h) {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  return c;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function wrap(ctx, text, width, maxLines = 3) {
  const words = String(text).split(/\s+/);
  const lines = [];
  let line = "";
  for (const w of words) {
    const next = line ? `${line} ${w}` : w;
    if (ctx.measureText(next).width > width && line) {
      lines.push(line);
      line = w;
      if (lines.length === maxLines) break;
    } else line = next;
  }
  if (lines.length < maxLines && line) lines.push(line);
  if (lines.length === maxLines && words.join(" ").length > lines.join(" ").length) {
    lines[maxLines - 1] = `${lines[maxLines - 1].replace(/.{0,2}$/, "")}…`;
  }
  return lines;
}

/**
 * createOffice(stage, opts) -> controller.
 * opts: { t (i18n), onAgent(id), onDossier(id), onHover(agent|null), settings: { effects, reducedMotion, hud } }
 */
export function createOffice(stage, opts) {
  const t = opts.t;
  const world = worldSize();
  const canvas = makeCanvas(300, 200);
  canvas.className = "office-canvas";
  canvas.tabIndex = 0;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", t("office.canvas_label"));
  const mini = makeCanvas(192, 128);
  mini.className = "minimap";
  mini.setAttribute("aria-hidden", "true");
  const hud = document.createElement("div");
  hud.className = "perf-hud";
  hud.hidden = true;
  hud.setAttribute("aria-hidden", "true");
  const tip = document.createElement("div");
  tip.className = "office-tip";
  tip.hidden = true;
  const card = document.createElement("div");
  card.className = "meeting-card";
  card.hidden = true;
  card.setAttribute("aria-live", "polite");
  stage.append(canvas, mini, hud, tip, card);
  const ctx = canvas.getContext("2d");
  const mctx = mini.getContext("2d");

  let map = null;
  let background = null;
  let colors = {};
  let roomNames = {};
  const agents = new Map();
  let settings = { effects: "high", reducedMotion: false, hud: false, ...(opts.settings || {}) };
  let screens = {};
  let advisor = { state: "DISABLED", lamp: "grey" };
  let sessions = { sessions: [] };
  let meeting = null;
  let particles = [];
  let rings = [];
  let cam = { x: world.w / 2, y: world.h / 2, zoom: 1 };
  let vp = { w: 300, h: 200 };
  let dpr = 1;
  let followId = null;
  let selectedId = null;
  let hoverId = null;
  let clock = 0;
  let running = true;
  let raf = 0;
  let last = performance.now();
  let fitted = false;
  let cardDirty = false;
  const perf = { fps: 60, frameMs: 0, drawMs: 0, lastHud: 0 };
  const history = new Map();

  function readColors() {
    const css = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (css.getPropertyValue(name) || "").trim() || fallback;
    colors = {
      page: v("--page", "#080E1A"), floor: v("--floor", "#0e1626"), floorAlt: v("--floor-alt", "#111c30"),
      corridor: v("--corridor", "#0b1220"), wall: v("--wall", "#1A3A6E"), wallTop: v("--wall-top", "#243f6e"),
      desk: v("--desk", "#2b3a55"), screen: v("--screen", "#050A10"), ink: v("--text-primary", "#E8EDF7"),
      muted: v("--text-muted", "#7A899E"), accent: v("--accent", "#00C9A7"), accent2: v("--accent-2", "#1874CD"),
      good: v("--good", "#00C9A7"), warning: v("--warning", "#F47C20"), critical: v("--critical", "#E63030"),
      night: v("--night", "rgba(5,10,16,0.45)"), bubble: v("--surface-2", "#0f1a2b"), border: v("--border-strong", "#2b4a7a"),
      label: v("--room-label", "#7A899E"), font: v("--font-display", "Syne, sans-serif"), mono: v("--mono", "monospace"),
    };
  }

  // ------------------------------------------------------------ background

  function drawBackground() {
    background = makeCanvas(world.w * BG_SCALE, world.h * BG_SCALE);
    const b = background.getContext("2d");
    b.scale(BG_SCALE, BG_SCALE);
    b.fillStyle = colors.page;
    b.fillRect(0, 0, world.w, world.h);
    const { grid, cols, rows } = map;
    for (let y = 0; y < rows; y += 1) {
      for (let x = 0; x < cols; x += 1) {
        const v = grid[y * cols + x];
        const px = x * TILE;
        const py = y * TILE;
        if (v === 1) continue;
        const room = roomAtTile(x, y);
        b.fillStyle = room ? ((x + y) % 2 ? colors.floor : colors.floorAlt) : colors.corridor;
        b.fillRect(px, py, TILE, TILE);
        if (!room && v !== 3 && (x % 4 === 0) && (y === 14 || y === 29)) {
          b.fillStyle = colors.wall;
          b.globalAlpha = 0.35;
          b.fillRect(px + 6, py + TILE - 2, TILE - 12, 2); // corridor floor markings
          b.globalAlpha = 1;
        }
      }
    }
    // Walls: a darker body and a lit top edge.
    for (let y = 0; y < rows; y += 1) {
      for (let x = 0; x < cols; x += 1) {
        if (grid[y * cols + x] !== 1) continue;
        b.fillStyle = colors.wall;
        b.fillRect(x * TILE, y * TILE, TILE, TILE);
        if (y + 1 < rows && grid[(y + 1) * cols + x] !== 1) {
          b.fillStyle = colors.wallTop;
          b.fillRect(x * TILE, y * TILE + TILE - 5, TILE, 5);
        }
      }
    }
    // Doors: a teal threshold.
    for (const doors of Object.values(map.doors)) {
      for (const [x, y] of doors) {
        b.fillStyle = colors.accent;
        b.globalAlpha = 0.5;
        b.fillRect(x * TILE + 2, y * TILE + TILE / 2 - 2, TILE - 4, 4);
        b.globalAlpha = 1;
      }
    }
    for (const f of map.furniture) drawFurniture(b, f);
    for (const [id, [x0, y0, x1]] of Object.entries(ROOMS)) {
      b.font = `700 13px ${colors.font}`;
      b.fillStyle = colors.label;
      b.textBaseline = "middle";
      b.fillText((roomNames[id] || id).toUpperCase(), (x0 + 1) * TILE + 6, (y0 + 1) * TILE + 12);
      b.fillStyle = colors.accent;
      b.fillRect((x0 + 1) * TILE + 6, (y0 + 1) * TILE + 22, 22, 2);
      void x1;
    }
    // Chairs under every seat.
    for (const s of [...map.seats.values(), ...map.meetingSeats, ...map.breakSeats]) {
      const [cx, cy] = [(s.x + 0.5) * TILE, (s.y + 0.5) * TILE];
      b.fillStyle = colors.desk;
      roundRect(b, cx - 8, cy - 2, 16, 12, 3);
      b.fill();
    }
  }

  function drawFurniture(b, f) {
    const x = f.x * TILE;
    const y = f.y * TILE;
    const w = f.w * TILE;
    const h = f.h * TILE;
    switch (f.kind) {
      case "desk":
        b.fillStyle = colors.desk; roundRect(b, x + 2, y + 6, w - 4, h - 6, 3); b.fill();
        b.fillStyle = colors.screen; roundRect(b, x + w / 2 - 14, y - 6, 28, 16, 2); b.fill();
        b.fillStyle = colors.wallTop; b.fillRect(x + w / 2 - 2, y + 10, 4, 4);
        break;
      case "table":
        b.fillStyle = colors.desk; roundRect(b, x + 2, y + 2, w - 4, h - 4, 14); b.fill();
        b.strokeStyle = colors.accent; b.globalAlpha = 0.35; b.lineWidth = 2; roundRect(b, x + 8, y + 8, w - 16, h - 16, 10);
        b.stroke(); b.globalAlpha = 1;
        break;
      case "sofa":
        b.fillStyle = "#1874CD"; roundRect(b, x, y + 4, w, h - 4, 8); b.fill();
        b.fillStyle = "#0f4f91"; roundRect(b, x, y + 4, w, 8, 6); b.fill();
        break;
      case "coffee":
        b.fillStyle = colors.desk; b.fillRect(x, y + 4, w, h - 4);
        b.fillStyle = "#2b2f36"; b.fillRect(x + 6, y - 6, 18, 18);
        b.fillStyle = "#F47C20"; b.fillRect(x + 11, y + 2, 8, 4);
        break;
      case "table_small":
        b.fillStyle = colors.desk; b.beginPath(); b.arc(x + w / 2, y + h / 2, w / 2 - 3, 0, Math.PI * 2); b.fill();
        break;
      case "plant":
        b.fillStyle = "#5b4a36"; b.fillRect(x + 7, y + 12, 10, 10);
        b.fillStyle = "#00C9A7"; b.beginPath(); b.arc(x + 12, y + 9, 9, 0, Math.PI * 2); b.fill();
        b.fillStyle = "#0a8f78"; b.beginPath(); b.arc(x + 8, y + 12, 5, 0, Math.PI * 2); b.fill();
        break;
      case "rack":
        b.fillStyle = "#0b0f16"; b.fillRect(x + 2, y - 10, w - 4, h + 8);
        b.strokeStyle = colors.wallTop; b.strokeRect(x + 2.5, y - 9.5, w - 5, h + 7);
        break;
      case "tv_wall": case "screens": case "ticker_board": case "screen_wall":
        b.fillStyle = colors.screen; roundRect(b, x, y + 2, w, h - 2, 3); b.fill();
        b.strokeStyle = colors.wallTop; b.lineWidth = 1; roundRect(b, x + 0.5, y + 2.5, w - 1, h - 3, 3); b.stroke();
        break;
      case "whiteboard":
        b.fillStyle = "#E8EDF7"; b.fillRect(x, y + 4, w, h - 6);
        b.strokeStyle = "#1874CD"; b.lineWidth = 1.5; b.beginPath();
        b.moveTo(x + 10, y + 16); b.lineTo(x + 40, y + 10); b.lineTo(x + 70, y + 14); b.lineTo(x + 110, y + 8); b.stroke();
        break;
      case "console":
        b.fillStyle = colors.desk; roundRect(b, x, y + 4, w, h - 4, 6); b.fill();
        b.fillStyle = colors.screen; roundRect(b, x + 10, y - 8, w - 20, 16, 3); b.fill();
        break;
      case "vault":
        b.fillStyle = "#1f2a44"; roundRect(b, x, y, w, h, 6); b.fill();
        b.strokeStyle = "#FFE600"; b.lineWidth = 2; b.beginPath(); b.arc(x + w / 2, y + h / 2, 12, 0, Math.PI * 2); b.stroke();
        break;
      case "printer":
        b.fillStyle = "#2b2f36"; roundRect(b, x + 2, y + 2, w - 4, h - 2, 4); b.fill();
        b.fillStyle = "#E8EDF7"; b.fillRect(x + 12, y - 2, w - 24, 6);
        break;
      default:
        break;
    }
  }

  function roomAtTile(x, y) {
    for (const [id, [x0, y0, x1, y1]] of Object.entries(ROOMS)) if (x > x0 && x < x1 && y > y0 && y < y1) return id;
    return null;
  }

  // ------------------------------------------------------------ agents

  function ensure(info) {
    let a = agents.get(info.id);
    if (!a) {
      a = { id: info.id, x: 0, y: 0, path: [], dir: "down", pose: "stand", seated: false, seat: null, phase: Math.random() * 4,
        bubble: null, bubbleUntil: 0, flash: null, flashUntil: 0, visual: "idle", room: info.home, look: null };
      agents.set(info.id, a);
    }
    return a;
  }

  function seatFor(a) {
    if (a.room === "boardroom" && meeting && meeting.participants.includes(a.id) && a.id !== "cio") {
      const others = meeting.participants.filter((p) => p !== "cio" && agents.get(p) && agents.get(p).room === "boardroom");
      const s = map.meetingSeats[Math.max(0, others.indexOf(a.id)) % map.meetingSeats.length];
      return s;
    }
    if (a.id === "cio" || a.home === "boardroom") return { x: CIO_SEAT[0], y: CIO_SEAT[1], face: "right" };
    if (a.room === "break_room") {
      const resting = [...agents.values()].filter((b) => b.room === "break_room").map((b) => b.id);
      return map.breakSeats[Math.max(0, resting.indexOf(a.id)) % map.breakSeats.length];
    }
    return map.seats.get(a.id) || (() => { const [x, y] = roomCenter(a.home); return { x, y, face: "down" }; })();
  }

  function feetOf(s) { return [(s.x + 0.5) * TILE, (s.y + 0.5) * TILE + 7]; }

  function route(a, instant = false) {
    const seat = seatFor(a);
    if (a.seat && a.seat.x === seat.x && a.seat.y === seat.y && (a.path.length || a.atSeat)) return;
    a.seat = seat;
    a.atSeat = false;
    const [fx, fy] = feetOf(seat);
    if (instant || settings.reducedMotion) {
      a.x = fx; a.y = fy; a.path = []; a.atSeat = true; return;
    }
    const from = [Math.max(0, Math.min(map.cols - 1, Math.floor(a.x / TILE))),
      Math.max(0, Math.min(map.rows - 1, Math.floor((a.y - 7) / TILE)))];
    const path = findPath(map.grid, map.cols, map.rows, from, [seat.x, seat.y]);
    if (!path) { a.x = fx; a.y = fy; a.path = []; a.atSeat = true; return; } // stuck: never leave a ghost walking
    a.path = compress(path).slice(1).map(([x, y]) => [(x + 0.5) * TILE, (y + 0.5) * TILE + 7]);
    if (!a.path.length) { a.atSeat = true; }
  }

  function applySnapshot(state) {
    if (!state || !state.agents) return;
    if (state.rooms) roomNames = Object.fromEntries(state.rooms.map((r) => [r.id, opts.lang() === "en" ? r.en : r.pt]));
    if (!map) {
      map = buildMap(state.agents.map((a) => ({ id: a.id, room: a.home })));
      drawBackground();
    }
    const fresh = agents.size === 0;
    for (const info of state.agents) {
      const a = ensure(info);
      Object.assign(a, { name: info.name, role: info.role, department: info.department, home: info.home,
        visual: info.visual, room: info.room, benched: info.benched, task: info.task, state: info.state });
      if (!a.look) a.look = lookFor(info);
      if (info.bubble && a.bubble !== info.bubble && info.bubble_ts && (state.now || Date.now() / 1000) - info.bubble_ts < 60) {
        a.bubble = info.bubble; a.bubbleUntil = clock + BUBBLE_SECONDS;
      }
      if (fresh) { a.seat = null; route(a, true); }
    }
    if (state.meeting && (!meeting || meeting.id !== state.meeting.id)) startMeeting(state.meeting);
    if (state.meeting && meeting && meeting.id === state.meeting.id) Object.assign(meeting, state.meeting);
    screens = state.screens || screens;
    advisor = state.advisor || advisor;
    for (const a of agents.values()) route(a);
    cardDirty = true;
    updateLabel();
  }

  function startMeeting(m) {
    meeting = { id: m.id, symbol: m.symbol, side: m.side, strategy: m.strategy, mode: m.mode,
      participants: [...(m.participants || [])], votes: [...(m.votes || [])], advisor: m.advisor || null,
      risk: m.risk || null, result: m.result || null, endsAt: clock + (m.result ? 12 : MEETING_SECONDS) };
  }

  function endMeeting() {
    if (!meeting) return;
    const ids = meeting.participants;
    meeting = null;
    for (const id of ids) {
      const a = agents.get(id);
      if (a && a.room === "boardroom") {
        a.room = a.benched ? "break_room" : a.home;
        if (a.visual === "meeting") a.visual = "idle";
        route(a);
      }
    }
    cardDirty = true;
  }

  function applyEffect(effect) {
    const a = agents.get(effect.agent);
    if (!a) return;
    if (effect.visual) {
      if (["veto", "ticket", "celebrating"].includes(effect.visual)) {
        a.flash = effect.visual; a.flashUntil = clock + FLASH_SECONDS;
        if (effect.visual === "celebrating") burst(a.x, a.y - SPRITE_H, BRAND, 26);
        if (effect.visual === "veto") ring(a.x, a.y - SPRITE_H / 2, colors.critical);
        if (effect.visual === "ticket") ring(a.x, a.y - SPRITE_H / 2, "#FFE600");
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
      if (h.length > 90) h.shift();
      history.set(event.symbol, h);
      screens.symbols = screens.symbols || {};
      screens.symbols[event.symbol] = { ...(screens.symbols[event.symbol] || {}), bid: p.bid, ask: p.ask,
        spread_points: p.spread_points };
    }
    if (event.topic === "market.regime" && event.symbol) {
      screens.symbols = screens.symbols || {};
      screens.symbols[event.symbol] = { ...(screens.symbols[event.symbol] || {}), regime: p.regime };
    }
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
      const tb = map && map.furniture.find((f) => f.kind === "ticker_board");
      if (tb) burst((tb.x + tb.w / 2) * TILE, tb.y * TILE, [colors.good], 10);
    }
    for (const effect of event.effects || []) applyEffect(effect);
    cardDirty = true;
  }

  // ------------------------------------------------------------ effects

  function effectsOn() { return settings.effects !== "off" && !settings.reducedMotion; }

  function burst(x, y, palette, n) {
    if (!effectsOn()) return;
    const cap = MAX_PARTICLES[settings.effects] || 0;
    const count = settings.effects === "low" ? Math.ceil(n / 3) : n;
    for (let i = 0; i < count && particles.length < cap; i += 1) {
      particles.push({ x, y, vx: (Math.random() - 0.5) * 150, vy: -Math.random() * 150 - 40, life: 1.3 + Math.random() * 0.5,
        c: palette[i % palette.length], s: 2 + Math.random() * 2 });
    }
  }

  function ring(x, y, color) {
    if (!effectsOn()) return;
    rings.push({ x, y, color, life: 1.2 });
  }

  // ------------------------------------------------------------ update

  function poseFor(a) {
    const visual = a.flash && clock < a.flashUntil ? a.flash : a.visual;
    if (!a.atSeat) return ["walk", false];
    const seated = a.seat && a.seat.face !== undefined && !["veto", "celebrating", "error"].includes(visual);
    switch (visual) {
      case "typing": return ["type", true];
      case "reading": return ["read", seated];
      case "talking": return ["talk", seated];
      case "meeting": return [a.bubble ? "talk" : "sit", true];
      case "ticket": return ["ticket", false];
      case "veto": return ["veto", false];
      case "celebrating": return ["celebrate", false];
      case "resting": return ["rest", true];
      case "error": return ["error", false];
      default: return ["sit", seated];
    }
  }

  function update(dt) {
    clock += dt;
    for (const a of agents.values()) {
      if (a.flash && clock > a.flashUntil) a.flash = null;
      if (a.bubble && clock > a.bubbleUntil + 1) a.bubble = null;
      if (a.path.length) {
        const [px, py] = a.path[0];
        const dx = px - a.x;
        const dy = py - a.y;
        const d = Math.hypot(dx, dy);
        const stepLen = SPEED * dt;
        if (Math.abs(dx) > Math.abs(dy)) a.dir = dx > 0 ? "right" : "left"; else if (d > 0.5) a.dir = dy > 0 ? "down" : "up";
        if (d <= stepLen) { a.x = px; a.y = py; a.path.shift(); } else { a.x += (dx / d) * stepLen; a.y += (dy / d) * stepLen; }
        a.phase += dt * 8;
        if (!a.path.length) a.atSeat = true;
      } else if (a.atSeat && a.seat) {
        a.dir = a.seat.face || "down";
      }
      [a.pose, a.seated] = poseFor(a);
    }
    if (meeting && clock > meeting.endsAt) endMeeting();
    particles = particles.filter((p) => (p.life -= dt) > 0);
    for (const p of particles) { p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 240 * dt; }
    rings = rings.filter((r) => (r.life -= dt) > 0);
    if (followId && agents.has(followId)) {
      const a = agents.get(followId);
      cam = clampCamera(followStep(cam, a.x, a.y - SPRITE_H / 2, dt, settings.reducedMotion ? 60 : 5), vp, world);
    }
  }

  // ------------------------------------------------------------ drawing

  function screenRect(fx, fy, w, h) { // a world rect anchored on the floor, in device pixels
    const [sx, sy] = worldToScreen(cam, vp, fx, fy);
    return [sx * dpr, sy * dpr, w * cam.zoom * dpr, h * cam.zoom * dpr];
  }

  function textOnScreen(lines, color, wx, wy, size = 9, mono = true) {
    const [sx, sy] = worldToScreen(cam, vp, wx, wy);
    ctx.font = `${mono ? "400" : "700"} ${Math.max(7, size * cam.zoom) * dpr}px ${mono ? colors.mono : colors.font}`;
    ctx.fillStyle = color;
    ctx.textBaseline = "top";
    lines.forEach((line, i) => ctx.fillText(String(line), sx * dpr, (sy + i * (size + 3) * cam.zoom) * dpr));
  }

  function drawScreens() {
    if (!map || cam.zoom < 0.55) return;
    const byKind = (kind) => map.furniture.filter((f) => f.kind === kind);
    const symbols = screens.symbols || {};
    const names = Object.keys(symbols);
    for (const f of byKind("ticker_board")) {
      const text = names.map((n) => `${n} ${symbols[n].bid !== undefined ? Number(symbols[n].bid).toFixed(5) : "--"}`).join("   ");
      const offset = settings.reducedMotion ? 0 : (clock * 30) % (f.w * TILE);
      ctx.save();
      const [rx, ry, rw, rh] = screenRect(f.x * TILE, f.y * TILE + 2, f.w * TILE, f.h * TILE - 2);
      ctx.beginPath(); ctx.rect(rx, ry, rw, rh); ctx.clip();
      textOnScreen([text || t("office.waiting_quotes")], colors.accent, f.x * TILE + 6 - offset, f.y * TILE + 7, 9);
      if (text) textOnScreen([text], colors.accent, f.x * TILE + 6 - offset + f.w * TILE, f.y * TILE + 7, 9);
      ctx.restore();
    }
    for (const f of byKind("tv_wall")) {
      const news = screens.next_news;
      textOnScreen([news ? `${news.currency} ${t("office.news_in")} ${news.minutes} min` : t("office.no_news")], f,
        colors.warning, f.x * TILE + 6, f.y * TILE + 7, 9);
    }
    for (const f of byKind("screens")) {
      const pnl = screens.pnl_today;
      const color = pnl === undefined || pnl === null ? colors.muted : pnl >= 0 ? colors.good : colors.critical;
      textOnScreen([`${t("office.pnl_today")} ${pnl === undefined || pnl === null ? "--" : (pnl >= 0 ? "+" : "") + Number(pnl).toFixed(2)}`
        + `   ${t("office.equity")} ${screens.equity ? Number(screens.equity).toFixed(2) : "--"}`], color,
      f.x * TILE + 6, f.y * TILE + 7, 9);
    }
    for (const f of byKind("screen_wall")) {
      const line = meeting ? `${meeting.side || ""} ${meeting.symbol || ""}` : t("office.no_meeting");
      textOnScreen([line], meeting ? colors.accent : colors.muted, f.x * TILE + 6, f.y * TILE + 7, 9);
    }
    for (const f of byKind("vault")) {
      const risk = screens.risk || {};
      if (risk.open_risk_pct !== undefined) {
        textOnScreen([`${Number(risk.open_risk_pct).toFixed(2)}%`], "#FFE600", f.x * TILE + 8, (f.y + f.h) * TILE + 4, 8);
      }
    }
    // Sparkline of the first symbol on the trading floor's wall.
    const first = names[0];
    const values = history.get(first);
    if (values && values.length > 2) {
      const lo = Math.min(...values);
      const hi = Math.max(...values);
      const [x0, y0] = [ROOMS.trading_floor[0] * TILE + 360, ROOMS.trading_floor[1] * TILE + 34];
      ctx.strokeStyle = colors.accent;
      ctx.lineWidth = Math.max(1, 1.5 * cam.zoom) * dpr;
      ctx.beginPath();
      values.forEach((v, i) => {
        const [sx, sy] = worldToScreen(cam, vp, x0 + (i / (values.length - 1)) * 120, y0 + 18 - ((v - lo) / ((hi - lo) || 1)) * 18);
        if (i) ctx.lineTo(sx * dpr, sy * dpr); else ctx.moveTo(sx * dpr, sy * dpr);
      });
      ctx.stroke();
    }
  }

  function drawLiveFixtures() {
    if (!map) return;
    // Monitors glow while their owner works at the desk.
    for (const [id, s] of map.seats) {
      const a = agents.get(id);
      if (!s.desk || !a || !a.atSeat || a.room !== a.home) continue;
      const on = ["typing", "reading", "talking"].includes(a.visual);
      const [rx, ry, rw, rh] = screenRect((s.desk[0] + 1.5) * TILE - 12, s.desk[1] * TILE - 4, 24, 12);
      ctx.fillStyle = on ? colors.accent2 : "#0b1220";
      ctx.globalAlpha = on ? 0.85 + (settings.reducedMotion ? 0 : 0.15 * Math.sin(clock * 6 + s.x)) : 1;
      ctx.fillRect(rx, ry, rw, rh);
      ctx.globalAlpha = 1;
    }
    // Server racks blink; the advisor lamp shows the advisor state.
    for (const f of map.furniture.filter((x) => x.kind === "rack")) {
      for (let j = 0; j < 4; j += 1) {
        const lit = settings.reducedMotion ? j % 2 === 0 : Math.floor(clock * 3 + f.x + j) % 3 !== 0;
        const [rx, ry, rw, rh] = screenRect(f.x * TILE + 8, f.y * TILE - 6 + j * 10, 6, 3);
        ctx.fillStyle = lit ? colors.good : "#0a3a33";
        ctx.fillRect(rx, ry, rw, rh);
      }
    }
    const lamp = map.furniture.find((f) => f.kind === "lamp");
    if (lamp) {
      const c = { green: colors.good, amber: colors.warning, grey: "#5b6474" }[advisor.lamp] || "#5b6474";
      const [sx, sy] = worldToScreen(cam, vp, (lamp.x + 0.5) * TILE, (lamp.y + 0.5) * TILE);
      const pulse = advisor.lamp === "green" && !settings.reducedMotion ? 2 * Math.sin(clock * 3) : 0;
      ctx.fillStyle = c;
      ctx.globalAlpha = 0.25;
      ctx.beginPath(); ctx.arc(sx * dpr, sy * dpr, (16 + pulse) * cam.zoom * dpr, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      ctx.beginPath(); ctx.arc(sx * dpr, sy * dpr, 8 * cam.zoom * dpr, 0, Math.PI * 2); ctx.fill();
    }
  }

  function drawAgent(a) {
    const frameNo = a.pose === "walk" ? Math.floor(a.phase) : Math.floor(clock * (a.pose === "type" ? 6 : 3) + a.phase);
    const img = frameFor(a.look, a.dir, a.pose, settings.reducedMotion ? 0 : frameNo, a.seated);
    const [rx, ry, rw, rh] = screenRect(a.x - SPRITE_W / 2, a.y - SPRITE_H, SPRITE_W, SPRITE_H);
    // Shadow.
    ctx.fillStyle = "rgba(0,0,0,0.35)";
    ctx.beginPath();
    ctx.ellipse(rx + rw / 2, ry + rh - 1 * cam.zoom * dpr, rw * 0.32, rh * 0.06, 0, 0, Math.PI * 2);
    ctx.fill();
    const away = a.visual === "away";
    if (away) ctx.globalAlpha = 0.35;
    ctx.drawImage(img, rx, ry, rw, rh);
    ctx.globalAlpha = 1;
    const selected = a.id === selectedId || a.id === followId;
    if (selected || a.id === hoverId) {
      ctx.strokeStyle = a.id === followId ? colors.accent : colors.ink;
      ctx.lineWidth = 2 * dpr;
      ctx.beginPath();
      ctx.ellipse(rx + rw / 2, ry + rh - 2 * cam.zoom * dpr, rw * 0.5, rh * 0.11, 0, 0, Math.PI * 2);
      ctx.stroke();
    }
    // Status icons above the head.
    const headX = rx + rw / 2;
    const headY = ry - 4 * dpr;
    const visual = a.flash && clock < a.flashUntil ? a.flash : a.visual;
    if (visual === "veto") stopSign(headX, headY - 6 * cam.zoom * dpr);
    else if (visual === "error") bang(headX, headY - 4 * cam.zoom * dpr, colors.critical);
    else if (visual === "ticket") ticket(headX, headY - 4 * cam.zoom * dpr);
    else if (a.benched) zz(headX, headY);
    // Name tag (when zoomed in enough, or for the selected/hovered agent).
    if (cam.zoom >= 0.85 || selected || a.id === hoverId) {
      const label = a.name || a.id;
      ctx.font = `700 ${Math.max(9, 10 * Math.min(cam.zoom, 1.4)) * dpr}px ${colors.font}`;
      const w = ctx.measureText(label).width + 10 * dpr;
      const h = 14 * dpr * Math.min(Math.max(cam.zoom, 0.9), 1.4);
      const tx = headX - w / 2;
      const ty = ry + rh + 2 * dpr;
      ctx.fillStyle = "rgba(5,10,16,0.78)";
      roundRect(ctx, tx, ty, w, h, 4 * dpr);
      ctx.fill();
      ctx.fillStyle = a.look.shirt;
      ctx.fillRect(tx, ty, 3 * dpr, h);
      ctx.fillStyle = "#E8EDF7";
      ctx.textBaseline = "middle";
      ctx.fillText(label, tx + 6 * dpr, ty + h / 2 + 0.5);
    }
  }

  function stopSign(x, y) {
    const r = 9 * Math.max(cam.zoom, 0.8) * dpr;
    ctx.fillStyle = colors.critical;
    ctx.beginPath();
    for (let i = 0; i < 8; i += 1) {
      const ang = Math.PI / 8 + (i * Math.PI) / 4;
      ctx.lineTo(x + r * Math.cos(ang), y - r + r * Math.sin(ang));
    }
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.fillRect(x - r * 0.55, y - r - r * 0.15, r * 1.1, r * 0.3);
  }

  function bang(x, y, color) {
    const s = Math.max(cam.zoom, 0.8) * dpr;
    const blink = settings.reducedMotion || Math.floor(clock * 3) % 2 === 0;
    if (!blink) return;
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(x, y - 8 * s, 8 * s, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.fillRect(x - 1.5 * s, y - 13 * s, 3 * s, 7 * s);
    ctx.fillRect(x - 1.5 * s, y - 4.5 * s, 3 * s, 2.5 * s);
  }

  function ticket(x, y) {
    const s = Math.max(cam.zoom, 0.8) * dpr;
    ctx.fillStyle = "#FFE600";
    ctx.fillRect(x - 8 * s, y - 14 * s, 16 * s, 10 * s);
    ctx.fillStyle = "#050A10";
    ctx.fillRect(x - 5 * s, y - 11 * s, 10 * s, 1.5 * s);
    ctx.fillRect(x - 5 * s, y - 8 * s, 7 * s, 1.5 * s);
  }

  function zz(x, y) {
    ctx.fillStyle = colors.muted;
    ctx.font = `700 ${10 * Math.max(cam.zoom, 0.8) * dpr}px ${colors.font}`;
    const dy = settings.reducedMotion ? 0 : Math.sin(clock * 2) * 2 * dpr;
    ctx.fillText("z z", x + 6 * dpr, y - 6 * dpr + dy);
  }

  function drawBubbles(ordered) {
    const speaking = ordered.filter((a) => a.bubble).sort((a, b) => b.bubbleUntil - a.bubbleUntil).slice(0, MAX_BUBBLES);
    const placed = [];
    ctx.font = `400 ${11 * dpr}px ${colors.mono}`;
    const free = (b) => !placed.some((p) => b.x < p.x + p.w + 4 * dpr && p.x < b.x + b.w + 4 * dpr && b.y < p.y + p.h + 4 * dpr
      && p.y < b.y + b.h + 4 * dpr);
    for (const a of speaking) {
      const [sx, sy] = worldToScreen(cam, vp, a.x, a.y - SPRITE_H - 6);
      const lines = wrap(ctx, a.bubble, 190 * dpr, 3);
      const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16 * dpr;
      const h = lines.length * 14 * dpr + 10 * dpr;
      let b = { x: Math.min(Math.max(4 * dpr, sx * dpr - w / 2), canvas.width - w - 4 * dpr), y: sy * dpr - h - 8 * dpr, w, h };
      if (!free(b)) b = { ...b, y: b.y - h - 6 * dpr };
      if (!free(b) || b.y < 0) continue;
      placed.push(b);
      const alpha = Math.min(1, Math.max(0, a.bubbleUntil + 1 - clock));
      ctx.globalAlpha = alpha;
      ctx.fillStyle = colors.bubble;
      ctx.strokeStyle = a.look.shirt;
      ctx.lineWidth = 1.5 * dpr;
      roundRect(ctx, b.x, b.y, b.w, b.h, 6 * dpr);
      ctx.fill();
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(sx * dpr - 5 * dpr, b.y + b.h);
      ctx.lineTo(sx * dpr, b.y + b.h + 7 * dpr);
      ctx.lineTo(sx * dpr + 5 * dpr, b.y + b.h);
      ctx.fill();
      ctx.fillStyle = colors.ink;
      ctx.textBaseline = "top";
      lines.forEach((l, i) => ctx.fillText(l, b.x + 8 * dpr, b.y + 5 * dpr + i * 14 * dpr));
      ctx.globalAlpha = 1;
    }
  }

  function drawAmbient() {
    const open = (sessions.sessions || []).length;
    if (open > 1) return;
    ctx.fillStyle = colors.night;
    ctx.globalAlpha = open === 0 ? 1 : 0.5;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.globalAlpha = 1;
  }

  function draw() {
    const started = performance.now();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = colors.page;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!map) return;
    ctx.imageSmoothingEnabled = cam.zoom < 1;
    const [vx, vy, vw, vh] = visibleRect(cam, vp);
    const sx0 = Math.max(0, vx);
    const sy0 = Math.max(0, vy);
    const sx1 = Math.min(world.w, vx + vw);
    const sy1 = Math.min(world.h, vy + vh);
    if (sx1 > sx0 && sy1 > sy0) {
      const [dx, dy] = worldToScreen(cam, vp, sx0, sy0);
      ctx.drawImage(background, sx0 * BG_SCALE, sy0 * BG_SCALE, (sx1 - sx0) * BG_SCALE, (sy1 - sy0) * BG_SCALE,
        dx * dpr, dy * dpr, (sx1 - sx0) * cam.zoom * dpr, (sy1 - sy0) * cam.zoom * dpr);
    }
    drawLiveFixtures();
    drawScreens();
    ctx.imageSmoothingEnabled = false;
    const ordered = [...agents.values()].filter((a) => a.look).sort((a, b) => a.y - b.y);
    for (const a of ordered) {
      if (a.x < vx - SPRITE_W || a.x > vx + vw + SPRITE_W || a.y < vy - 4 || a.y > vy + vh + SPRITE_H) continue;
      drawAgent(a);
    }
    for (const r of rings) {
      const [sx, sy] = worldToScreen(cam, vp, r.x, r.y);
      ctx.strokeStyle = r.color;
      ctx.globalAlpha = Math.min(1, r.life);
      ctx.lineWidth = 3 * dpr;
      ctx.beginPath();
      ctx.arc(sx * dpr, sy * dpr, (1.3 - r.life) * 60 * cam.zoom * dpr + 8 * dpr, 0, Math.PI * 2);
      ctx.stroke();
    }
    for (const p of particles) {
      const [sx, sy] = worldToScreen(cam, vp, p.x, p.y);
      ctx.fillStyle = p.c;
      ctx.globalAlpha = Math.min(1, p.life);
      ctx.fillRect(sx * dpr, sy * dpr, p.s * cam.zoom * dpr, p.s * cam.zoom * dpr);
    }
    ctx.globalAlpha = 1;
    drawAmbient();
    drawBubbles(ordered);
    perf.drawMs = perf.drawMs * 0.9 + (performance.now() - started) * 0.1;
  }

  function drawMinimap() {
    if (!map) return;
    const sx = mini.width / world.w;
    const sy = mini.height / world.h;
    mctx.drawImage(background, 0, 0, background.width, background.height, 0, 0, mini.width, mini.height);
    for (const a of agents.values()) {
      if (!a.look) continue;
      mctx.fillStyle = a.id === followId ? "#FFE600" : a.look.shirt;
      mctx.fillRect(a.x * sx - 2, (a.y - 10) * sy - 2, 4, 4);
    }
    const [vx, vy, vw, vh] = visibleRect(cam, vp);
    mctx.strokeStyle = "#E8EDF7";
    mctx.lineWidth = 1.5;
    mctx.strokeRect(vx * sx, vy * sy, vw * sx, vh * sy);
  }

  function updateHud(now, dt) {
    perf.fps = perf.fps * 0.92 + (dt > 0 ? 1 / dt : 60) * 0.08;
    if (!settings.hud || now - perf.lastHud < 250) return;
    perf.lastHud = now;
    hud.hidden = false;
    hud.textContent = `${perf.fps.toFixed(0)} fps · ${perf.frameMs.toFixed(1)} ms/frame · draw ${perf.drawMs.toFixed(1)} ms · `
      + `${agents.size} ${t("office.hud_agents")} · ${particles.length} ${t("office.hud_particles")} · zoom ${cam.zoom.toFixed(2)}`;
  }

  // ------------------------------------------------------------ meeting card

  function renderCard() {
    while (card.firstChild) card.removeChild(card.firstChild);
    if (!meeting) { card.hidden = true; return; }
    card.hidden = false;
    const side = meeting.side === "BUY" ? 1 : -1;
    const h = document.createElement("h4");
    h.textContent = `${t("office.meeting")}: ${meeting.side || ""} ${meeting.symbol || ""} · ${meeting.strategy || ""}`;
    card.append(h);
    for (const v of meeting.votes.slice(0, 8)) {
      const a = agents.get(v.agent);
      const row = document.createElement("div");
      row.className = "vote-row";
      const name = document.createElement("span");
      name.textContent = a ? a.name : v.agent;
      const stance = document.createElement("span");
      stance.className = "muted";
      stance.textContent = t(`stance.${v.stance}`);
      const bar = document.createElement("span");
      bar.className = "vote-bar";
      const fill = document.createElement("span");
      const agrees = (v.stance === "long" ? 1 : v.stance === "short" ? -1 : 0) * side;
      if (agrees < 0) fill.className = "against";
      fill.style.width = `${Math.round((v.confidence || 0) * 100)}%`;
      bar.append(fill);
      row.append(name, stance, bar);
      card.append(row);
    }
    const adv = meeting.advisor;
    const advLine = document.createElement("div");
    advLine.className = "small";
    advLine.textContent = `Claude: ${adv && adv.available ? `${adv.verdict} x${Number(adv.multiplier || 0).toFixed(2)}`
      : t("office.advisor_away")}`;
    card.append(advLine);
    if (meeting.risk) {
      const r = document.createElement("div");
      r.className = "small";
      r.textContent = `${t("office.risk")}: ${meeting.risk.allowed ? "OK" : `VETO (${meeting.risk.reason})`}`;
      card.append(r);
    }
    if (meeting.result) {
      const r = document.createElement("div");
      r.className = "small strong";
      r.textContent = `${t("office.result")}: ${meeting.result.action === "enter"
        ? `${t("decisions.enter")} x${Number(meeting.result.multiplier || 0).toFixed(2)}` : t("decisions.no_trade")}`;
      card.append(r);
    }
    const open = document.createElement("button");
    open.type = "button";
    open.className = "mini-btn";
    open.textContent = t("office.open_dossier");
    const id = meeting.id;
    open.addEventListener("click", () => opts.onDossier(id));
    card.append(open);
  }

  function updateLabel() {
    const busy = [...agents.values()].filter((a) => ["typing", "talking", "meeting", "reading"].includes(a.visual)).length;
    canvas.setAttribute("aria-label", t("office.canvas_summary", { n: agents.size, busy,
      meeting: meeting ? `${meeting.side || ""} ${meeting.symbol || ""}` : t("office.no_meeting") }));
  }

  // ------------------------------------------------------------ loop and sizing

  function resize() {
    const rect = stage.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    vp = { w: Math.max(200, rect.width), h: Math.max(160, rect.height) };
    canvas.width = Math.round(vp.w * dpr);
    canvas.height = Math.round(vp.h * dpr);
    canvas.style.width = `${vp.w}px`;
    canvas.style.height = `${vp.h}px`;
    if (!fitted) { cam = { x: world.w / 2, y: world.h / 2, zoom: fitZoom(vp, world) }; fitted = true; }
    cam = clampCamera(cam, vp, world);
  }

  function frame(now) {
    if (!running) return;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    const started = performance.now();
    update(dt);
    draw();
    drawMinimap();
    if (cardDirty) { cardDirty = false; renderCard(); updateLabel(); }
    perf.frameMs = perf.frameMs * 0.9 + (performance.now() - started) * 0.1;
    updateHud(now, dt);
    raf = requestAnimationFrame(frame);
  }

  function setRunning(on) {
    if (on && !running) { running = true; last = performance.now(); raf = requestAnimationFrame(frame); }
    if (!on && running) { running = false; cancelAnimationFrame(raf); }
  }

  // ------------------------------------------------------------ input

  function agentAt(sx, sy) {
    const [wx, wy] = screenToWorld(cam, vp, sx, sy);
    let best = null;
    let bestD = 22 / Math.min(1, cam.zoom);
    for (const a of agents.values()) {
      const d = Math.hypot(a.x - wx, a.y - SPRITE_H / 2 - wy);
      if (d < bestD) { best = a; bestD = d; }
    }
    return best;
  }

  const pointers = new Map();
  let drag = null;
  let pinch = null;
  canvas.addEventListener("pointerdown", (ev) => {
    canvas.setPointerCapture(ev.pointerId);
    pointers.set(ev.pointerId, [ev.offsetX, ev.offsetY]);
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), zoom: cam.zoom };
      drag = null;
    } else {
      drag = { x: ev.offsetX, y: ev.offsetY, cam: { ...cam }, moved: false };
    }
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (pointers.has(ev.pointerId)) pointers.set(ev.pointerId, [ev.offsetX, ev.offsetY]);
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      cam = clampCamera(zoomAt(cam, vp, (pinch.zoom * (d / pinch.d)) / cam.zoom, mid[0], mid[1]), vp, world);
      return;
    }
    if (drag) {
      const dx = ev.offsetX - drag.x;
      const dy = ev.offsetY - drag.y;
      if (Math.hypot(dx, dy) > 4) { drag.moved = true; followId = null; }
      if (drag.moved) cam = clampCamera({ x: drag.cam.x - dx / cam.zoom, y: drag.cam.y - dy / cam.zoom, zoom: cam.zoom }, vp, world);
      return;
    }
    const hit = agentAt(ev.offsetX, ev.offsetY);
    hoverId = hit ? hit.id : null;
    canvas.style.cursor = hit ? "pointer" : "grab";
    if (hit) {
      tip.hidden = false;
      tip.textContent = `${hit.name} · ${hit.role} · ${t(`state.${hit.state}`)}${hit.task ? `: ${hit.task}` : ""}`;
      tip.style.left = `${Math.min(ev.offsetX + 14, vp.w - 240)}px`;
      tip.style.top = `${Math.max(ev.offsetY - 34, 4)}px`;
    } else tip.hidden = true;
  });
  const endPointer = (ev) => {
    pointers.delete(ev.pointerId);
    if (pointers.size < 2) pinch = null;
    if (drag && !drag.moved && ev.type === "pointerup") {
      const hit = agentAt(ev.offsetX, ev.offsetY);
      if (hit) { selectedId = hit.id; opts.onAgent(hit.id); }
    }
    drag = null;
  };
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("pointerleave", () => { hoverId = null; tip.hidden = true; });
  canvas.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    cam = clampCamera(zoomAt(cam, vp, Math.exp(-ev.deltaY * 0.0015), ev.offsetX, ev.offsetY), vp, world);
  }, { passive: false });
  canvas.addEventListener("dblclick", (ev) => {
    cam = clampCamera(zoomAt(cam, vp, 1.6, ev.offsetX, ev.offsetY), vp, world);
  });
  canvas.addEventListener("keydown", (ev) => {
    const step = 80 / cam.zoom;
    const keys = {
      ArrowLeft: () => { cam = { ...cam, x: cam.x - step }; }, ArrowRight: () => { cam = { ...cam, x: cam.x + step }; },
      ArrowUp: () => { cam = { ...cam, y: cam.y - step }; }, ArrowDown: () => { cam = { ...cam, y: cam.y + step }; },
      "+": () => { cam = zoomAt(cam, vp, 1.25, vp.w / 2, vp.h / 2); }, "=": () => { cam = zoomAt(cam, vp, 1.25, vp.w / 2, vp.h / 2); },
      "-": () => { cam = zoomAt(cam, vp, 0.8, vp.w / 2, vp.h / 2); }, 0: () => resetView(),
      Escape: () => { followId = null; },
    };
    const fn = keys[ev.key];
    if (!fn) return;
    ev.preventDefault();
    if (ev.key.startsWith("Arrow")) followId = null;
    fn();
    cam = clampCamera(cam, vp, world);
  });
  const miniMove = (ev) => {
    const r = mini.getBoundingClientRect();
    cam = clampCamera({ x: ((ev.clientX - r.left) / r.width) * world.w, y: ((ev.clientY - r.top) / r.height) * world.h,
      zoom: cam.zoom }, vp, world);
    followId = null;
  };
  mini.addEventListener("pointerdown", (ev) => { mini.setPointerCapture(ev.pointerId); miniMove(ev); });
  mini.addEventListener("pointermove", (ev) => { if (ev.buttons) miniMove(ev); });

  function resetView() { followId = null; cam = { x: world.w / 2, y: world.h / 2, zoom: fitZoom(vp, world) }; }

  const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(resize) : null;
  if (observer) observer.observe(stage); else window.addEventListener("resize", resize);
  const onVisibility = () => setRunning(!document.hidden);
  document.addEventListener("visibilitychange", onVisibility);
  readColors();
  resize();
  raf = requestAnimationFrame(frame);

  return {
    applySnapshot, applyEvent,
    setSessions(value) { sessions = value || sessions; },
    setSettings(next) {
      settings = { ...settings, ...next };
      hud.hidden = !settings.hud;
      if (settings.reducedMotion) { particles = []; rings = []; for (const a of agents.values()) route(a, true); }
    },
    follow(id) {
      followId = id && agents.has(id) ? id : null;
      selectedId = followId || selectedId;
      if (followId && cam.zoom < 1.1) cam = { ...cam, zoom: 1.4 };
      return followId;
    },
    following: () => followId,
    focusRoom(id) {
      if (!ROOMS[id]) return;
      const [x0, y0, x1, y1] = ROOMS[id];
      followId = null;
      const z = Math.min(vp.w / ((x1 - x0 + 2) * TILE), vp.h / ((y1 - y0 + 2) * TILE), 2.2);
      cam = clampCamera({ x: ((x0 + x1 + 1) / 2) * TILE, y: ((y0 + y1 + 1) / 2) * TILE, zoom: z }, vp, world);
    },
    zoomBy(f) { cam = clampCamera(zoomAt(cam, vp, f, vp.w / 2, vp.h / 2), vp, world); },
    resetView,
    resetForReplay(list) {
      meeting = null; particles = []; rings = []; cardDirty = true;
      for (const a of agents.values()) {
        a.visual = "idle"; a.flash = null; a.bubble = null; a.room = a.home; a.benched = false; a.seat = null; route(a, true);
      }
      if (list) applySnapshot({ agents: list.map((i) => ({ ...i, visual: "idle", room: i.home, bubble: null })) });
    },
    refreshTheme() { readColors(); if (map) drawBackground(); },
    agentsList: () => [...agents.values()],
    perf: () => ({ ...perf, agents: agents.size, particles: particles.length, zoom: cam.zoom }),
    destroy() {
      setRunning(false);
      document.removeEventListener("visibilitychange", onVisibility);
      if (observer) observer.disconnect(); else window.removeEventListener("resize", resize);
    },
  };
}

