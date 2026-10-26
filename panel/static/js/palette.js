// Command palette (Ctrl+K / Cmd+K, or "/"): go to a tab, follow an agent, change a
// preference, start the replay or the tour. ARIA combobox + listbox, fully keyboard driven.

import { t } from "./i18n.js";
import { clear, el } from "./util.js";

let open = false;
let provider = () => [];
let lastFocus = null;

function normalise(text) {
  return String(text).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
}

/** Subsequence match score (higher is better); -1 when the query does not match. */
export function score(query, text) {
  const q = normalise(query).trim();
  const s = normalise(text);
  if (!q) return 0;
  if (s.includes(q)) return 1000 - s.indexOf(q);
  let i = 0;
  let gaps = 0;
  for (const ch of s) {
    if (ch === q[i]) i += 1; else if (i > 0) gaps += 1;
    if (i === q.length) return 50 - gaps;
  }
  return -1;
}

export function registerCommands(fn) { provider = fn; }

export function isPaletteOpen() { return open; }

export function openPalette() {
  if (open) return;
  open = true;
  lastFocus = document.activeElement;
  const root = document.getElementById("palette");
  const input = el("input", { type: "text", class: "palette-input", role: "combobox", "aria-expanded": "true",
    "aria-controls": "palette-list", "aria-autocomplete": "list", placeholder: t("palette.placeholder"),
    "aria-label": t("palette.label"), spellcheck: "false", autocomplete: "off" });
  const list = el("ul", { class: "palette-list", id: "palette-list", role: "listbox", "aria-label": t("palette.label") });
  const hint = el("p", { class: "palette-hint", text: t("palette.hint") });
  clear(root).append(el("div", { class: "palette-box", role: "dialog", "aria-modal": "true", "aria-label": t("palette.label") },
    input, list, hint));
  root.hidden = false;
  let items = [];
  let active = 0;

  function render() {
    const all = provider();
    const q = input.value;
    let scored = all.map((c) => ({ ...c, s: score(q, `${c.label} ${c.keywords || ""} ${c.group}`) })).filter((c) => c.s >= 0);
    if (scored.some((c) => c.s >= 900)) scored = scored.filter((c) => c.s >= 900); // literal matches beat fuzzy ones
    items = scored.sort((a, b) => b.s - a.s).slice(0, 40);
    active = Math.min(active, Math.max(0, items.length - 1));
    clear(list);
    items.forEach((c, i) => {
      const li = el("li", { role: "option", id: `palette-opt-${i}`, class: i === active ? "active" : "",
        "aria-selected": i === active ? "true" : "false" },
      el("span", { class: "palette-group", text: c.group }), el("span", { class: "palette-label", text: c.label }),
      c.hint ? el("kbd", { text: c.hint }) : null);
      li.addEventListener("mousemove", () => { if (active !== i) { active = i; render(); } });
      li.addEventListener("click", () => run(i));
      list.append(li);
    });
    if (!items.length) list.append(el("li", { class: "palette-empty", text: t("palette.empty") }));
    input.setAttribute("aria-activedescendant", items.length ? `palette-opt-${active}` : "");
    const node = document.getElementById(`palette-opt-${active}`);
    if (node && node.scrollIntoView) node.scrollIntoView({ block: "nearest" });
  }

  function run(i) {
    const cmd = items[i];
    closePalette();
    if (cmd) setTimeout(() => cmd.run(), 0);
  }

  input.addEventListener("input", () => { active = 0; render(); });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") { ev.preventDefault(); active = Math.min(items.length - 1, active + 1); render(); }
    if (ev.key === "ArrowUp") { ev.preventDefault(); active = Math.max(0, active - 1); render(); }
    if (ev.key === "Enter") { ev.preventDefault(); run(active); }
    if (ev.key === "Escape") { ev.preventDefault(); closePalette(); }
    if (ev.key === "Tab") ev.preventDefault(); // focus stays in the dialog
  });
  root.addEventListener("mousedown", (ev) => { if (ev.target === root) closePalette(); }, { once: true });
  render();
  input.focus();
}

export function closePalette() {
  if (!open) return;
  open = false;
  const root = document.getElementById("palette");
  clear(root);
  root.hidden = true;
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

export function wirePalette() {
  document.addEventListener("keydown", (ev) => {
    const typing = ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA" || ev.target.isContentEditable);
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); if (open) closePalette(); else openPalette(); }
    else if (ev.key === "/" && !typing && !open) { ev.preventDefault(); openPalette(); }
  });
}
