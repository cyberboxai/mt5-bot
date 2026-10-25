// Original, procedurally drawn characters (canvas). Each agent's look is derived from a hash
// of its id, so it is stable and distinct; the department sets the shirt colour.

import { hashString } from "./util.js";

const SKIN = ["#f1c9a5", "#e0ac83", "#c68c5c", "#a86f45", "#8a5530", "#5e3a20"];
const HAIR = ["#1d1a17", "#3b2a1e", "#6b4a2e", "#a5753e", "#d8b36a", "#9b9b9b", "#5a2a2a"];
const SHIRT = {
  market_intelligence: "#2f7fb5", strategy_desk: "#3f9f73", risk_control: "#b5473f", execution: "#c58a1f",
  research_review: "#7a5cc4", operations_leadership: "#4b5563",
};
const STYLES = ["short", "long", "bun", "curly", "cap", "side"];

export function look(agent) {
  const h = hashString(agent.id);
  return {
    skin: SKIN[h % SKIN.length], hair: HAIR[(h >> 3) % HAIR.length], style: STYLES[(h >> 6) % STYLES.length],
    shirt: SHIRT[agent.department] || "#4b5563", glasses: ((h >> 9) & 3) === 0 || agent.department === "research_review",
  };
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

function hair(ctx, style, color, hx, hy) {
  ctx.fillStyle = color;
  ctx.beginPath();
  if (style === "cap") {
    ctx.arc(hx, hy - 2, 10.5, Math.PI, 0);
    ctx.fill();
    ctx.fillRect(hx - 2, hy - 4, 14, 3);
    return;
  }
  ctx.arc(hx, hy - 1, 10, Math.PI * 1.05, Math.PI * 1.95);
  ctx.fill();
  if (style === "long") { ctx.fillRect(hx - 10, hy - 2, 4, 14); ctx.fillRect(hx + 6, hy - 2, 4, 14); }
  if (style === "bun") { ctx.beginPath(); ctx.arc(hx, hy - 12, 5, 0, Math.PI * 2); ctx.fill(); }
  if (style === "curly") {
    for (let i = -2; i <= 2; i += 1) { ctx.beginPath(); ctx.arc(hx + i * 4, hy - 9, 3.5, 0, Math.PI * 2); ctx.fill(); }
  }
  if (style === "side") { ctx.beginPath(); ctx.ellipse(hx - 5, hy - 6, 7, 4, -0.4, 0, Math.PI * 2); ctx.fill(); }
}

function accessory(ctx, agent, hx, hy, bx, by) {
  const id = agent.id;
  ctx.lineWidth = 1.5;
  if (id === "cio") { // tie
    ctx.fillStyle = "#d4a017";
    ctx.beginPath(); ctx.moveTo(bx, by + 2); ctx.lineTo(bx - 3, by + 12); ctx.lineTo(bx, by + 15); ctx.lineTo(bx + 3, by + 12);
    ctx.closePath(); ctx.fill();
  } else if (id === "execution_trader" || id === "ops_engineer" || id === "data_clerk") { // headset
    ctx.strokeStyle = "#111"; ctx.beginPath(); ctx.arc(hx, hy, 11, Math.PI * 1.1, Math.PI * 1.9); ctx.stroke();
    ctx.fillStyle = "#111"; ctx.fillRect(hx + 9, hy - 2, 3, 6);
  } else if (id === "risk_manager" || id === "guardian") { // shield badge
    ctx.fillStyle = id === "guardian" ? "#e5e7eb" : "#fde68a";
    ctx.beginPath(); ctx.moveTo(bx + 5, by + 4); ctx.lineTo(bx + 11, by + 6); ctx.lineTo(bx + 10, by + 12);
    ctx.lineTo(bx + 8, by + 14); ctx.lineTo(bx + 6, by + 12); ctx.closePath(); ctx.fill();
  } else if (id === "claude_strategist") { // spark pin
    ctx.fillStyle = "#f59e0b";
    ctx.beginPath();
    for (let i = 0; i < 8; i += 1) {
      const r = i % 2 ? 2 : 5;
      const a = (Math.PI / 4) * i;
      ctx.lineTo(bx + 8 + Math.cos(a) * r, by + 8 + Math.sin(a) * r);
    }
    ctx.closePath(); ctx.fill();
  }
}

// visual: idle | typing | reading | talking | meeting | ticket | veto | celebrating | resting | error | away
export function drawAgent(ctx, agent, x, y, t, opts) {
  const lk = agent.look || (agent.look = look(agent));
  const walking = opts.walking;
  const visual = agent.flash || agent.visual || "idle";
  const motion = opts.reduced ? 0 : 1;
  const bob = motion * (visual === "typing" ? Math.sin(t * 12 + agent.phase) * 0.8 : 0);
  const jump = motion * (visual === "celebrating" ? Math.abs(Math.sin(t * 7 + agent.phase)) * 7 : 0);
  const alpha = visual === "away" ? 0.45 : 1;
  ctx.save();
  ctx.globalAlpha = alpha;
  const by = y - jump;
  // shadow
  ctx.fillStyle = "rgba(0,0,0,0.25)";
  ctx.beginPath(); ctx.ellipse(x, y + 18, 12, 4, 0, 0, Math.PI * 2); ctx.fill();
  if (visual === "resting") {
    // reclined on the sofa
    ctx.fillStyle = lk.shirt; roundRect(ctx, x - 14, y - 2, 28, 12, 6); ctx.fill();
    ctx.fillStyle = lk.skin; ctx.beginPath(); ctx.arc(x - 18, y + 1, 7.5, 0, Math.PI * 2); ctx.fill();
    hair(ctx, lk.style, lk.hair, x - 18, y + 2);
    ctx.fillStyle = opts.ink; ctx.font = "bold 11px system-ui"; ctx.globalAlpha = 0.6 + 0.4 * Math.sin(t * 2 + agent.phase);
    ctx.fillText("z", x - 6, y - 12 - (motion * (t * 6 % 8))); ctx.fillText("Z", x + 2, y - 20 - (motion * (t * 6 % 8)));
    ctx.restore();
    return;
  }
  // legs
  ctx.fillStyle = "#2b2f38";
  const step = walking && motion ? Math.sin(t * 14 + agent.phase) * 4 : 0;
  ctx.fillRect(x - 6, by + 8, 5, 10 + step * 0.3);
  ctx.fillRect(x + 1, by + 8, 5, 10 - step * 0.3);
  // torso
  ctx.fillStyle = lk.shirt;
  roundRect(ctx, x - 10, by - 6 + bob, 20, 18, 6); ctx.fill();
  // arms
  ctx.strokeStyle = lk.skin; ctx.lineWidth = 3.5; ctx.lineCap = "round";
  const armUp = visual === "celebrating" || visual === "veto";
  ctx.beginPath();
  if (armUp) {
    ctx.moveTo(x - 9, by - 2); ctx.lineTo(x - 15, by - 16);
    ctx.moveTo(x + 9, by - 2); ctx.lineTo(x + 15, by - 16);
  } else if (visual === "typing") {
    ctx.moveTo(x - 9, by); ctx.lineTo(x - 6, by + 8 + bob);
    ctx.moveTo(x + 9, by); ctx.lineTo(x + 6, by + 8 - bob);
  } else if (visual === "ticket" || visual === "reading") {
    ctx.moveTo(x - 9, by); ctx.lineTo(x - 13, by + 8);
    ctx.moveTo(x + 9, by); ctx.lineTo(x + 18, by + 2);
  } else {
    ctx.moveTo(x - 9, by); ctx.lineTo(x - 12, by + 10 + step * 0.4);
    ctx.moveTo(x + 9, by); ctx.lineTo(x + 12, by + 10 - step * 0.4);
  }
  ctx.stroke();
  // head
  const hx = x;
  const hy = by - 15 + bob * 0.5;
  ctx.fillStyle = lk.skin;
  ctx.beginPath(); ctx.arc(hx, hy, 9, 0, Math.PI * 2); ctx.fill();
  hair(ctx, lk.style, lk.hair, hx, hy);
  // eyes and mouth
  ctx.fillStyle = "#1b1b1b";
  ctx.fillRect(hx - 4, hy - 1, 2, 2); ctx.fillRect(hx + 2, hy - 1, 2, 2);
  if (lk.glasses) {
    ctx.strokeStyle = "#1b1b1b"; ctx.lineWidth = 1;
    ctx.strokeRect(hx - 5.5, hy - 2.5, 5, 4); ctx.strokeRect(hx + 0.5, hy - 2.5, 5, 4);
  }
  const talking = (visual === "talking" || visual === "meeting") && motion && Math.sin(t * 10 + agent.phase) > 0;
  ctx.fillStyle = "#7a2d2d";
  ctx.fillRect(hx - 2, hy + 4, 4, talking ? 2.5 : 1.2);
  accessory(ctx, agent, hx, hy, x - 4, by - 6);
  // props
  if (visual === "reading") {
    ctx.fillStyle = "#f3f4f6"; ctx.fillRect(x + 14, by - 4, 9, 12);
    ctx.fillStyle = "#9ca3af"; for (let i = 0; i < 3; i += 1) ctx.fillRect(x + 15, by - 2 + i * 3, 7, 1);
  }
  if (visual === "ticket") {
    ctx.fillStyle = "#fefce8"; ctx.fillRect(x + 16, by - 6, 16, 10);
    ctx.fillStyle = "#1f2937"; ctx.font = "bold 6px system-ui"; ctx.fillText(opts.ticketText || "ORDEM", x + 17, by + 1);
  }
  if (visual === "veto") {
    const sx = x + 17;
    const sy = by - 26;
    ctx.fillStyle = "#c62828"; ctx.beginPath();
    for (let i = 0; i < 8; i += 1) {
      const a = Math.PI / 8 + (Math.PI / 4) * i;
      ctx.lineTo(sx + Math.cos(a) * 11, sy + Math.sin(a) * 11);
    }
    ctx.closePath(); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.font = "bold 7px system-ui"; ctx.textAlign = "center"; ctx.fillText("VETO", sx, sy + 3);
    ctx.textAlign = "left";
  }
  if (visual === "error") {
    const on = !motion || Math.floor(t * 3) % 2 === 0;
    ctx.fillStyle = on ? "#ef4444" : "#7f1d1d";
    ctx.beginPath(); ctx.arc(hx, hy - 17, 4.5, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.font = "bold 7px system-ui"; ctx.textAlign = "center"; ctx.fillText("!", hx, hy - 14.5);
    ctx.textAlign = "left";
  }
  if (visual === "away") {
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#6b7280"; roundRect(ctx, x - 20, by - 40, 40, 12, 3); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.font = "bold 8px system-ui"; ctx.textAlign = "center";
    ctx.fillText(opts.awayText || "AUSENTE", x, by - 31); ctx.textAlign = "left";
  }
  if (visual === "talking" && motion) {
    ctx.strokeStyle = opts.muted; ctx.lineWidth = 1;
    for (let i = 1; i <= 2; i += 1) {
      ctx.beginPath(); ctx.arc(hx + 11, hy, 3 * i + (t * 6 % 3), -0.6, 0.6); ctx.stroke();
    }
  }
  ctx.restore();
  // name tag (never transparent)
  ctx.save();
  ctx.font = "600 10px system-ui";
  const label = agent.name;
  const w = ctx.measureText(label).width + 10;
  ctx.fillStyle = opts.tagBg;
  roundRect(ctx, x - w / 2, y + 21, w, 14, 4); ctx.fill();
  ctx.fillStyle = opts.ink; ctx.textAlign = "center";
  ctx.fillText(label, x, y + 31.5);
  ctx.restore();
}

// Bubble geometry (wrapped to 3 lines), above the speaker by default; null for empty text.
export function layoutBubble(ctx, x, y, text, opts) {
  if (!text) return null;
  ctx.save();
  ctx.font = "11px system-ui";
  const maxW = 190;
  const words = String(text).split(/\s+/);
  const lines = [];
  let line = "";
  for (const word of words) {
    const test = line ? `${line} ${word}` : word;
    if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = word; } else line = test;
    if (lines.length === 3) break;
  }
  if (lines.length < 3 && line) lines.push(line);
  if (lines.length === 3 && words.join(" ").length > lines.join(" ").length) lines[2] = `${lines[2].slice(0, -1)}…`;
  const w = Math.min(maxW, Math.max(...lines.map((l) => ctx.measureText(l).width))) + 14;
  ctx.restore();
  const h = lines.length * 14 + 8;
  const bx = Math.max(4, Math.min(opts.width - w - 4, x - w / 2));
  return { x, y, lines, w, h, bx, by: Math.max(4, y - h - 44), below: false };
}

// The same bubble moved under the speaker (used when the space above is taken).
export function bubbleBelow(b, opts) {
  return { ...b, by: Math.min(opts.height - b.h - 4, b.y + 30), below: true };
}

export function paintBubble(ctx, b, alpha, opts) {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.font = "11px system-ui";
  ctx.fillStyle = opts.bubbleBg;
  ctx.strokeStyle = opts.border;
  ctx.lineWidth = 1;
  roundRect(ctx, b.bx, b.by, b.w, b.h, 7); ctx.fill(); ctx.stroke();
  ctx.beginPath();
  if (b.below) { ctx.moveTo(b.x - 4, b.by); ctx.lineTo(b.x, b.by - 7); ctx.lineTo(b.x + 4, b.by); } else {
    ctx.moveTo(b.x - 4, b.by + b.h); ctx.lineTo(b.x, b.by + b.h + 7); ctx.lineTo(b.x + 4, b.by + b.h);
  }
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = opts.ink;
  b.lines.forEach((l, i) => ctx.fillText(l, b.bx + 7, b.by + 15 + i * 14));
  ctx.restore();
}

export { roundRect };
