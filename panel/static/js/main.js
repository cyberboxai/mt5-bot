// CYBERBOX Trading Office: the shell. Top bar (account badge, KPIs, cushion, exploration
// budget, advisor lamp, session clocks, mode chip), roster sidebar, tabs and router, bottom
// bar (ticker and replay), command palette, preferences, onboarding tour and announcements.

import { getJSON, onEvent, onStreamStatus, startStream } from "./api.js";
import { openAgent, openControlModal, wireDrawer } from "./drawer.js";
import { language, reasonText, setLanguage, t } from "./i18n.js";
import { lookFor, portrait } from "./office/sprites.js";
import { openPalette, registerCommands, wirePalette } from "./palette.js";
import { apply as applySettings, onSettings, setSetting, settings } from "./settings.js";
import { maybeStartTour, startTour } from "./tour.js";
import { ago, clear, el, num, setLocale, signed, storeGet, storeSet } from "./util.js";
import { mountAdvisor, mountDecisions, mountHealth, mountMarkets, mountOverview, mountPerformance, mountRisk,
  mountStrategies, mountTeam, mountTrades } from "./views/dashboards.js";
import { mountFunnel } from "./views/funnel.js";
import { describe, mountOffice } from "./views/office.js";

const VIEWS = [
  ["office", mountOffice], ["why", mountFunnel], ["overview", mountOverview], ["performance", mountPerformance],
  ["trades", mountTrades], ["team", mountTeam], ["strategies", mountStrategies], ["risk", mountRisk],
  ["markets", mountMarkets], ["advisor", mountAdvisor], ["health", mountHealth], ["decisions", mountDecisions],
];
const CLOCKS = [["tokyo", "Asia/Tokyo"], ["london", "Europe/London"], ["new_york", "America/New_York"]];
const app = { status: null, officeApi: null, replayApi: null, roster: [], names: {} };
const $ = (id) => document.getElementById(id);
let current = null;
let currentId = null;

// ------------------------------------------------------------------ announcements

let lastAnnounce = 0;
function announce(text, urgent = false) {
  const node = $(urgent ? "announcer-urgent" : "announcer");
  if (!node || !text) return;
  const now = Date.now();
  if (!urgent && now - lastAnnounce < 4000) return; // never flood a screen reader
  lastAnnounce = now;
  node.textContent = "";
  setTimeout(() => { node.textContent = text; }, 50);
}

// ------------------------------------------------------------------ router

function renderTabs() {
  const nav = clear($("tabs"));
  for (const [id] of VIEWS) {
    const btn = el("button", { class: `tab${id === "why" ? " tab-why" : ""}`, role: "tab", type: "button", id: `tab-${id}`,
      "aria-selected": id === currentId ? "true" : "false", tabindex: id === currentId ? "0" : "-1", "aria-controls": "view",
      text: t(`tab.${id}`) });
    btn.addEventListener("click", () => { window.location.hash = `#/${id}`; });
    btn.addEventListener("keydown", (ev) => {
      const ids = VIEWS.map((v) => v[0]);
      const i = ids.indexOf(id);
      let next = null;
      if (ev.key === "ArrowRight") next = ids[(i + 1) % ids.length];
      if (ev.key === "ArrowLeft") next = ids[(i - 1 + ids.length) % ids.length];
      if (ev.key === "Home") next = ids[0];
      if (ev.key === "End") next = ids[ids.length - 1];
      if (next) { ev.preventDefault(); document.getElementById(`tab-${next}`).focus(); }
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
  document.body.dataset.view = currentId;
  renderTabs();
  current = entry[1](root, app);
  document.title = currentId === "office" ? t("app.title") : `${t(`tab.${currentId}`)} · ${t("app.title")}`;
  $("view").setAttribute("aria-label", t(`tab.${currentId}`));
  if (window.innerWidth < 900) document.body.classList.remove("roster-open");
}

// ------------------------------------------------------------------ top bar

function kpi(id, label, value, note, cls = "") {
  const node = $(id);
  clear(node);
  node.className = `kpi ${cls}`;
  node.append(el("span", { class: "kpi-label", text: label }), el("strong", { class: "kpi-value", text: value }));
  if (note) node.append(el("span", { class: "kpi-note", text: note }));
}

function renderTopbar(s) {
  const kind = s.simulation ? "sim" : s.account_type === "REAL" ? "real" : s.account_type === "DEMO" ? "demo" : "";
  const acc = $("account-badge");
  acc.className = `env-badge ${kind ? `env-${kind}` : "env-unknown"}`;
  acc.textContent = kind ? t(`account.${kind}`) : t("account.unknown");
  acc.title = kind ? t(`banner.${kind}`) : "";
  const banner = $("banner-account");
  banner.className = `banner ${kind}`;
  banner.textContent = kind === "real" ? t("banner.real") : kind === "sim" ? t("banner.sim") : "";
  banner.hidden = !(kind === "real" || kind === "sim");
  const cur = s.currency ? ` (${s.currency})` : "";
  kpi("kpi-equity", `${t("kpi.equity")}${cur}`, s.equity === null || s.equity === undefined ? "n/d" : num(s.equity, 2),
    s.drawdown_pct === null || s.drawdown_pct === undefined ? "" : t("kpi.drawdown", { v: num(s.drawdown_pct, 2) }));
  const pnl = s.pnl_today;
  kpi("kpi-pnl", t("kpi.pnl_today"), pnl === null || pnl === undefined ? "n/d" : signed(pnl, 2), "",
    pnl > 0 ? "up" : pnl < 0 ? "down" : "");
  const cap = s.capital || {};
  kpi("kpi-cushion", t("kpi.cushion"), cap.cushion === undefined || cap.cushion === null ? "n/d" : num(cap.cushion, 2),
    cap.floor ? t("kpi.floor", { v: num(cap.floor, 2) }) : "", cap.cushion !== undefined && cap.cushion <= 0 ? "down" : "");
  const ex = s.exploration || {};
  const used = (ex.day_used || 0) + (ex.open_risk || 0);
  const frac = ex.day_budget ? Math.min(1, used / ex.day_budget) : 0;
  const meter = $("kpi-explore");
  clear(meter);
  meter.className = `kpi kpi-meter${frac >= 0.9 ? " crit" : frac >= 0.7 ? " warn" : ""}`;
  const fill = el("span", { class: "mini-fill" });
  fill.style.width = `${(frac * 100).toFixed(0)}%`;
  meter.append(el("span", { class: "kpi-label", text: t("kpi.explore") }),
    el("span", { class: "mini-track", role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(frac * 100),
      "aria-label": t("kpi.explore_aria", { used: num(used, 2), budget: num(ex.day_budget || 0, 2) }) }, fill),
    el("span", { class: "kpi-note", text: ex.day_budget ? `${num(used, 2)} / ${num(ex.day_budget, 2)}` : "n/d" }));
  const mode = s.mode || "exploration";
  const chip = $("mode-chip");
  chip.className = `mode-chip mode-${mode}`;
  chip.textContent = t(`modechip.${mode}`);
  chip.title = reasonText(s.status_line) || "";
  const adv = (s.advisor || {}).state || "DISABLED";
  const lamp = $("advisor-lamp");
  const color = adv === "ONLINE" ? "green" : adv === "DEGRADED" || adv === "BUDGET_CAPPED" ? "amber" : "grey";
  lamp.className = `lamp lamp-${color}`;
  clear(lamp).append(el("span", { class: "lamp-dot", "aria-hidden": "true" }), el("span", { class: "lamp-text",
    text: `Claude: ${t(`advisor.${adv}`)}` }));
  lamp.title = reasonText((s.advisor || {}).reason) || "";
  const offline = $("banner-offline");
  if (!s.has_data) { offline.hidden = false; offline.textContent = t("banner.nodata"); }
  else if (!s.bot_alive) { offline.hidden = false; offline.textContent = t("banner.offline", { age: ago(s.health_age_s) }); }
  else offline.hidden = true;
  $("banner-paused").hidden = !s.paused;
  $("banner-paused").textContent = t("banner.paused");
  $("status-line").textContent = reasonText(s.status_line) || "";
}

function tickClocks() {
  const skew = (app.clockSkew || 0) * 1000;
  const now = new Date(Date.now() + skew);
  const server = new Date(Date.now() + (app.serverOffset || 0) * 1000);
  $("clock-server").textContent = server.toISOString().slice(11, 16);
  const detail = ((app.status || {}).sessions || {}).detail || {};
  for (const [name, zone] of CLOCKS) {
    const node = $(`clock-${name}`);
    if (!node) continue;
    let text = "--:--";
    try { text = new Intl.DateTimeFormat("pt-PT", { hour: "2-digit", minute: "2-digit", timeZone: zone }).format(now); } catch (e) { /* old engine */ }
    const open = detail[name] && detail[name].open;
    node.className = `clock-pill${open ? " open" : ""}`;
    node.textContent = `${t(`session.short.${name}`)} ${text}`;
    node.title = `${t(`session.${name}`)}: ${open ? t("session.open") : t("session.closed")}`;
    node.setAttribute("aria-label", node.title);
  }
}

let lastMode = null;
async function refreshStatus() {
  try {
    const s = await getJSON("/api/status");
    app.status = s;
    app.clockSkew = (s.now_utc || Date.now() / 1000) - Date.now() / 1000;
    app.serverOffset = (s.server_utc_offset_hours || 0) * 3600 + app.clockSkew;
    renderTopbar(s);
    if (lastMode && lastMode !== s.mode) announce(t("announce.mode", { mode: t(`modechip.${s.mode}`) }), true);
    lastMode = s.mode;
    $("app").classList.remove("loading");
  } catch (e) {
    $("banner-offline").hidden = false;
    $("banner-offline").textContent = t("banner.unreachable");
  }
}

// ------------------------------------------------------------------ roster

const STATE_CLASS = { error: "crit", resting: "off", executing: "hot", deciding: "hot", debating: "hot", analyzing: "busy",
  scanning: "busy", monitoring: "busy", reviewing: "busy" };

function renderRoster(state) {
  const list = clear($("roster-list"));
  const q = ($("roster-search").value || "").toLowerCase();
  const departments = state.departments || [];
  const byDept = {};
  for (const a of state.agents || []) (byDept[a.department] ||= []).push(a);
  for (const d of departments.length ? departments : Object.keys(byDept).map((id) => ({ id, pt: id, en: id }))) {
    const members = (byDept[d.id] || []).filter((a) => !q || `${a.name} ${a.role}`.toLowerCase().includes(q));
    if (!members.length) continue;
    const group = el("li", { class: "roster-dept" }, el("h3", { class: "dept-title" },
      el("span", { class: `dept-swatch dept-${d.id}`, "aria-hidden": "true" }), language() === "en" ? d.en : d.pt));
    const ul = el("ul", { class: "roster-members" });
    for (const a of members) {
      const look = lookFor(a);
      const pic = portrait(look, 32);
      pic.className = "portrait";
      pic.setAttribute("aria-hidden", "true");
      const stateText = `${t(`state.${a.state}`)}${a.benched ? ` (${t("roster.bench")})` : ""}`;
      const open = el("button", { type: "button", class: "agent-btn", "aria-label": `${a.name}, ${a.role}: ${stateText}${a.task ? `, ${a.task}` : ""}` },
        pic, el("span", { class: "an" }, el("strong", { text: a.name }), el("span", { class: "ar", text: a.role })),
        el("span", { class: `state-dot ${STATE_CLASS[a.state] || ""}`, title: stateText, "aria-hidden": "true" }));
      open.addEventListener("click", () => {
        if (app.officeApi) app.officeApi.follow(a.id);
        openAgent(a.id);
      });
      const follow = el("button", { type: "button", class: "follow-btn", text: "◎", title: t("roster.follow", { name: a.name }),
        "aria-label": t("roster.follow", { name: a.name }) });
      follow.addEventListener("click", () => {
        if (!app.officeApi) { window.location.hash = "#/office"; setTimeout(() => app.officeApi && app.officeApi.follow(a.id), 600); }
        else app.officeApi.follow(a.id);
      });
      ul.append(el("li", { class: "roster-item" }, open, follow));
    }
    group.append(ul);
    list.append(group);
  }
  if (!list.childElementCount) list.append(el("li", { class: "empty", text: q ? t("roster.none") : t("roster.waiting") }));
}

async function refreshRoster() {
  try {
    const state = await getJSON("/api/office/state");
    app.roster = state.agents || [];
    for (const a of app.roster) app.names[a.id] = a.name;
    renderRoster(state);
    $("roster-count").textContent = String(app.roster.length);
  } catch (e) { /* keep the last list */ }
}

// ------------------------------------------------------------------ ticker

const ticker = [];
function onTickerEvent(e) {
  if (["market.snapshot", "system.health", "opinion", "agent.state"].includes(e.topic)) return;
  const text = describe(e, app.names);
  if (!text) return;
  ticker.unshift({ e, text });
  if (ticker.length > 12) ticker.pop();
  const node = clear($("ticker-list"));
  for (const { e: ev, text: tx } of ticker.slice(0, 8)) {
    node.append(el("li", { class: `tick ${ev.severity || "info"}` }, el("time", { text: new Date(ev.ts * 1000).toISOString().slice(11, 16) }),
      el("span", { text: tx })));
  }
  if (e.topic === "order.executed") announce(t("announce.order", { text }), true);
  else if (e.severity === "critical" || e.topic === "guardian.alert") announce(text, true);
  else if (e.topic === "risk.decision" && e.payload && e.payload.allowed === false) announce(text);
}

// ------------------------------------------------------------------ preferences

function renderSettingsPanel() {
  const panel = clear($("settings-panel"));
  const s = settings();
  const group = (key, label, options) => {
    const fs = el("fieldset", { class: "setting" }, el("legend", { text: label }));
    for (const [value, text] of options) {
      const id = `set-${key}-${value}`;
      const input = el("input", { type: "radio", name: `set-${key}`, id, value, checked: s[key] === value });
      input.addEventListener("change", () => setSetting(key, value));
      fs.append(el("label", { for: id }, input, text));
    }
    return fs;
  };
  panel.append(el("h2", { class: "settings-title", text: t("settings.title") }),
    group("theme", t("settings.theme"), [["dark", t("settings.dark")], ["light", t("settings.light")]]),
    group("contrast", t("settings.contrast"), [["normal", t("settings.normal")], ["high", t("settings.high")]]),
    group("motion", t("settings.motion"), [["auto", t("settings.auto")], ["full", t("settings.full")], ["reduced", t("settings.reduced")]]),
    group("effects", t("settings.effects"), [["off", t("settings.off")], ["low", t("settings.low")], ["high", t("settings.high_fx")]]),
    group("hud", t("settings.hud"), [["off", t("settings.off")], ["on", t("settings.on")]]),
    el("p", { class: "small muted", text: t("settings.note") }));
  const close = el("button", { type: "button", class: "btn", text: t("common.close") });
  close.addEventListener("click", toggleSettings);
  panel.append(close);
}

function toggleSettings() {
  const panel = $("settings-panel");
  const open = panel.hidden;
  panel.hidden = !open;
  $("settings-btn").setAttribute("aria-expanded", open ? "true" : "false");
  if (open) { renderSettingsPanel(); panel.querySelector("input:checked")?.focus(); } else $("settings-btn").focus();
}

// ------------------------------------------------------------------ palette commands

function commands() {
  const go = t("palette.go");
  const out = VIEWS.map(([id]) => ({ group: go, label: t(`tab.${id}`), run: () => { window.location.hash = `#/${id}`; } }));
  for (const a of app.roster) {
    out.push({ group: t("palette.follow"), label: `${a.name} · ${a.role}`, keywords: a.id, run: () => {
      if (!app.officeApi) { window.location.hash = "#/office"; setTimeout(() => app.officeApi && app.officeApi.follow(a.id), 700); }
      else app.officeApi.follow(a.id);
    } });
    out.push({ group: t("palette.open_agent"), label: a.name, keywords: a.role, run: () => openAgent(a.id) });
  }
  const pref = t("palette.prefs");
  out.push(
    { group: pref, label: t("palette.theme"), run: () => setSetting("theme", settings().theme === "dark" ? "light" : "dark") },
    { group: pref, label: t("palette.contrast"), run: () => setSetting("contrast", settings().contrast === "high" ? "normal" : "high") },
    { group: pref, label: t("palette.motion"), run: () => setSetting("motion", settings().reducedMotion ? "full" : "reduced") },
    { group: pref, label: t("palette.effects"), run: () => setSetting("effects", { off: "low", low: "high", high: "off" }[settings().effects] || "high") },
    { group: pref, label: t("palette.hud"), hint: "", run: () => setSetting("hud", settings().hud === "on" ? "off" : "on") },
    { group: pref, label: t("palette.language"), run: () => { storeSet("cbx-lang", language() === "en" ? "pt-PT" : "en"); window.location.reload(); } },
    { group: t("palette.actions"), label: t("palette.replay"), run: () => {
      if (!app.replayApi) { window.location.hash = "#/office"; setTimeout(() => app.replayApi && app.replayApi.start(), 800); }
      else app.replayApi.start();
    } },
    { group: t("palette.actions"), label: t("palette.live"), run: () => app.replayApi && app.replayApi.live() },
    { group: t("palette.actions"), label: t("palette.tour"), run: startTour },
    { group: t("palette.actions"), label: t("palette.why"), run: () => { window.location.hash = "#/why"; } },
  );
  const controls = (app.status || {}).controls || {};
  if (controls.enabled) {
    for (const action of ["pause", "resume", "kill"]) {
      out.push({ group: t("palette.controls"), label: t(`control.${action}`), run: () => openControlModal(action, refreshStatus) });
    }
  }
  for (const room of ["trading_floor", "research_lab", "risk_office", "advisor_booth", "newsroom", "boardroom",
    "execution_desk", "ops_room", "analytics", "break_room"]) {
    out.push({ group: t("palette.room"), label: t(`room.${room}`), run: () => {
      if (!app.officeApi) { window.location.hash = "#/office"; setTimeout(() => app.officeApi && app.officeApi.focusRoom(room), 700); }
      else app.officeApi.focusRoom(room);
    } });
  }
  return out;
}

// ------------------------------------------------------------------ init

function init() {
  const lang = storeGet("cbx-lang", "pt-PT");
  setLanguage(lang);
  setLocale(lang);
  applySettings();
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const node of document.querySelectorAll("[data-i18n-label]")) node.setAttribute("aria-label", t(node.dataset.i18nLabel));
  for (const node of document.querySelectorAll("[data-i18n-title]")) node.title = t(node.dataset.i18nTitle);
  $("lang-btn").textContent = t("lang.toggle");
  $("theme-btn").addEventListener("click", () => setSetting("theme", settings().theme === "dark" ? "light" : "dark"));
  $("lang-btn").addEventListener("click", () => { storeSet("cbx-lang", language() === "en" ? "pt-PT" : "en"); window.location.reload(); });
  $("palette-btn").addEventListener("click", openPalette);
  $("help-btn").addEventListener("click", startTour);
  $("settings-btn").addEventListener("click", toggleSettings);
  $("roster-toggle").addEventListener("click", () => {
    const open = document.body.classList.toggle("roster-open");
    $("roster-toggle").setAttribute("aria-expanded", open ? "true" : "false");
  });
  $("roster-search").addEventListener("input", () => refreshRoster());
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !$("settings-panel").hidden) toggleSettings();
  });
  onSettings(() => { if (current && current.theme) current.theme(); });
  wireDrawer();
  wirePalette();
  registerCommands(commands);
  onStreamStatus((status) => {
    const node = $("stream-badge");
    node.className = `stream stream-${status}`;
    node.textContent = t(`stream.${status}`);
    if (status === "reconnecting") announce(t("announce.reconnecting"));
  });
  onEvent(onTickerEvent);
  window.addEventListener("hashchange", route);
  refreshStatus().then(() => { route(); maybeStartTour(); });
  refreshRoster();
  setInterval(refreshStatus, 5000);
  setInterval(refreshRoster, 5000);
  setInterval(tickClocks, 1000);
  tickClocks();
  startStream();
}

init();
