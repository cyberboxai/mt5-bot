// Onboarding tour: a few steps that point at the main areas of the office. Starts once on the
// first visit (remembered in this browser), and any time from the palette or the "?" button.

import { t } from "./i18n.js";
import { clear, el, storeGet, storeSet } from "./util.js";

const STEPS = [
  { target: "#account-badge", key: "tour.account" },
  { target: "#kpis", key: "tour.kpis" },
  { target: "#roster", key: "tour.roster" },
  { target: "#view", key: "tour.office" },
  { target: "#tab-why", key: "tour.why" },
  { target: "#bottombar", key: "tour.replay" },
  { target: "#palette-btn", key: "tour.palette" },
];

let index = -1;
let lastFocus = null;

function place() {
  const root = document.getElementById("tour");
  const step = STEPS[index];
  const target = document.querySelector(step.target);
  const rect = target && target.offsetParent !== null ? target.getBoundingClientRect() : null;
  const spot = el("div", { class: "tour-spot" });
  if (rect) {
    Object.assign(spot.style, { left: `${rect.left - 6}px`, top: `${rect.top - 6}px`, width: `${rect.width + 12}px`,
      height: `${rect.height + 12}px` });
  } else spot.hidden = true;
  const back = el("button", { type: "button", class: "btn", text: t("tour.back"), disabled: index === 0 });
  const next = el("button", { type: "button", class: "btn primary",
    text: index === STEPS.length - 1 ? t("tour.done") : t("tour.next") });
  const skip = el("button", { type: "button", class: "btn ghost", text: t("tour.skip") });
  const box = el("div", { class: "tour-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "tour-title" },
    el("p", { class: "tour-step", text: t("tour.step", { n: index + 1, total: STEPS.length }) }),
    el("h3", { id: "tour-title", text: t(`${step.key}.title`) }),
    el("p", { text: t(`${step.key}.body`) }),
    el("div", { class: "tour-actions" }, skip, back, next));
  if (rect) {
    const below = rect.bottom + 230 < window.innerHeight;
    Object.assign(box.style, {
      left: `${Math.max(12, Math.min(window.innerWidth - 352, rect.left))}px`,
      top: below ? `${Math.min(window.innerHeight - 220, rect.bottom + 14)}px` : `${Math.max(12, rect.top - 214)}px`,
    });
  }
  clear(root).append(spot, box);
  back.addEventListener("click", () => go(index - 1));
  next.addEventListener("click", () => (index === STEPS.length - 1 ? endTour() : go(index + 1)));
  skip.addEventListener("click", endTour);
  next.focus();
}

function go(i) {
  index = Math.max(0, Math.min(STEPS.length - 1, i));
  place();
}

function onKey(ev) {
  if (index < 0) return;
  if (ev.key === "Escape") endTour();
  if (ev.key === "ArrowRight") go(index + 1);
  if (ev.key === "ArrowLeft") go(index - 1);
}

export function startTour() {
  lastFocus = document.activeElement;
  document.getElementById("tour").hidden = false;
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", place);
  go(0);
}

export function endTour() {
  index = -1;
  storeSet("cbx-tour", "done");
  const root = document.getElementById("tour");
  clear(root);
  root.hidden = true;
  document.removeEventListener("keydown", onKey);
  window.removeEventListener("resize", place);
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

export function maybeStartTour() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("tour") === "0" || storeGet("cbx-tour", "") === "done") return;
  setTimeout(startTour, 1200);
}
