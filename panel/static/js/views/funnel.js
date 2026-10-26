// "Porque não estou a negociar?": the decision funnel in plain language. Where signals stop,
// what each filter was worth (the shadow outcome of the signals it blocked), what the Flow
// Doctor is doing about it, and what the account can afford.

import { getJSON } from "../api.js";
import { chartCard, dataTable, lineChart, meter } from "../charts.js";
import { reasonText, t } from "../i18n.js";
import { card, clear, dateTime, el, empty, num, signed, timeOnly } from "../util.js";

const HOURS = [6, 24, 72, 168];

function funnelBars(stages) {
  const max = Math.max(1, ...stages.map((s) => s.count || 0));
  return el("ol", { class: "funnel" }, stages.map((s) => {
    const fill = el("span", { class: `funnel-fill stage-${s.stage}` });
    fill.style.width = `${Math.max(1.5, ((s.count || 0) / max) * 100).toFixed(1)}%`;
    return el("li", { class: "funnel-row" }, el("span", { class: "funnel-label", text: t(`why.stage.${s.stage}`) }),
      el("span", { class: "funnel-track" }, fill), el("strong", { class: "funnel-count", text: num(s.count || 0, 0) }));
  }));
}

function headline(d) {
  const rep = d.report || {};
  if (!rep.available) return t("why.no_data");
  const top = (rep.top_blockers || [])[0];
  if (rep.orders > 0 && (!top || top.share < 0.5)) return t("why.trading", { orders: rep.orders, hours: d.hours });
  if (!rep.signals) return t("why.no_signals", { bars: num(rep.bars_evaluated, 0), hours: d.hours });
  if (top) return t("why.blocked_by", { reason: top.description, share: Math.round(top.share * 100), n: top.count });
  return t("why.signals_no_orders", { signals: rep.signals });
}

function verdictFor(cf) {
  if (!cf || !cf.n) return { text: t("why.cf_none"), cls: "" };
  if (cf.n < 10) return { text: t("why.cf_few", { n: cf.n, r: signed(cf.virtual_mean_r, 2) }), cls: "" };
  if (cf.virtual_mean_r < -0.05) return { text: t("why.cf_protects", { n: cf.n, r: signed(cf.virtual_mean_r, 2) }), cls: "good" };
  if (cf.virtual_mean_r > 0.05) return { text: t("why.cf_costs", { n: cf.n, r: signed(cf.virtual_mean_r, 2) }), cls: "warning" };
  return { text: t("why.cf_neutral", { n: cf.n, r: signed(cf.virtual_mean_r, 2) }), cls: "" };
}

export function mountFunnel(root, app) {
  let hours = 24;
  let alive = true;
  let last = null;
  const picker = el("div", { class: "segmented", role: "radiogroup", "aria-label": t("why.window") });
  const body = el("div");
  root.append(el("h2", { class: "view-title", text: t("tab.why") }), el("p", { class: "view-sub", text: t("why.sub") }),
    picker, body);

  function renderPicker() {
    clear(picker);
    for (const h of HOURS) {
      const b = el("button", { type: "button", role: "radio", "aria-checked": h === hours ? "true" : "false",
        class: h === hours ? "on" : "", text: h < 48 ? `${h} h` : `${h / 24} d` });
      b.addEventListener("click", () => { hours = h; renderPicker(); load(); });
      picker.append(b);
    }
  }

  function render(d) {
    clear(body);
    const rep = d.report || {};
    const status = app.status || {};
    const hero = el("section", { class: "card why-hero" },
      el("p", { class: "eyebrow", text: t(`modechip.${d.mode || "exploration"}`) }),
      el("h3", { class: "why-headline", text: headline(d) }),
      el("p", { class: "why-status", text: reasonText(d.status_line) || t("why.status_unknown") }),
      d.entry_block ? el("p", { class: "why-block", text: t("why.entry_block", { reason: reasonText(d.entry_block) }) }) : null,
      el("p", { class: "small muted", text: t("why.cli") }));
    body.append(hero);
    if (!rep.available) { body.append(empty(t("why.no_data_detail"))); return; }
    const grid = el("div", { class: "grid cols-2" });
    grid.append(card(t("why.funnel"), t("why.funnel_sub", { hours: d.hours }), funnelBars(d.stages || [])));

    const cfs = d.counterfactuals || {};
    const blockers = (rep.top_blockers || []).map((b) => {
      const v = verdictFor(cfs[b.reason]);
      return { code: b.reason, what: b.description, n: b.count, share: `${Math.round(b.share * 100)}%`,
        stage: t(`why.stagename.${b.stage}`) || b.stage, knob: b.knob ? t("why.knob_yes") : t("why.knob_no"), value: v.text };
    });
    grid.append(card(t("why.blockers"), t("why.blockers_sub"), blockers.length ? dataTable(blockers, [
      { key: "what", label: t("why.col.what"), wrap: true }, { key: "n", label: t("why.col.n"), num: true },
      { key: "share", label: t("why.col.share"), num: true }, { key: "stage", label: t("why.col.stage") },
      { key: "knob", label: t("why.col.knob") }, { key: "value", label: t("why.col.value"), wrap: true },
      { key: "code", label: t("why.col.code") }]) : empty(t("why.no_blocks"))));

    const flow = d.flow || {};
    const knobs = (flow.knobs || []).filter((k) => k.value !== k.default);
    const flowCard = card(t("why.flow"), reasonText(flow.status_text) || t("why.flow_idle"));
    if (knobs.length) {
      flowCard.append(dataTable(knobs.map((k) => ({ ajuste: k.label, valor: k.value, base: k.default, limite: k.ceiling })), [
        { key: "ajuste", label: t("why.col.knob_name"), wrap: true }, { key: "valor", label: t("why.col.now"), num: true },
        { key: "base", label: t("why.col.base"), num: true }, { key: "limite", label: t("why.col.bound"), num: true }]));
    } else flowCard.append(el("p", { class: "small muted", text: t("why.flow_none") }));
    const adjustments = (flow.adjustments || []).slice(0, 6).map((a) => ({ quando: dateTime(a.ts), acção: a.direction,
      ajuste: a.knob, de: a.old_value, para: a.new_value, porquê: a.reason }));
    if (adjustments.length) flowCard.append(dataTable(adjustments));
    flowCard.append(el("p", { class: "small muted", text: t("why.flow_bounds") }));
    grid.append(flowCard);

    const ex = d.exploration || {};
    const tiers = d.tiers || {};
    const budget = card(t("why.budget"), t("why.budget_sub"));
    if (ex.day_budget !== undefined) {
      budget.append(meter({ label: t("why.budget_day"), value: (ex.day_used || 0) + (ex.open_risk || 0), max: ex.day_budget || 1,
        text: `${num((ex.day_used || 0) + (ex.open_risk || 0), 2)} / ${num(ex.day_budget, 2)} ${status.currency || ""}` }),
      meter({ label: t("why.budget_week"), value: (ex.week_used || 0) + (ex.open_risk || 0), max: ex.week_budget || 1,
        text: `${num((ex.week_used || 0) + (ex.open_risk || 0), 2)} / ${num(ex.week_budget, 2)} ${status.currency || ""}` }));
    } else budget.append(empty(t("common.nodata")));
    budget.append(el("div", { class: "tier-strip" }, ["T0", "T1", "T2", "T3"].map((k) =>
      el("span", { class: `tier-pill tier-${k}` }, el("strong", { text: num(tiers[k] || 0, 0) }), ` ${t(`tier.${k}`)}`))));
    grid.append(budget);

    const hourly = d.hourly || [];
    grid.append(chartCard({ title: t("why.hourly"), rows: hourly.map((h) => ({ hora: dateTime(h.ts), sinais: h.signals,
      bloqueados: h.blocked, ordens: h.orders })), csvName: "funnel_hourly.csv", n: hourly.length, render: (n) =>
      lineChart(n, { series: [
        { name: t("why.stage.signals"), color: "var(--series-1)", points: hourly.map((h) => ({ x: h.ts, y: h.signals || 0 })) },
        { name: t("why.stage.blocked"), color: "var(--series-2)", points: hourly.map((h) => ({ x: h.ts, y: h.blocked || 0 })) },
        { name: t("why.stage.orders"), color: "var(--series-3)", points: hourly.map((h) => ({ x: h.ts, y: h.orders || 0 })) }],
      xFormat: timeOnly, yFormat: (v) => num(v, 0) }) }));

    const aff = ((d.affordability || {}).rows || []).filter((r) => r.tier === "T1").slice(0, 24);
    grid.append(card(t("why.afford"), t("why.afford_sub"), aff.length ? dataTable(aff.map((r) => ({
      símbolo: r.symbol, tf: r.timeframe, "lote mín. arrisca": `${num(r.min_lot_risk_pct, 3)}%`,
      viável: r.feasible ? t("common.yes") : t("common.no"), "capital mínimo": num(r.min_equity, 0) }))) : empty(t("common.nodata"))));
    body.append(grid);
  }

  async function load() {
    try {
      const d = await getJSON(`/api/funnel?hours=${hours}`);
      if (!alive) return;
      last = d;
      render(d);
    } catch (e) {
      if (alive && !last) { clear(body); body.append(empty(`${t("common.load_error")} (${e.message})`)); }
    }
  }

  renderPicker();
  body.append(empty(t("common.loading")));
  load();
  const timer = setInterval(load, 20000);
  return { unmount() { alive = false; clearInterval(timer); }, theme() { if (last) render(last); } };
}
