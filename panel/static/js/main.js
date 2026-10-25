// CYBERBOX Trading Office: shell (header, tabs, router, theme, language, live stream).

import { getJSON, onStreamStatus, startStream } from "./api.js";
import { wireDrawer } from "./drawer.js";
import { language, setLanguage, t } from "./i18n.js";
import { ago, clear, el, setLocale, storeGet, storeSet } from "./util.js";
import { mountAdvisor, mountDecisions, mountHealth, mountMarkets, mountOverview, mountPerformance, mountRisk,
  mountStrategies, mountTeam, mountTrades } from "./views/dashboards.js";
import { mountOffice } from "./views/office.js";

const VIEWS = [
  ["office", mountOffice], ["overview", mountOverview], ["performance", mountPerformance], ["trades", mountTrades],
  ["team", mountTeam], ["strategies", mountStrategies], ["risk", mountRisk], ["markets", mountMarkets],
  ["advisor", mountAdvisor], ["health", mountHealth], ["decisions", mountDecisions],
];
const app = { status: null };
const $ = (id) => document.getElementById(id);
let current = null;
let currentId = null;

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  storeSet("cbx-theme", theme);
  if (current && current.theme) current.theme();
}

function renderTabs() {
  const nav = clear($("tabs"));
  for (const [id] of VIEWS) {
    const btn = el("button", { class: "tab", role: "tab", type: "button", id: `tab-${id}`, "aria-selected": id === currentId ? "true" : "false",
      "aria-controls": "view", text: t(`tab.${id}`) });
    btn.addEventListener("click", () => { window.location.hash = `#/${id}`; });
    btn.addEventListener("keydown", (ev) => {
      const ids = VIEWS.map((v) => v[0]);
      const i = ids.indexOf(id);
      if (ev.key === "ArrowRight") document.getElementById(`tab-${ids[(i + 1) % ids.length]}`).focus();
      if (ev.key === "ArrowLeft") document.getElementById(`tab-${ids[(i - 1 + ids.length) % ids.length]}`).focus();
    });
    nav.append(btn);
  }
}

function route() {
  const id = (window.location.hash.match(/^#\/(\w+)/) || [])[1] || "office";
  const entry = VIEWS.find((v) => v[0] === id) || VIEWS[0];
  if (current && current.unmount) current.unmount();
  const root = clear($("view"));
  currentId = entry[0];
  renderTabs();
  current = entry[1](root, app);
  document.title = t("app.title");
  $("view").setAttribute("aria-label", t(`tab.${currentId}`));
}

function badge(node, text, cls) {
  node.className = `badge ${cls || ""}`;
  clear(node);
  node.append(el("span", { class: "dot" }), text);
}

async function refreshStatus() {
  try {
    const s = await getJSON("/api/status");
    app.status = s;
    const kind = s.simulation ? "sim" : s.account_type === "REAL" ? "real" : s.account_type === "DEMO" ? "demo" : "";
    const acc = $("account-badge");
    acc.className = `badge ${kind ? `account-${kind}` : ""}`;
    acc.textContent = kind ? t(`account.${kind}`) : "?";
    const banner = $("banner-account");
    banner.className = `banner ${kind}`;
    banner.textContent = kind ? t(`banner.${kind}`) : "";
    banner.hidden = !kind;
    badge($("mode-badge"), `${t("overview.team")}: ${t(`mode.${(s.team || {}).effective_mode || "baseline"}`)}`, "");
    const adv = (s.advisor || {}).state || "DISABLED";
    badge($("advisor-badge"), `Claude: ${t(`advisor.${adv}`)}`, adv === "ONLINE" ? "good" : adv === "DEGRADED" || adv === "BUDGET_CAPPED"
      ? "warning" : adv.startsWith("OFFLINE") ? "critical" : "");
    const offline = $("banner-offline");
    if (!s.has_data) { offline.hidden = false; offline.textContent = t("banner.nodata"); } else if (!s.bot_alive) {
      offline.hidden = false; offline.textContent = t("banner.offline", { age: ago(s.health_age_s) });
    } else offline.hidden = true;
    $("banner-paused").hidden = !s.paused;
    $("banner-paused").textContent = t("banner.paused");
    const sessions = clear($("sessions"));
    for (const name of ["tokyo", "london", "new_york"]) {
      const open = ((s.sessions || {}).detail || {})[name] && s.sessions.detail[name].open;
      sessions.append(el("span", { class: `session-pill${open ? " open" : ""}`, text: t(`session.${name}`) }));
    }
    // The server's reference time: the wall clock, or a simulation's virtual clock.
    app.clockSkew = (s.now_utc || Date.now() / 1000) - Date.now() / 1000;
    app.serverOffset = (s.server_utc_offset_hours || 0) * 3600 + app.clockSkew;
  } catch (e) { /* header keeps its last state */ }
}

function tickClock() {
  const now = new Date(Date.now() + (app.serverOffset || 0) * 1000);
  $("clock-time").textContent = now.toISOString().slice(11, 19);
}

function init() {
  const lang = storeGet("cbx-lang", "pt-PT");
  setLanguage(lang);
  setLocale(lang);
  applyTheme(storeGet("cbx-theme", "dark"));
  $("app-sub").textContent = t("app.subtitle");
  $("clock-label").textContent = t("clock.server");
  $("theme-btn").textContent = t("theme.toggle");
  $("lang-btn").textContent = t("lang.toggle");
  $("theme-btn").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));
  $("lang-btn").addEventListener("click", () => { storeSet("cbx-lang", language() === "en" ? "pt-PT" : "en"); window.location.reload(); });
  wireDrawer();
  onStreamStatus((status) => badge($("stream-badge"), status === "live" ? "ao vivo" : status === "polling" ? "a consultar" : "a ligar",
    status === "live" ? "good" : "warning"));
  window.addEventListener("hashchange", route);
  refreshStatus().then(route);
  setInterval(refreshStatus, 5000);
  setInterval(tickClock, 1000);
  tickClock();
  startStream();
}

init();
