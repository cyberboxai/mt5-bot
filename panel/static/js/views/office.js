// Office view: the live scene (camera, minimap, follow), the event feed with filters and the
// 24-hour replay, whose scrubber lives in the shell's bottom bar while this view is open.

import { getJSON, onEvent, seedLastEventId } from "../api.js";
import { openAgent, openDossier } from "../drawer.js";
import { language, t } from "../i18n.js";
import { createOffice } from "../office/engine.js";
import { ROOMS } from "../office/map.js";
import { onSettings, settings } from "../settings.js";
import { clear, dateTime, el, num, signed, timeOnly } from "../util.js";

const QUIET = new Set(["market.snapshot", "system.health", "opinion"]);
const MAX_FEED = 200;
const RANK = { debug: 0, info: 1, notice: 2, warning: 3, critical: 4 };

export function describe(e, names) {
  const p = e.payload || {};
  const who = names[e.agent] || e.agent || "";
  switch (e.topic) {
    case "agent.message": return `${who}: ${p.text}`;
    case "agent.state": return p.message ? `${who}: ${p.message}` : null;
    case "agent.error": return `${who}: erro (${p.error})`;
    case "agent.benched": return `${who} foi para o banco (${p.reason})`;
    case "agent.restored": return `${who} regressou (${p.reason})`;
    case "decision.started": return `Reunião: ${p.side} ${e.symbol} proposto por ${p.strategy}`;
    case "decision.finished": return p.action === "enter" ? `Decisão: entrada ${e.symbol} x${num(p.multiplier, 2)}`
      : `Decisão: sem trade em ${e.symbol} (${(p.reasons || [])[0] || ""})`;
    case "risk.decision": return p.allowed ? `Risco: aprovado (${p.stage})` : `Risco: VETO, ${p.reason}`;
    case "order.executed": return `Ordem ${p.side} ${p.volume} ${e.symbol} com SL ${p.sl} e TP ${p.tp}${p.dry_run ? " (simulação a seco)" : ""}`;
    case "order.failed": return `Ordem não enviada: ${p.reason}`;
    case "trade.closed": return `Fechado ${e.symbol}: ${signed(p.r, 2)}R`;
    case "advisor.state": return `Conselheiro: ${p.old} -> ${p.new} (${p.reason})`;
    case "advisor.verdict": return p.available ? `Conselheiro: ${p.verdict} x${num(p.multiplier, 2)}` : `Conselheiro indisponível: ${p.reason}`;
    case "guardian.alert": return `Guardião: ${p.action}: ${p.detail}`;
    case "team.mode": return `Modo da equipa: ${p.effective} (${p.reason})`;
    case "control.action": return `Acção de controlo: ${p.action} (${p.detail})`;
    case "market.regime": return `${e.symbol}: regime ${p.regime}`;
    case "position.managed": return `Gestão de posição: ${p.action} (${p.detail})`;
    case "arm.tier": return `Braço ${p.arm || ""}: ${p.old || ""} -> ${p.new || ""} (${p.reason || ""})`;
    case "flow.adjustment": return `Doutor de Fluxo: ${p.knob} ${p.old} -> ${p.new} (${p.reason || ""})`;
    case "system.alert": return p.text;
    default: return null;
  }
}

export function mountOffice(root, app) {
  const stage = el("div", { class: "office-stage", id: "office-stage" });
  const feed = el("div", { class: "feed", role: "log", "aria-live": "off", "aria-label": t("office.feed") });
  const agentSel = el("select", { "aria-label": t("office.filter_agent") }, el("option", { value: "", text: t("office.all") }));
  const symbolSel = el("select", { "aria-label": t("office.filter_symbol") }, el("option", { value: "", text: t("office.all") }));
  const sevSel = el("select", { "aria-label": t("office.filter_severity") },
    el("option", { value: "", text: t("office.all") }), el("option", { value: "notice", text: t("office.sev_notice") }),
    el("option", { value: "warning", text: t("office.sev_warning") }));
  const roomSel = el("select", { "aria-label": t("office.go_room") }, el("option", { value: "", text: t("office.go_room") }),
    Object.keys(ROOMS).map((id) => el("option", { value: id, text: t(`room.${id}`) })));
  const btn = (text, label, fn) => {
    const b = el("button", { type: "button", class: "icon-btn", text, "aria-label": label, title: label });
    b.addEventListener("click", fn);
    return b;
  };
  const followLabel = el("span", { class: "follow-label small", "aria-live": "polite" });
  const toolbar = el("div", { class: "office-toolbar", role: "toolbar", "aria-label": t("office.toolbar") },
    btn("−", t("office.zoom_out"), () => office.zoomBy(0.8)), btn("+", t("office.zoom_in"), () => office.zoomBy(1.25)),
    btn("⤢", t("office.fit"), () => office.resetView()), roomSel, followLabel);
  const feedPanel = el("aside", { class: "office-feed", "aria-label": t("office.feed") },
    el("h3", { text: t("office.feed") }), el("div", { class: "filters" }, agentSel, symbolSel, sevSel), feed);
  root.append(el("h2", { class: "view-title sr-only", text: t("office.title") }),
    el("div", { class: "office-layout" }, el("div", { class: "office-main" }, toolbar, stage,
      el("p", { class: "office-help small muted", text: t("office.help") })), feedPanel));

  const names = {};
  const office = createOffice(stage, {
    t, lang: language, onAgent: (id) => { openAgent(id); },
    onDossier: openDossier, settings: { effects: settings().effects, reducedMotion: settings().reducedMotion,
      hud: settings().hud === "on" },
  });
  const offSettings = onSettings((s) => office.setSettings({ effects: s.effects, reducedMotion: s.reducedMotion,
    hud: s.hud === "on" }));
  roomSel.addEventListener("change", () => { if (roomSel.value) office.focusRoom(roomSel.value); roomSel.value = ""; });

  let feedEvents = [];
  let replay = null;
  let roster = [];

  function showFollow(id) {
    followLabel.textContent = id ? t("office.following", { name: names[id] || id }) : "";
  }

  app.officeApi = {
    follow(id) { const f = office.follow(id); showFollow(f); return f; },
    focusRoom: (id) => office.focusRoom(id),
    perf: () => office.perf(),
  };

  function renderFeed() {
    clear(feed);
    const minSev = RANK[sevSel.value] || 0;
    const rows = feedEvents.filter((e) => (!agentSel.value || e.agent === agentSel.value)
      && (!symbolSel.value || e.symbol === symbolSel.value) && (RANK[e.severity] || 0) >= minSev);
    if (!rows.length) { feed.append(el("p", { class: "empty", text: replay ? t("office.feed_empty_replay") : t("office.feed_empty") })); return; }
    for (const e of rows.slice(-80).reverse()) {
      const text = describe(e, names);
      if (!text) continue;
      const item = el("div", { class: `feed-item ${e.severity}` }, el("span", { class: "t", text: timeOnly(e.ts) }),
        el("span", { class: "msg", text }));
      if (e.correlation_id && e.topic.startsWith("decision")) {
        item.classList.add("clickable");
        item.tabIndex = 0;
        item.setAttribute("role", "button");
        const open = () => openDossier(e.correlation_id);
        item.addEventListener("click", open);
        item.addEventListener("keydown", (ev) => { if (ev.key === "Enter") open(); });
      }
      feed.append(item);
    }
  }

  function addFeed(e) {
    if (QUIET.has(e.topic) && !(e.severity === "warning" || e.severity === "critical")) return;
    if (!describe(e, names)) return;
    feedEvents.push(e);
    if (feedEvents.length > MAX_FEED) feedEvents = feedEvents.slice(-MAX_FEED);
    if (e.symbol && ![...symbolSel.options].some((o) => o.value === e.symbol)) {
      symbolSel.append(el("option", { value: e.symbol, text: e.symbol }));
    }
  }

  async function refresh() {
    if (replay) return;
    try {
      const state = await getJSON("/api/office/state");
      if (!roster.length && state.agents && state.agents.length) {
        roster = state.agents;
        for (const a of state.agents) {
          names[a.id] = a.name;
          agentSel.append(el("option", { value: a.id, text: `${a.name} (${a.role})` }));
        }
        seedLastEventId(state.last_event_id || 0);
        (state.recent || []).forEach(addFeed);
        renderFeed();
      }
      office.applySnapshot(state);
      office.setSessions(app.status && app.status.sessions);
    } catch (e) { /* keep the last frame */ }
  }

  // Stream events are queued and applied in batches (4 per second): a busy bot can emit
  // hundreds of events per second and the page must stay responsive.
  let queue = [];
  const MAX_QUEUE = 2000;
  const offEvent = onEvent((e) => {
    if (replay) return;
    queue.push(e);
    if (queue.length > MAX_QUEUE) queue = queue.filter((x) => !QUIET.has(x.topic)).slice(-MAX_QUEUE);
  });
  const flush = setInterval(() => {
    if (!queue.length) return;
    const batch = queue;
    queue = [];
    for (const e of batch) { office.applyEvent(e); addFeed(e); }
    renderFeed();
  }, 250);

  // ---------------------------------------------------------------- replay (bottom bar)

  const slot = document.getElementById("replay-slot");
  const slider = el("input", { type: "range", min: 0, max: 86400, step: 60, value: 86400, class: "scrubber",
    "aria-label": t("office.replay"), "aria-valuetext": t("office.live") });
  const playBtn = el("button", { class: "btn", type: "button", text: t("office.play") });
  const speedSel = el("select", { "aria-label": t("office.speed") }, ["10", "60", "300", "900"].map((s) =>
    el("option", { value: s, text: `${s}x`, selected: s === "60" })));
  const liveBtn = el("button", { class: "btn live on", type: "button", "aria-pressed": "true", text: t("office.live") });
  const replayLabel = el("span", { class: "replay-label small" });
  clear(slot).append(el("span", { class: "replay-title", text: t("office.replay_short") }), slider, playBtn, speedSel, liveBtn,
    replayLabel);

  async function enterReplay() {
    const now = Date.now() / 1000 + (app.clockSkew || 0);
    const data = await getJSON(`/api/replay?from=${Math.floor(now - 86400)}&to=${Math.floor(now)}`);
    replay = { events: data.events, start: now - 86400, time: now - 86400 + Number(slider.value), idx: 0, playing: false };
    liveBtn.classList.remove("on");
    liveBtn.setAttribute("aria-pressed", "false");
    document.documentElement.dataset.replay = "on";
    seek(replay.time);
  }

  function seek(time) {
    if (!replay) return;
    office.resetForReplay(roster);
    feedEvents = [];
    replay.time = time;
    let i = 0;
    const ev = replay.events;
    for (; i < ev.length && ev[i].ts <= time; i += 1) {
      const recent = time - ev[i].ts < 300;
      if (recent || ev[i].topic === "agent.benched" || ev[i].topic === "agent.restored" || ev[i].topic === "advisor.state") {
        office.applyEvent(ev[i]);
      }
      if (recent) addFeed(ev[i]);
    }
    replay.idx = i;
    renderFeed();
    replayLabel.textContent = `${dateTime(time)} · ${ev.length} ${t("office.events")}`;
    slider.setAttribute("aria-valuetext", dateTime(time));
  }

  const timer = setInterval(() => {
    if (!replay || !replay.playing) return;
    replay.time += 0.1 * Number(speedSel.value);
    const ev = replay.events;
    let changed = false;
    while (replay.idx < ev.length && ev[replay.idx].ts <= replay.time) {
      office.applyEvent(ev[replay.idx]);
      addFeed(ev[replay.idx]);
      replay.idx += 1;
      changed = true;
    }
    if (changed) renderFeed();
    slider.value = String(Math.min(86400, Math.max(0, replay.time - replay.start)));
    replayLabel.textContent = `${dateTime(replay.time)} · x${speedSel.value}`;
    if (replay.time >= replay.start + 86400) { replay.playing = false; playBtn.textContent = t("office.play"); }
  }, 100);

  function goLive() {
    replay = null;
    slider.value = "86400";
    slider.setAttribute("aria-valuetext", t("office.live"));
    playBtn.textContent = t("office.play");
    replayLabel.textContent = "";
    liveBtn.classList.add("on");
    liveBtn.setAttribute("aria-pressed", "true");
    delete document.documentElement.dataset.replay;
    feedEvents = [];
    office.resetForReplay(roster);
    refresh();
  }

  slider.addEventListener("change", async () => {
    if (!replay) await enterReplay(); else seek(replay.start + Number(slider.value));
  });
  playBtn.addEventListener("click", async () => {
    if (!replay) await enterReplay();
    replay.playing = !replay.playing;
    playBtn.textContent = replay.playing ? t("office.pause") : t("office.play");
  });
  liveBtn.addEventListener("click", goLive);
  app.replayApi = { start: async () => { if (!replay) await enterReplay(); replay.playing = true; playBtn.textContent = t("office.pause"); },
    live: goLive };
  for (const sel of [agentSel, symbolSel, sevSel]) sel.addEventListener("change", renderFeed);

  refresh();
  const poll = setInterval(refresh, 5000);
  return {
    unmount() {
      clearInterval(poll); clearInterval(timer); clearInterval(flush); offEvent(); offSettings(); office.destroy();
      delete document.documentElement.dataset.replay;
      app.officeApi = null;
      app.replayApi = null;
      const hint = el("button", { type: "button", class: "btn ghost", text: t("office.replay_hint") });
      hint.addEventListener("click", () => { window.location.hash = "#/office"; });
      clear(slot).append(hint);
    },
    theme() { office.refreshTheme(); },
  };
}
