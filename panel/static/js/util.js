// Small helpers. Every text goes through textContent: data from the bot is never parsed as HTML.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") Object.assign(node.style, value); // CSSOM: allowed by the CSP
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null) node.setAttribute(key, String(value));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

let locale = "pt-PT";
export function setLocale(value) { locale = value === "en" ? "en-GB" : "pt-PT"; }

export function num(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/d";
  return Number(value).toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function compact(value) {
  if (value === null || value === undefined) return "n/d";
  const abs = Math.abs(value);
  if (abs >= 1e6) return `${num(value / 1e6, 1)}M`;
  if (abs >= 1e4) return `${num(value / 1e3, 1)}K`;
  return num(value, abs >= 100 ? 0 : 2);
}

export function signed(value, digits = 2, suffix = "") {
  if (value === null || value === undefined) return "n/d";
  const text = num(value, digits);
  return `${value > 0 ? "+" : ""}${text}${suffix}`;
}

export function pct(value, digits = 1) {
  if (value === null || value === undefined) return "n/d";
  return `${num(value * 100, digits)}%`;
}

export function dateTime(ts, withSeconds = false) {
  if (!ts) return "n/d";
  const d = new Date(ts * 1000);
  return d.toLocaleString(locale, {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined, timeZone: "UTC",
  }) + " UTC";
}

export function timeOnly(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
}

export function ago(seconds) {
  if (seconds === null || seconds === undefined) return "n/d";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${num(seconds / 3600, 1)} h`;
}

export function clamp(x, lo, hi) { return Math.min(hi, Math.max(lo, x)); }

export function downloadCsv(filename, rows) {
  if (!rows || !rows.length) return;
  const keys = Object.keys(rows[0]);
  const escape = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const text = [keys.join(","), ...rows.map((r) => keys.map((k) => escape(r[k])).join(","))].join("\n");
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function storeGet(key, fallback) {
  try {
    const v = window.localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch (e) {
    return fallback;
  }
}

export function storeSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* private mode: preference not kept */ }
}

export function hashString(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

export function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function tile(label, value, note, cls = "") {
  const long = String(value ?? "").length > 10;
  return el("div", { class: `tile ${cls}` }, el("div", { class: "label", text: label }),
    el("div", { class: long ? "value long" : "value", text: value }), note ? el("div", { class: "note", text: note }) : null);
}

export function card(title, sub, ...body) {
  return el("section", { class: "card" }, el("h3", { text: title }), sub ? el("p", { class: "card-sub", text: sub }) : null, ...body);
}

export function empty(text) { return el("div", { class: "empty", text }); }
