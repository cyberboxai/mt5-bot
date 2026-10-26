// Original procedural pixel-art characters. Each agent's look is derived from its id (stable
// and distinct) and its role; the department sets the shirt in the CYBERBOX palette. Frames
// are rendered once into small offscreen canvases and cached: drawing a character is one
// drawImage per frame.
//
// A sprite is 12 x 18 units; the feet are at the bottom centre. Directions: down, left,
// right, up. Walk cycle: 4 frames. Poses: stand, walk, sit, type, talk, read, celebrate,
// veto, rest, ticket, error.

export const UNITS_W = 12;
export const UNITS_H = 18;
export const UNIT = 2;            // world pixels per unit: a sprite is 24 x 36 world px
export const RENDER_UNIT = 4;     // backing pixels per unit (crisp up to ~2x zoom)

const SKIN = ["#f6d5b8", "#e8b48f", "#c98b5e", "#a26a43", "#7d4b2a", "#5a341c"];
const HAIR = ["#1b1714", "#3d2a1d", "#6e4a2b", "#b07a3f", "#e0c070", "#9aa0a6", "#7a2b2b", "#2b3f6e"];
const PANTS = ["#1f2a44", "#2b2f36", "#5b4a36", "#111827", "#3a3f58"];
const SHOES = ["#0b0f16", "#3b2416", "#e8edf7"];
export const DEPT_COLOR = {
  market_intelligence: "#00CFCF", strategy_desk: "#00C9A7", risk_control: "#E63030", execution: "#F47C20",
  research_review: "#1874CD", operations_leadership: "#1A3A6E",
};
const STYLES = ["short", "long", "bun", "curly", "cap", "crop", "side"];
const OUTLINE = "#050A10";

function hash(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

function shade(hex, amount) {
  const n = parseInt(hex.slice(1), 16);
  const ch = (v) => Math.max(0, Math.min(255, Math.round(v + amount * 255)));
  const r = ch(n >> 16);
  const g = ch((n >> 8) & 255);
  const b = ch(n & 255);
  return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
}

/** The stable look of one agent (also used by the roster's mini portraits). */
export function lookFor(agent) {
  const h = hash(agent.id);
  const role = agent.id;
  const coat = ["quant_researcher", "validation_auditor", "meta_labeler", "flow_doctor"].includes(role);
  return {
    id: agent.id,
    skin: SKIN[h % SKIN.length],
    hair: HAIR[(h >>> 3) % HAIR.length],
    style: role === "guardian" ? "cap" : STYLES[(h >>> 6) % STYLES.length],
    shirt: DEPT_COLOR[agent.department] || "#7A899E",
    pants: PANTS[(h >>> 9) % PANTS.length],
    shoes: SHOES[(h >>> 12) % 2],
    coat,
    glasses: coat || ((h >>> 14) & 3) === 0 || role === "performance_analyst",
    headset: ["execution_trader", "ops_engineer", "data_clerk"].includes(role),
    tie: role === "cio",
    blazer: role === "cio" || role === "risk_manager",
    badge: role === "risk_manager" || role === "guardian",
    scarf: role === "reporter",
    pin: role === "claude_strategist" ? "#F47C20" : role === "flow_doctor" ? "#E63030" : null,
    beanie: role === "news_sentinel",
  };
}

// ------------------------------------------------------------------ painter

function painter(ctx) {
  return (x, y, w, h, color) => {
    if (!color || w <= 0 || h <= 0) return;
    ctx.fillStyle = color;
    ctx.fillRect(x * RENDER_UNIT, y * RENDER_UNIT, w * RENDER_UNIT, h * RENDER_UNIT);
  };
}

function hairCap(p, look, dir, top) {
  const c = look.hair;
  const d = shade(c, -0.08);
  if (look.beanie) { p(3, top - 1, 6, 3, "#1874CD"); p(3, top + 1, 6, 1, "#0f4f91"); return; }
  if (look.style === "cap") {
    p(3, top - 1, 6, 2, "#1A3A6E");
    if (dir === "down") p(3, top + 1, 6, 1, "#0f2244");
    if (dir === "left") p(1, top + 1, 3, 1, "#0f2244");
    if (dir === "right") p(8, top + 1, 3, 1, "#0f2244");
    return;
  }
  p(3, top - 1, 6, 2, c);
  if (dir === "up") { p(3, top + 1, 6, 4, c); p(3, top + 4, 6, 1, d); }
  if (dir === "left") p(6, top + 1, 3, 3, c);
  if (dir === "right") p(3, top + 1, 3, 3, c);
  if (dir === "down") { p(3, top + 1, 1, 2, c); p(8, top + 1, 1, 2, c); }
  if (look.style === "long") {
    p(2, top, 1, 6, c); p(9, top, 1, 6, c);
    if (dir === "up") p(3, top + 4, 6, 3, d);
  }
  if (look.style === "bun") p(5, top - 3, 2, 2, c);
  if (look.style === "curly") { p(2, top - 1, 1, 3, c); p(9, top - 1, 1, 3, c); p(4, top - 2, 4, 1, c); }
  if (look.style === "side" && dir !== "up") p(dir === "left" ? 2 : 7, top, 3, 1, d);
}

function face(p, look, dir, top, mouthOpen) {
  if (dir === "up") return;
  const eye = "#111827";
  if (dir === "down") {
    p(4, top + 2, 1, 1, eye); p(7, top + 2, 1, 1, eye);
    if (look.glasses) { p(3, top + 2, 2, 1, "#E8EDF7"); p(7, top + 2, 2, 1, "#E8EDF7"); p(4, top + 2, 1, 1, eye);
      p(7, top + 2, 1, 1, eye); p(5, top + 2, 2, 1, "#9aa0a6"); }
    p(5, top + 4, 2, 1, mouthOpen ? "#7a2b2b" : shade(look.skin, -0.18));
  } else {
    const ex = dir === "left" ? 3 : 8;
    p(ex, top + 2, 1, 1, eye);
    if (look.glasses) p(dir === "left" ? 3 : 7, top + 2, 2, 1, "#9aa0a6");
    p(dir === "left" ? 3 : 8, top + 4, 1, 1, mouthOpen ? "#7a2b2b" : shade(look.skin, -0.18));
  }
}

/** Draw one frame into a fresh canvas (12 x 18 units at RENDER_UNIT). */
function render(look, dir, pose, frame, seatedFlag) {
  const canvas = typeof OffscreenCanvas !== "undefined"
    ? new OffscreenCanvas(UNITS_W * RENDER_UNIT, UNITS_H * RENDER_UNIT)
    : Object.assign(document.createElement("canvas"), { width: UNITS_W * RENDER_UNIT, height: UNITS_H * RENDER_UNIT });
  const ctx = canvas.getContext("2d");
  const p = painter(ctx);
  const seated = seatedFlag || ["sit", "type", "rest"].includes(pose);
  const walking = pose === "walk";
  const step = walking ? frame % 4 : 0;
  const bob = walking && (step === 1 || step === 3) ? -1 : pose === "celebrate" && frame % 2 ? -2 : 0;
  const top = 1 + bob + (seated ? 2 : 0);            // head top row
  const bodyTop = top + 6;
  const skin = look.skin;
  const shirt = look.coat ? "#E8EDF7" : look.blazer ? "#1A3A6E" : look.shirt;
  const sleeve = look.coat ? "#d5dbe6" : shade(shirt, -0.08);

  // Legs and shoes.
  if (seated) {
    p(3, bodyTop + 6, 6, 2, look.pants);
    if (dir === "down") { p(3, bodyTop + 8, 2, 2, look.pants); p(7, bodyTop + 8, 2, 2, look.pants);
      p(3, bodyTop + 10, 2, 1, look.shoes); p(7, bodyTop + 10, 2, 1, look.shoes); }
  } else {
    const lift = (leg) => (walking && ((leg === 0 && step === 1) || (leg === 1 && step === 3)) ? 1 : 0);
    const legX = dir === "left" || dir === "right" ? [[4, 0], [6, 1]] : [[3, 0], [7, 1]];
    for (const [x, leg] of legX) {
      const swing = walking && (dir === "left" || dir === "right") ? (leg === 0 ? (step === 1 ? -1 : step === 3 ? 1 : 0)
        : (step === 1 ? 1 : step === 3 ? -1 : 0)) : 0;
      p(x + swing, bodyTop + 6, 2, 4 - lift(leg), look.pants);
      p(x + swing, bodyTop + 10 - lift(leg), 2, 1, look.shoes);
    }
  }

  // Torso.
  p(2, bodyTop, 8, 6, shirt);
  p(2, bodyTop + 5, 8, 1, shade(shirt, -0.12));
  if (look.coat) { p(5, bodyTop, 2, 6, look.shirt); }                      // coat over the department shirt
  if (look.blazer && dir === "down") { p(5, bodyTop, 2, 3, "#E8EDF7"); }
  if (look.tie && dir === "down") { p(5, bodyTop + 1, 2, 4, "#FFE600"); }
  if (look.scarf) p(3, bodyTop, 6, 1, "#F47C20");
  if (look.badge && dir === "down") p(3, bodyTop + 1, 2, 2, "#FFE600");
  if (look.pin && dir !== "up") p(dir === "left" ? 3 : 7, bodyTop + 1, 1, 1, look.pin);

  // Arms by pose.
  const armL = (y, h) => { p(1, bodyTop + y, 1, h, sleeve); p(1, bodyTop + y + h, 1, 1, skin); };
  const armR = (y, h) => { p(10, bodyTop + y, 1, h, sleeve); p(10, bodyTop + y + h, 1, 1, skin); };
  if (pose === "celebrate") {
    p(1, top - 2 + (frame % 2), 1, 6, sleeve); p(10, top - 2 + ((frame + 1) % 2), 1, 6, sleeve);
    p(1, top - 3 + (frame % 2), 1, 1, skin); p(10, top - 3 + ((frame + 1) % 2), 1, 1, skin);
  } else if (pose === "veto") {
    armL(0, 4);
    p(10, top - 1, 1, 7, sleeve); p(10, top - 2, 1, 1, skin);
  } else if (pose === "talk") {
    armL(0, 4);
    const up = frame % 2 === 0;
    p(10, bodyTop + (up ? -2 : 0), 1, 4, sleeve); p(10, bodyTop + (up ? -3 : 4), 1, 1, skin);
  } else if (pose === "error") {
    p(1, top + 1, 1, 6, sleeve); p(10, top + 1, 1, 6, sleeve); p(2, top, 1, 1, skin); p(9, top, 1, 1, skin);
  } else if (pose === "type") {
    const a = frame % 2;
    if (dir === "up") { p(2, bodyTop + 2, 2, 2, sleeve); p(8, bodyTop + 2, 2, 2, sleeve);
      p(3 + a, bodyTop + 1, 1, 1, skin); p(8 - a, bodyTop + 1, 1, 1, skin); } else { armL(0, 4); armR(0, 4); }
  } else if (pose === "read" || pose === "ticket" || pose === "rest") {
    armL(0, 3); armR(0, 3);
    if (dir !== "up") {
      const prop = pose === "read" ? "#E8EDF7" : pose === "ticket" ? "#FFE600" : "#7a4b2a";
      if (pose === "rest") { p(8, bodyTop + 2, 2, 3, "#E8EDF7"); p(8, bodyTop + 2, 2, 1, prop); }
      else p(3, bodyTop + 2, 6, 3, prop);
      if (pose === "read") { p(4, bodyTop + 3, 4, 1, "#7A899E"); }
    }
  } else if (walking) {
    const swing = step === 1 ? 1 : step === 3 ? -1 : 0;
    armL(swing, 4); armR(-swing, 4);
  } else {
    armL(0, 4); armR(0, 4);
  }

  // Head.
  p(3, top, 6, 6, skin);
  p(3, top + 5, 6, 1, shade(skin, -0.1));
  hairCap(p, look, dir, top);
  face(p, look, dir, top, pose === "talk" && frame % 2 === 0);
  if (look.headset) {
    p(2, top + 1, 1, 3, "#111827"); p(9, top + 1, 1, 3, "#111827"); p(3, top - 1, 6, 1, "#111827");
    if (dir !== "up") p(dir === "left" ? 2 : 8, top + 4, 2, 1, "#111827");
  }

  // One-unit outline around the silhouette, for contrast on any floor.
  const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const out = ctx.createImageData(canvas.width, canvas.height);
  const W = canvas.width;
  const H = canvas.height;
  const filled = (x, y) => x >= 0 && y >= 0 && x < W && y < H && img.data[(y * W + x) * 4 + 3] > 0;
  const o = parseInt(OUTLINE.slice(1), 16);
  for (let y = 0; y < H; y += RENDER_UNIT) {
    for (let x = 0; x < W; x += RENDER_UNIT) {
      if (filled(x, y)) continue;
      const near = filled(x - RENDER_UNIT, y) || filled(x + RENDER_UNIT, y) || filled(x, y - RENDER_UNIT)
        || filled(x, y + RENDER_UNIT);
      if (!near) continue;
      for (let j = 0; j < RENDER_UNIT; j += 1) {
        for (let i = 0; i < RENDER_UNIT; i += 1) {
          const k = ((y + j) * W + x + i) * 4;
          out.data[k] = o >> 16; out.data[k + 1] = (o >> 8) & 255; out.data[k + 2] = o & 255; out.data[k + 3] = 200;
        }
      }
    }
  }
  const final = typeof OffscreenCanvas !== "undefined"
    ? new OffscreenCanvas(W, H) : Object.assign(document.createElement("canvas"), { width: W, height: H });
  const fctx = final.getContext("2d");
  fctx.putImageData(out, 0, 0);
  fctx.drawImage(canvas, 0, 0);
  return final;
}

const cache = new Map();
const MAX_CACHE = 1200;

/** Cached frame canvas for (agent look, direction, pose, frame, seated). */
export function frameFor(look, dir, pose, frame, seated = false) {
  const f = pose === "walk" ? frame % 4 : ["type", "talk", "celebrate"].includes(pose) ? frame % 2 : 0;
  const key = `${look.id}|${dir}|${pose}|${f}|${seated ? 1 : 0}`;
  let canvas = cache.get(key);
  if (!canvas) {
    canvas = render(look, dir, pose, f, seated);
    cache.set(key, canvas);
    if (cache.size > MAX_CACHE) cache.delete(cache.keys().next().value);
  }
  return canvas;
}

export function clearSpriteCache() { cache.clear(); }

/** Small portrait (head and shoulders) for lists: a data URL is not needed, the canvas is returned. */
export function portrait(look, size = 36) {
  const frame = frameFor(look, "down", "stand", 0);
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  const scale = size / (8 * RENDER_UNIT);
  ctx.drawImage(frame, 2 * RENDER_UNIT, 0, 8 * RENDER_UNIT, 8 * RENDER_UNIT, 0, 0, 8 * RENDER_UNIT * scale,
    8 * RENDER_UNIT * scale);
  return canvas;
}
