// Viewer preferences (this browser only): theme, motion, effects level, contrast, perf HUD.
// Applied as data attributes on <html> so the CSS and the office engine follow them.

import { storeGet, storeSet } from "./util.js";

const DEFAULTS = { theme: "dark", motion: "auto", effects: "high", contrast: "normal", hud: "off" };
const listeners = new Set();
let current = null;

function systemReduced() {
  return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

export function settings() {
  if (!current) {
    current = {};
    for (const [key, value] of Object.entries(DEFAULTS)) current[key] = storeGet(`cbx-${key}`, value);
  }
  return { ...current, reducedMotion: current.motion === "reduced" || (current.motion === "auto" && systemReduced()) };
}

export function setSetting(key, value) {
  if (!(key in DEFAULTS)) return;
  settings();
  current[key] = value;
  storeSet(`cbx-${key}`, value);
  apply();
}

export function apply() {
  const s = settings();
  const root = document.documentElement;
  root.dataset.theme = s.theme;
  root.dataset.contrast = s.contrast;
  root.dataset.motion = s.reducedMotion ? "reduced" : "full";
  root.dataset.effects = s.effects;
  for (const fn of listeners) {
    try { fn(s); } catch (e) { console.warn("settings listener failed", e); }
  }
}

export function onSettings(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export function cycle(key, values) {
  const s = settings();
  const i = values.indexOf(s[key]);
  setSetting(key, values[(i + 1) % values.length]);
  return settings()[key];
}

if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (mq.addEventListener) mq.addEventListener("change", () => apply());
}
