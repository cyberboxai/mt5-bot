// Side drawers (agent, decision dossier) and the control-action modal. The panel token is
// typed per action, sent once in the Authorization header and never stored.

import { getJSON, postControl } from "./api.js";
import { dataTable, lineChart } from "./charts.js";
import { t } from "./i18n.js";
import { clear, dateTime, el, num, signed } from "./util.js";

const $ = (id) => document.getElementById(id);
let lastFocus = null;

export function closeDrawer() {
  $("drawer").hidden = true;
  $("drawer-backdrop").hidden = true;
  if (lastFocus) lastFocus.focus();
}

function openDrawer(title, subtitle, sections) {
  lastFocus = document.activeElement;
  const body = clear($("drawer-body"));
  $("drawer-title").textContent = title;
  $("drawer-sub").textContent = subtitle || "";
  for (const s of sections) {
    if (!s || !s.node) continue;
    body.append(el("section", {}, el("h3", { text: s.title }), s.node));
  }
  $("drawer").hidden = false;
  $("drawer-backdrop").hidden = false;
  $("drawer-close").focus();
}

function kv(pairs) {
  return el("dl", { class: "kv" }, pairs.filter(Boolean).flatMap(([k, v]) => [el("dt", { text: k }),
    el("dd", { text: v === null || v === undefined || v === "" ? "n/d" : String(v) })]));
}

export async function openAgent(id) {
  openDrawer(t("common.loading"), "", []);
  let a;
  try { a = await getJSON(`/api/agents/${encodeURIComponent(id)}`); } catch (e) { closeDrawer(); return; }
  const weights = Object.entries(a.weights || {}).map(([regime, w]) => `${regime}: ${num(w, 2)}`).join(" · ");
  const wh = (a.weight_history || []).filter((p) => p.regime === "all");
  const weightNode = el("div");
  if (wh.length >= 2) {
    lineChart(weightNode, { series: [{ name: t("agent.weight"), color: "var(--series-1)",
      points: wh.map((p) => ({ x: p.ts, y: p.weight })) }], height: 150, xFormat: (v) => dateTime(v).slice(0, 10) });
  } else weightNode.append(el("div", { class: "empty", text: t("common.insufficient", { n: wh.length, min: 2 }) }));
  const byRegime = Object.entries(a.accuracy_by_regime || {}).map(([regime, v]) => ({ regime, votes: v.n,
    accuracy: v.accuracy === null ? "n/d" : `${num(v.accuracy * 100, 1)}%` }));
  openDrawer(`${a.name} · ${a.role}`, t(`state.${a.state}`) + (a.benched ? ` · ${a.bench_reason || ""}` : ""), [
    { title: t("agent.role"), node: kv([[t("agent.can"), a.can], [t("agent.cannot"), a.cannot], [t("agent.task"), a.task],
      [t("agent.weight"), weights || "uniforme"],
      [t("agent.calibration"), a.brier_raw === null ? "n/d" : `${num(a.brier_raw, 4)} / ${num(a.brier_cal, 4)}`],
      [t("agent.contribution"), a.value_added === null || a.value_added === undefined ? "n/d" : signed(a.value_added, 4)],
      ["Chamadas / falhas", `${a.calls ?? 0} / ${a.failures ?? 0}`]]) },
    { title: t("agent.weight"), node: weightNode },
    { title: t("agent.accuracy"), node: byRegime.length ? dataTable(byRegime) : el("div", { class: "empty", text: t("common.nodata") }) },
    { title: t("agent.reliability"), node: (a.reliability || []).length ? dataTable(a.reliability.map((b) => ({
      bin: `${b.bin[0]}-${b.bin[1]}`, n: b.n, declarada: num(b.claimed, 3), observada: num(b.observed, 3) })))
      : el("div", { class: "empty", text: t("common.nodata") }) },
    { title: t("agent.opinions"), node: (a.opinions || []).length ? dataTable(a.opinions.slice(0, 15).map((o) => ({
      hora: dateTime(o.ts), símbolo: o.symbol, proposta: o.side, posição: t(`stance.${o.stance}`),
      confiança: num(o.confidence, 2), resultado: o.outcome_r === null ? "pendente" : signed(o.outcome_r, 2) }))) : null },
    { title: t("agent.messages"), node: el("ul", { class: "small" }, (a.messages || []).slice(0, 15).map((m) =>
      el("li", { text: `${dateTime(m.ts)}: ${m.text || m.topic}` }))) },
  ]);
}

export async function openDossier(id) {
  openDrawer(t("common.loading"), "", []);
  let d;
  try { d = await getJSON(`/api/decisions/${encodeURIComponent(id)}`); } catch (e) { closeDrawer(); return; }
  const ens = d.ensemble || {};
  const opinions = (d.opinions || []).map((o) => ({ agente: o.agent, papel: o.role, posição: t(`stance.${o.stance}`),
    confiança: o.confidence === undefined ? "" : num(o.confidence, 3), peso: o.weight === undefined ? "" : num(o.weight, 3),
    banco: o.benched ? "sim" : "", motivo: (o.reasons && o.reasons[0] ? o.reasons[0][1] : o.error) || "" }));
  const adv = d.advisor || {};
  const risk = d.risk || {};
  const exec = d.execution || {};
  const outcome = d.outcome || {};
  openDrawer(`${t("dossier.title")}: ${d.side} ${d.symbol}`, `${dateTime(d.ts)} · ${d.strategy} · ${t(`mode.${d.mode}`)}`, [
    { title: t("office.result"), node: kv([["Acção", d.final_action === "enter" ? t("decisions.enter") : t("decisions.no_trade")],
      ["Multiplicador equipa", num(d.team_multiplier, 3)], ["Multiplicador conselheiro", d.advisor_multiplier ?? "n/d"],
      ["Multiplicador final", num(d.final_multiplier, 3)], ["Regime", d.regime], ["Sessão", d.session]]) },
    { title: t("dossier.ensemble"), node: kv([["Confiança agregada", num(ens.aggregate_confidence, 3)],
      ["Desacordo", num(ens.disagreement, 3)], ["Concordantes", ens.agreement_count], ["Motivos", (ens.reasons || []).join("; ")],
      ["Vetos", (ens.vetoed_by || []).join(", ") || "nenhum"], ["Regra da CIO", ens.cio_rule_reason || "nenhuma"],
      ["Bloqueio de segurança", ens.all_mode_block || "nenhum"],
      ["Contrafactual (equipa completa)", num(ens.counterfactual_full, 3)]]) },
    { title: t("dossier.opinions"), node: dataTable(opinions) },
    { title: t("dossier.advisor"), node: kv([["Estado", adv.status], ["Veredicto", adv.verdict], ["Multiplicador", adv.effective_multiplier],
      ["Motivo", adv.reason || (adv.reasons || []).map((r) => r.text).join("; ")], ["Latência (ms)", adv.latency_ms]]) },
    { title: t("dossier.risk"), node: kv([["Pré-verificação", risk.preview ? `${risk.preview.allowed ? "OK" : "VETO"}: ${risk.preview.reason}` : "n/d"],
      ["Na hora do envio", risk.gate ? `${risk.gate.allowed ? "OK" : "VETO"}: ${risk.gate.reason}` : "n/d"]]) },
    { title: t("dossier.execution"), node: kv([["Enviada", exec.sent ? "sim" : "não"], ["Resultado", exec.reason],
      ["Volume", exec.volume], ["Preço", exec.price], ["SL / TP", exec.sl ? `${exec.sl} / ${exec.tp}` : "n/d"],
      ["Slippage (pontos)", exec.slippage_points], ["Simulação a seco", exec.dry_run ? "sim" : "não"]]) },
    { title: t("dossier.outcome"), node: kv([["R da proposta", d.r_base === null || d.r_base === undefined ? "pendente" : signed(d.r_base, 3)],
      ["Fonte", outcome.kind], ["R efectivo", outcome.taken_r === undefined ? "n/d" : signed(outcome.taken_r, 3)]]) },
    d.explanation ? { title: t("dossier.explanation"), node: el("div", {}, el("span", { class: "ai-label", text: t("common.ai") }),
      el("p", { text: d.explanation })) } : null,
  ]);
}

export function openControlModal(action, onDone) {
  const modal = $("modal");
  const box = clear($("modal-box"));
  const token = el("input", { type: "password", autocomplete: "off", "aria-label": t("control.token") });
  const confirm = el("input", { type: "checkbox", id: "confirm-control" });
  const send = el("button", { class: `btn ${action === "kill" ? "danger" : "primary"}`, type: "button", text: t("control.send") });
  const cancel = el("button", { class: "btn", type: "button", text: t("common.close") });
  const msg = el("p", { class: "small", "aria-live": "polite" });
  box.append(el("h3", { text: t(`control.${action}`) }),
    action === "kill" ? el("p", { class: "small", text: t("control.kill_warn") }) : null,
    el("label", { class: "small", text: t("control.token") }), token,
    el("label", { class: "small" }, confirm, ` ${t("control.confirm")}`), msg,
    el("div", { class: "controls-row" }, send, cancel));
  const close = () => { modal.hidden = true; token.value = ""; };
  cancel.addEventListener("click", close);
  send.addEventListener("click", async () => {
    if (!confirm.checked || !token.value) { msg.textContent = t("control.confirm"); return; }
    send.disabled = true;
    try {
      const res = await postControl(action, token.value);
      msg.textContent = res.ok ? res.body.detail : (res.body.error || `erro ${res.status}`);
      if (res.ok) { token.value = ""; setTimeout(close, 1200); if (onDone) onDone(); }
    } catch (e) { msg.textContent = String(e); } finally { send.disabled = false; }
  });
  modal.hidden = false;
  token.focus();
}

export function wireDrawer() {
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { closeDrawer(); $("modal").hidden = true; }
  });
}
