// The views that matter most for decisions: what really happened, and whether there is any edge.
// Evidence is drawn as intervals, never as bare point estimates. Long lists are windowed.

import { getJSON } from "../api.js";
import { barChart, chartCard, dataTable, lineChart, meter } from "../charts.js";
import { reasonText, t, tv } from "../i18n.js";
import { axisTime, card, clear, dateTime, el, empty, num, signed, tile } from "../util.js";

const S = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];
const SERVICE_ORDER = ["data", "signals", "allocation", "risk", "execution", "research", "ops"];
const value = (x, digits = 3) => (x === null || x === undefined ? "n/d" : num(x, digits));
const rValue = (x) => (x === null || x === undefined ? "n/d" : signed(x, 3));

function refreshing(root, title, sub, load, render, everyMs) {
  root.append(el("h2", { class: "view-title", text: title }), el("p", { class: "view-sub", text: sub }));
  const body = el("div");
  root.append(body);
  let alive = true;
  let last = null;
  const run = async () => {
    try {
      const data = await load();
      if (!alive) return;
      last = data;
      clear(body);
      render(body, data);
    } catch (e) {
      if (alive && !last) { clear(body); body.append(empty(`${t("common.nodata")} (${e.message})`)); }
    }
  };
  body.append(empty(t("common.loading")));
  run();
  const timer = everyMs ? setInterval(run, everyMs) : null;
  return { unmount() { alive = false; if (timer) clearInterval(timer); },
    theme() { if (last) { clear(body); render(body, last); } } };
}

// A credible interval on a shared axis, with zero marked: the honest picture of an estimate.
function intervalBar(lo, hi, mean, span) {
  const box = el("div", { class: "interval", role: "img", "aria-label": `${rValue(lo)} a ${rValue(hi)}` });
  if (lo === null || lo === undefined || hi === null || hi === undefined) return box;
  const pos = (x) => `${(Math.min(span, Math.max(-span, x)) + span) / (2 * span) * 100}%`;
  const zero = el("span", { class: "interval-zero" });
  zero.style.left = pos(0);
  const bar = el("span", { class: `interval-bar ${lo > 0 ? "pos" : hi < 0 ? "neg" : "mixed"}` });
  bar.style.left = pos(lo);
  bar.style.right = `calc(100% - ${pos(hi)})`;
  const dot = el("span", { class: "interval-mean" });
  dot.style.left = pos(mean ?? (lo + hi) / 2);
  box.append(zero, bar, dot);
  return box;
}

// Virtualized table: only the rows on screen exist in the DOM, however long the list is.
const ROW_HEIGHT = 40;
function windowed(rows, columns, height = 420) {
  const scroller = el("div", { class: "vt", tabindex: "0" });
  scroller.style.maxHeight = `${height}px`;
  const head = el("div", { class: "vt-row vt-head" }, columns.map((c) => el("span", { class: `vt-cell ${c.cls || ""}`, text: c.label })));
  const spacer = el("div", { class: "vt-body" });
  spacer.style.height = `${rows.length * ROW_HEIGHT}px`;
  scroller.append(head, spacer);
  const draw = () => {
    const first = Math.max(0, Math.floor(scroller.scrollTop / ROW_HEIGHT) - 4);
    const count = Math.ceil(height / ROW_HEIGHT) + 8;
    clear(spacer);
    rows.slice(first, first + count).forEach((row, i) => {
      const line = el("div", { class: "vt-row" });
      line.style.top = `${(first + i) * ROW_HEIGHT}px`;
      for (const c of columns) {
        const cell = el("span", { class: `vt-cell ${c.cls || ""}` });
        const content = c.format ? c.format(row[c.key], row) : String(row[c.key] ?? "");
        cell.append(content);
        line.append(cell);
      }
      if (row.onOpen) {
        line.classList.add("vt-click");
        line.tabIndex = 0;
        line.addEventListener("click", row.onOpen);
        line.addEventListener("keydown", (ev) => { if (ev.key === "Enter") row.onOpen(); });
      }
      spacer.append(line);
    });
  };
  scroller.addEventListener("scroll", draw, { passive: true });
  draw();
  return scroller;
}

function verdictChip(verdict) {
  const level = verdict === "edge_plausible" ? "ok" : verdict === "no_edge" ? "bad" : "warn";
  return el("span", { class: `status-chip ${level}`, text: t(`truth.verdict.${verdict}`) });
}

// ---------------------------------------------------------------------- truth

function renderGap(body, gap) {
  const interval = gap.interval ? `[${signed(gap.interval[0], 3)}; ${signed(gap.interval[1], 3)}] R` : t("truth.no_pairs");
  const status = t(`truth.gap.${gap.status || "insufficient"}`);
  const tiles = el("div", { class: "grid tiles" },
    tile(t("truth.gap_title"), gap.gap_r === null || gap.gap_r === undefined ? "n/d" : `${signed(gap.gap_r, 3)} R`,
      `${status} · ${t("truth.pairs", { n: num(gap.n || 0, 0) })}`, gap.status === "diverged" ? "warn" : ""),
    tile(t("truth.gap_interval"), interval, t("truth.gap_note")),
    tile(t("truth.fill_rate"), gap.fill_rate === null || gap.fill_rate === undefined ? "n/d" : `${num(gap.fill_rate * 100, 1)}%`),
    tile(t("truth.slip_gap"), gap.slippage_gap_points === null || gap.slippage_gap_points === undefined ? "n/d"
      : `${signed(gap.slippage_gap_points, 2)} pts`));
  body.append(el("section", { class: "tile-section" }, el("h3", { class: "tile-section-title", text: t("truth.sec_gap") }), tiles));
  for (const reason of gap.reasons || []) body.append(el("p", { class: "banner warn", text: reason }));
  const hist = gap.history || [];
  if (hist.length >= 2) {
    const span = hist[hist.length - 1].ts - hist[0].ts;
    body.append(chartCard({ title: t("truth.gap_history"), rows: hist, csvName: "reality_gap.csv", n: hist.length, render: (n) =>
      lineChart(n, { series: [{ name: t("truth.gap_title"), color: S[0], points: hist.map((p) => ({ x: p.ts, y: p.gap_r })) }],
        zeroLine: true, xFormat: (v) => axisTime(v, span), xTip: (v) => dateTime(v), yFormat: (v) => `${signed(v, 2)} R` }) }));
  }
}

function renderArms(body, arms, trials) {
  const shown = arms.filter((a) => a.tier !== "T0" || a.trades);
  const list = shown.length ? shown : arms;
  const extent = Math.max(0.25, ...list.flatMap((a) => [Math.abs(a.lo_r ?? 0), Math.abs(a.hi_r ?? 0)]));
  const columns = [
    { key: "arm", label: t("truth.col_arm"), cls: "vt-wide" },
    { key: "tier", label: t("truth.col_tier"), cls: "vt-tight" },
    { key: "lo_r", label: t("truth.col_interval"), cls: "vt-wide", format: (_v, r) => el("span", { class: "interval-cell" },
      intervalBar(r.lo_r, r.hi_r, r.mean_r, extent), el("small", { text: `[${rValue(r.lo_r)}; ${rValue(r.hi_r)}]` })) },
    { key: "cost_to_edge", label: t("truth.col_cost"), cls: "vt-num", format: (v) => value(v, 2) },
    { key: "dsr", label: t("truth.col_dsr"), cls: "vt-num", format: (v) => value(v, 3) },
    { key: "pbo", label: t("truth.col_pbo"), cls: "vt-num", format: (v) => value(v, 2) },
    { key: "verdict", label: t("truth.col_verdict"), format: (v) => verdictChip(v) },
  ];
  body.append(card(t("truth.arms_title"), t("truth.arms_sub", { trials: num(trials || 0, 0) }),
    list.length ? windowed(list, columns) : empty(t("truth.no_arms"))));
}

function renderExecution(grid, ex) {
  const rows = (ex.sessions || []).map((s) => ({ [t("truth.col_session")]: s.session, [t("truth.col_orders")]: s.orders,
    [t("truth.col_fills")]: s.fills, requotes: s.requotes, [t("truth.col_rejects")]: s.rejects,
    [t("truth.col_slip")]: value(s.slippage, 2), [t("truth.col_latency")]: value(s.latency, 0), spread: value(s.spread, 1) }));
  const states = Object.entries(ex.order_states || {}).map(([k, n]) => ({ label: k, value: n }));
  grid.append(card(t("truth.exec_title"), t("truth.exec_sub"), rows.length ? dataTable(rows) : empty(t("truth.no_orders"))));
  const hours = ex.hours || [];
  if (hours.length) {
    grid.append(chartCard({ title: t("truth.slip_by_hour"), rows: hours, csvName: "slippage_by_hour.csv", n: hours.length,
      render: (n) => barChart(n, { bars: hours.map((h) => ({ label: String(h.hour).padStart(2, "0"), value: h.slippage ?? 0 })),
        yFormat: (v) => num(v, 2) }) }));
  }
  if (states.length) {
    grid.append(card(t("truth.states_title"), t("truth.states_sub"), dataTable(states.map((s) => ({ [t("truth.col_state")]: s.label,
      [t("truth.col_orders")]: s.value })))));
  }
}

function renderTail(grid, tail) {
  const stress = tail.stress;
  const tiles = el("div", { class: "grid tiles" },
    tile(t("truth.margin"), tail.margin_level ? `${num(tail.margin_level, 0)}%` : "n/d", t("truth.margin_note")),
    tile(t("truth.ladder"), `x${num(tail.ladder ?? 1, 2)}`, t("truth.ladder_note")),
    tile(t("truth.n_eff"), value((tail.portfolio || {}).n_eff, 2), t("truth.n_eff_note")),
    tile(t("truth.cvar"), stress && stress.cvar_pct !== null && stress.cvar_pct !== undefined ? `${num(stress.cvar_pct, 2)}%` : "n/d",
      t("truth.cvar_note")));
  const rows = stress ? (stress.scenarios || []).map((s) => ({ [t("truth.col_scenario")]: t(`stress.${s.name}`),
    [t("truth.col_loss")]: `${num(s.loss_pct, 2)}%`, [t("truth.col_margin")]: s.margin_level ? `${num(s.margin_level, 0)}%` : "n/d",
    [t("truth.col_stopout")]: s.stopped_out })) : [];
  grid.append(card(t("truth.tail_title"), stress ? t("truth.stress_when", { at: dateTime(stress.ts) }) : t("truth.no_stress"),
    tiles, rows.length ? dataTable(rows) : empty(t("truth.no_book")),
    stress && stress.breached ? el("p", { class: "banner warn", text: t("truth.stress_breached") }) : null));
}

export function mountTruth(root) {
  return refreshing(root, t("tab.truth"), t("truth.sub"), () => getJSON("/api/truth"), (body, data) => {
    if (data.promotions_frozen) body.append(el("p", { class: "banner warn", text: t("truth.frozen", { why: data.promotions_frozen }) }));
    renderGap(body, data.reality_gap || {});
    renderArms(body, data.arms || [], data.trials);
    const grid = el("div", { class: "grid cols-2" });
    renderExecution(grid, data.execution || {});
    renderTail(grid, data.tail || {});
    body.append(grid);
  }, 20000);
}

// --------------------------------------------------------------- time machine

function stepCard(step, index, active) {
  const node = el("li", { class: `tm-step tm-${step.service}${active ? " active" : ""}${step.outcome === "block" ? " blocked" : ""}` });
  node.append(el("span", { class: "tm-index", text: String(index + 1) }),
    el("div", { class: "tm-text" },
      el("strong", { text: t(`service.${step.service}`) }),
      el("span", { class: "tm-kind", text: step.kind.startsWith("funnel.") ? reasonText(step.title) : tv("tm.kind", step.kind) }),
      el("time", { text: dateTime(step.ts, true) })));
  return node;
}

function renderDecision(host, d) {
  clear(host);
  const steps = d.steps || [];
  let index = 0;
  const list = el("ol", { class: "tm-steps" });
  const detail = el("pre", { class: "tm-detail", tabindex: "0" });
  const position = el("span", { class: "tm-pos" });
  const show = () => {
    clear(list);
    steps.forEach((s, i) => {
      const node = stepCard(s, i, i === index);
      node.addEventListener("click", () => { index = i; show(); });
      list.append(node);
    });
    position.textContent = `${index + 1} / ${steps.length}`;
    detail.textContent = steps.length ? JSON.stringify(steps[index].data, null, 2) : "";
  };
  const go = (delta) => { index = Math.min(steps.length - 1, Math.max(0, index + delta)); show(); };
  const prev = el("button", { class: "btn", type: "button", text: t("tm.prev") });
  const next = el("button", { class: "btn", type: "button", text: t("tm.next") });
  prev.addEventListener("click", () => go(-1));
  next.addEventListener("click", () => go(1));
  const stamp = d.stamp || {};
  host.append(el("div", { class: "tm-head" }, el("strong", { text: d.id }),
    el("span", { class: "muted small", text: t("tm.stamp", { code: stamp.code || "n/d", config: stamp.config || "n/d",
      data: stamp.data || "n/d", model: stamp.model || "n/d" }) })),
  el("div", { class: "tm-controls" }, prev, position, next),
  el("div", { class: "grid cols-2" }, card(t("tm.steps"), "", list), card(t("tm.detail"), "", detail)));
  const orders = (d.orders || []).map((o) => ({ [t("truth.col_state")]: o.state, lado: o.side, volume: o.volume, preenchido: o.filled_volume,
    preço: o.fill_price ?? "n/d", ticket: o.position_ticket ?? o.ticket ?? "n/d", retcode: o.retcode ?? "n/d",
    [t("tm.protected")]: o.protected ? t("common.yes") : t("common.no") }));
  const outcome = (d.outcome || []).map((o) => ({ modo: o.mode, lado: o.side, entrada: o.entry, saída: o.exit ?? "n/d",
    "P&L": o.pnl === null ? "n/d" : signed(o.pnl, 2), R: o.r === null ? "n/d" : signed(o.r, 2), motivo: o.exit_reason ?? o.status }));
  host.append(el("div", { class: "grid cols-2" }, card(t("tm.orders"), "", orders.length ? dataTable(orders) : empty(t("tm.no_orders"))),
    card(t("tm.outcome"), t("tm.outcome_sub"), outcome.length ? dataTable(outcome) : empty(t("tm.no_outcome")))));
  show();
}

export function mountTimeMachine(root) {
  const detail = el("div");
  const open = async (id) => {
    clear(detail);
    detail.append(empty(t("common.loading")));
    try {
      renderDecision(detail, await getJSON(`/api/timemachine/${encodeURIComponent(id)}`));
      window.history.replaceState(null, "", `#/timemachine/${encodeURIComponent(id)}`);
    } catch (e) { clear(detail); detail.append(empty(e.message)); }
  };
  const view = refreshing(root, t("tab.timemachine"), t("tm.sub"), () => getJSON("/api/timemachine?limit=500"), (body, data) => {
    const rows = (data.decisions || []).map((d) => ({ ...d, onOpen: () => open(d.id) }));
    const columns = [{ key: "id", label: t("tm.col_decision"), cls: "vt-wide" }, { key: "ts", label: t("tm.col_when"), format: (v) => dateTime(v) },
      { key: "events", label: t("tm.col_events"), cls: "vt-num" }];
    body.append(card(t("tm.list"), t("tm.list_sub"), rows.length ? windowed(rows, columns, 300) : empty(t("tm.none"))), detail);
    const wanted = (window.location.hash.match(/^#\/timemachine\/(.+)$/) || [])[1];
    if (wanted && !detail.childNodes.length) open(decodeURIComponent(wanted));
  }, 0);
  return view;
}

// --------------------------------------------------------------- project health

export function projectCard(p) {
  const interval = p.ci_low === null || p.ci_low === undefined ? "n/d" : `[${signed(p.ci_low, 2)}; ${signed(p.ci_high, 2)}]`;
  const next = p.next_review ? dateTime(p.next_review) : t("project.no_review");
  return card(t("project.title"), t("project.sub"),
    el("div", { class: "project-verdict" }, verdictChip(p.verdict === "edge_plausible" ? "edge_plausible"
      : p.verdict === "no_edge" ? "no_edge" : "insufficient"), el("strong", { text: t(`project.verdict.${p.verdict}`) })),
    meter({ label: t("project.progress"), value: (p.progress || 0) * 100, max: 100,
      text: t("project.progress_text", { pct: num((p.progress || 0) * 100, 0), n: num(p.n || 0, 0), trades: num(p.horizon_trades || 200, 0),
        days: num(p.horizon_days || 91, 0) }) }),
    el("p", { class: "small", text: t("project.expectancy", { mean: p.mean === null || p.mean === undefined ? "n/d" : signed(p.mean, 2), interval }) }),
    el("p", { class: "small muted", text: t("project.next", { at: next }) }),
    p.research_only && p.research_only.active ? el("p", { class: "banner warn", text: t("project.research_only") }) : null,
    el("p", { class: "small muted", text: t("project.no_promise") }));
}

export function servicesCard(p) {
  const services = p.services || {};
  const rows = SERVICE_ORDER.filter((k) => services[k]).map((k) => ({ [t("project.col_service")]: t(`service.${k}`),
    [t("project.col_events")]: services[k].events, [t("project.col_agents")]: services[k].agents.length }));
  const comps = (p.components || []).map((c) => ({ [t("project.col_component")]: tv("component", c.id),
    [t("project.col_status")]: tv("component.status", c.status), [t("project.col_days")]: `${num(c.days_observed, 0)} / ${c.evaluation_days}` }));
  return card(t("project.services"), t("project.services_sub"), rows.length ? dataTable(rows) : empty(t("common.nodata")),
    comps.length ? dataTable(comps) : null);
}
