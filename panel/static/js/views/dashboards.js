// The ten dashboards. Each mount() returns { unmount, theme }. Data comes only from the API;
// with too little data a chart says so instead of drawing misleading numbers.

import { getJSON } from "../api.js";
import { barChart, chartCard, dataTable, heatmap, lineChart, meter, sparkline } from "../charts.js";
import { openControlModal, openDossier } from "../drawer.js";
import { reasonText, t, tv } from "../i18n.js";
import { ago, card, clear, dateTime, downloadCsv, el, empty, num, pct, signed, tile } from "../util.js";

const S = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)"];
const VOTERS = ["trend_analyst", "mean_reversion_analyst", "breakout_analyst", "mtf_analyst", "meta_labeler"];
const day = (ts) => dateTime(ts).slice(0, 10);
// Quotes as MT5 shows them: 3 decimals for JPY-like prices, 5 otherwise.
const price = (x) => (x === null || x === undefined || x === "" ? "" : Number(x).toFixed(Math.abs(Number(x)) >= 20 ? 3 : 5));

function view(root, title, sub, load, render, everyMs = 15000, prefix = null) {
  const head = [el("h2", { class: "view-title", text: title }), sub ? el("p", { class: "view-sub", text: sub }) : null, prefix];
  const body = el("div");
  root.append(...head.filter(Boolean), body);
  let alive = true;
  let lastData = null;
  const run = async () => {
    try {
      const data = await load();
      if (!alive) return;
      lastData = data;
      body.style.opacity = "1";
      clear(body);
      render(body, data);
    } catch (e) {
      if (alive && !lastData) { clear(body); body.append(empty(`${t("common.nodata")} (${e.message})`)); }
    }
  };
  body.append(empty(t("common.loading")));
  run();
  const timer = everyMs ? setInterval(() => { body.style.opacity = "0.85"; run(); }, everyMs) : null;
  return { unmount() { alive = false; if (timer) clearInterval(timer); },
    theme() { if (lastData) { clear(body); render(body, lastData); } }, rerun: run };
}

function deltaTile(label, value, note) {
  const cls = value === null || value === undefined ? "" : value >= 0 ? "delta-up" : "delta-down";
  const node = tile(label, value === null || value === undefined ? "n/d" : signed(value, 2), note);
  node.querySelector(".value").classList.add(...(cls ? [cls] : []));
  return node;
}

// The bot's per-symbol status line, without repeating the champion shown next to it.
function statusText(line) { return line && line.startsWith("champion ") ? "a negociar" : reasonText(line); }

function statusChip(text, level) { return el("span", { class: `status-chip ${level}`, text }); }

// ------------------------------------------------------------------ overview

export function mountOverview(root, app) {
  return view(root, t("tab.overview"), null, async () => {
    const [equity, office, open, strategies] = await Promise.all([getJSON("/api/equity?days=90"), getJSON("/api/office/state"),
      getJSON("/api/trades?mode=live&status=open&limit=50"), getJSON("/api/strategies")]);
    return { equity, office, open, strategies };
  }, (body, { equity, office, open, strategies }) => {
    const s = equity.summary || {};
    const status = app.status || {};
    body.append(el("div", { class: "grid tiles" },
      tile(t("overview.equity"), s.equity === null || s.equity === undefined ? "n/d" : num(s.equity, 2), `${t("overview.drawdown")}: ${num(s.drawdown_pct, 2)}%`, "hero"),
      deltaTile(t("overview.today"), s.today), deltaTile(t("overview.week"), s.week), deltaTile(t("overview.month"), s.month),
      deltaTile(t("overview.all"), s.all_time), tile(t("overview.positions"), String(status.open_positions ?? open.rows.length)),
      tile(t("overview.team"), t(`mode.${(status.team || {}).effective_mode || "baseline"}`), reasonText((status.team || {}).mode_reason)),
      tile(t("tab.advisor"), t(`advisor.${(status.advisor || {}).state || "DISABLED"}`), reasonText((status.advisor || {}).reason))));
    const pts = equity.points || [];
    const rows = pts.map((p) => ({ utc: dateTime(p.ts), equity: p.equity, balance: p.balance, peak: p.peak }));
    const grid = el("div", { class: "grid cols-2" });
    grid.append(chartCard({ title: t("overview.curve"), rows, csvName: "equity.csv", n: pts.length, render: (n) =>
      lineChart(n, { series: [{ name: t("overview.equity"), color: S[0], points: pts.map((p) => ({ x: p.ts, y: p.equity })) },
        { name: "Saldo", color: S[1], points: pts.map((p) => ({ x: p.ts, y: p.balance })) }], xFormat: day, yFormat: (v) => num(v, 0) }) }));
    const g = (office.screens || {}).risk || {};
    const gauges = el("section", { class: "card" }, el("h3", { text: t("overview.gauges") }));
    if (g.daily_loss_used_pct_of_limit === undefined) gauges.append(empty(t("common.nodata")));
    else {
      gauges.append(meter({ label: t("overview.daily_loss"), value: g.daily_loss_used_pct_of_limit, max: 100,
        text: `${num(g.daily_loss_used_pct_of_limit, 0)}% do limite` }),
      meter({ label: t("overview.open_risk"), value: g.open_risk_pct, max: g.max_open_risk_pct || 1,
        text: `${num(g.open_risk_pct, 2)}% / ${num(g.max_open_risk_pct, 2)}%` }),
      meter({ label: t("overview.dd_breaker"), value: g.drawdown_pct, max: g.drawdown_limit_pct || 1,
        text: `${num(g.drawdown_pct, 2)}% / ${num(g.drawdown_limit_pct, 1)}%`, note: "o disjuntor fecha tudo e pára ao atingir o limite" }));
    }
    grid.append(gauges);
    const champs = Object.entries(strategies.symbols || {}).map(([sym, v]) => ({ símbolo: sym, campeão: v.champion, estado: statusText(v.status_line) }));
    grid.append(card(t("overview.champions"), null, champs.length ? dataTable(champs) : empty(t("common.nodata"))));
    const screens = (office.screens || {}).symbols || {};
    const openRows = (open.rows || []).map((r) => {
      const sc = screens[r.symbol] || {};
      const price = r.side === "BUY" ? sc.bid : sc.ask;
      const dist = Math.abs(r.entry - r.sl);
      const rNow = price && dist ? ((price - r.entry) * (r.side === "BUY" ? 1 : -1)) / dist : null;
      return { símbolo: r.symbol, lado: r.side, volume: r.volume, entrada: r.entry, sl: r.sl, tp: r.tp,
        risco: num(r.risk_money, 2), "P&L (R aprox.)": rNow === null ? "n/d" : signed(rNow, 2) };
    });
    grid.append(card(t("overview.open_table"), "P&L em R a partir do último preço visto (aproximado)",
      openRows.length ? dataTable(openRows) : empty("Sem posições abertas.")));
    body.append(grid);
  }, 10000);
}

// --------------------------------------------------------------- performance

export function mountPerformance(root) {
  return view(root, t("tab.performance"), "Apenas resultados passados; nenhuma projecção.", () => getJSON("/api/performance"),
    (body, d) => {
      const live = d.live || {};
      const eq = d.equity || {};
      const note = live.insufficient ? t("common.insufficient", { n: live.trades || 0, min: live.min_trades || 10 }) : null;
      body.append(el("div", { class: "grid tiles" },
        tile(t("perf.net"), num(live.net_profit, 2), note), tile(t("perf.win"), live.win_rate === null ? "n/d" : pct(live.win_rate)),
        tile(t("perf.avg_r"), signed(live.avg_r, 3)), tile(t("perf.exp"), live.expectancy_r === null ? "n/d" : `${signed(live.expectancy_r, 3)} R`),
        tile(t("perf.pf"), num(live.profit_factor, 2)), tile(t("perf.sharpe"), num(eq.sharpe, 2), eq.insufficient ? "dias insuficientes" : "diário, anualizado"),
        tile(t("perf.sortino"), num(eq.sortino, 2)), tile(t("perf.calmar"), num(eq.calmar, 2)),
        tile(t("perf.maxdd"), eq.max_drawdown_pct === null ? "n/d" : `${num(eq.max_drawdown_pct, 2)}%`, `${live.max_drawdown_r ?? 0} R`),
        tile(t("perf.dddays"), eq.max_drawdown_days === null || eq.max_drawdown_days === undefined ? "n/d" : `${eq.max_drawdown_days} dias`),
        tile(t("perf.best"), signed(live.best_r, 2) + " R", num(live.best_pnl, 2)), tile(t("perf.worst"), signed(live.worst_r, 2) + " R", num(live.worst_pnl, 2)),
        tile(t("perf.wstreak"), String(live.longest_win_streak ?? 0)), tile(t("perf.lstreak"), String(live.longest_loss_streak ?? 0))));
      const grid = el("div", { class: "grid cols-2" });
      const hist = (live.histogram || []).map((h) => ({ de: h.from, até: h.to, trades: h.count }));
      grid.append(chartCard({ title: t("perf.hist"), rows: hist, csvName: "r_distribution.csv", n: live.trades || 0, min: 10,
        render: (n) => barChart(n, { bars: hist.map((h) => ({ label: `${h.de}`, value: h.trades, note: `${h.de} a ${h.até} R` })) }) }));
      const months = eq.monthly || [];
      const years = [...new Set(months.map((m) => m.month.slice(0, 4)))];
      const cols = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"];
      grid.append(chartCard({ title: t("perf.monthly"), sub: "variação % do capital por mês (azul ganho, vermelho perda)",
        rows: months, csvName: "monthly_returns.csv", n: months.length, min: 1, render: (n) => heatmap(n, { rows: years, cols,
          values: years.map((y) => cols.map((c) => { const m = months.find((x) => x.month === `${y}-${c}`); return m ? m.return_pct : null; })),
          scale: "diverging", format: (v) => `${num(v, 2)}%`, title: t("perf.monthly") }) }));
      const cum = live.cumulative_r || [];
      grid.append(chartCard({ title: t("perf.cum"), rows: cum.map((v, i) => ({ trade: i + 1, r_acumulado: v })), csvName: "cum_r.csv",
        n: cum.length, render: (n) => lineChart(n, { series: [{ name: "R", color: S[0], points: cum.map((v, i) => ({ x: i + 1, y: v })) }],
          zeroLine: true, xFormat: (v) => `#${Math.round(v)}` }) }));
      const roll = live.rolling || [];
      grid.append(chartCard({ title: t("perf.rolling"), rows: roll, csvName: "rolling.csv", n: roll.length, render: (n) =>
        lineChart(n, { series: [{ name: "expectativa", color: S[0], points: roll.map((r) => ({ x: r.i, y: r.expectancy_r })) }],
          zeroLine: true, xFormat: (v) => `#${Math.round(v)}`, yFormat: (v) => num(v, 2) }) }));
      const bt = d.baseline_vs_team || {};
      const base = bt.baseline_cum_r || [];
      const team = bt.team_cum_r || [];
      grid.append(chartCard({ title: t("perf.baseline"), sub: reasonText(bt.report && bt.report.verdict) || "a recolher evidência",
        rows: base.map((v, i) => ({ decisão: i + 1, núcleo_base: v, equipa: team[i] })), csvName: "baseline_vs_team.csv",
        n: base.length, min: 2, render: (n) => lineChart(n, { series: [
          { name: "Núcleo base (v2)", color: S[0], points: base.map((v, i) => ({ x: i + 1, y: v })) },
          { name: "Equipa completa", color: S[1], points: team.map((v, i) => ({ x: i + 1, y: v })) }],
        zeroLine: true, xFormat: (v) => `#${Math.round(v)}`, yFormat: (v) => `${num(v, 1)}R` }) }));
      const bh = d.buy_and_hold || {};
      grid.append(chartCard({ title: t("perf.bh"), sub: bh.note, rows: bh.curve || [], csvName: "buy_and_hold.csv",
        n: bh.available ? (bh.curve || []).length : 0, render: (n) => lineChart(n, { series: [{ name: bh.symbol, color: S[0],
          points: bh.curve.map((p) => ({ x: p.time, y: p.pct })) }], zeroLine: true, xFormat: day, yFormat: (v) => `${num(v, 1)}%` }) }));
      const sh = d.shadow || {};
      grid.append(card(t("perf.shadow"), "virtuais, com as regras de preenchimento do backtester",
        el("div", { class: "grid tiles" }, tile("Trades", String(sh.trades || 0)), tile(t("perf.exp"), signed(sh.expectancy_r, 3)),
          tile(t("perf.win"), sh.win_rate === null ? "n/d" : pct(sh.win_rate)))));
      body.append(grid);
    }, 30000);
}

// -------------------------------------------------------------------- trades

export function mountTrades(root) {
  const state = { mode: "live", status: "", symbol: "", sort: "open_ts", order: "desc" };
  const mode = el("select", {}, el("option", { value: "live", text: t("trades.live") }), el("option", { value: "shadow", text: t("trades.shadow") }));
  const status = el("select", {}, el("option", { value: "", text: t("office.all") }), el("option", { value: "open", text: t("trades.open") }),
    el("option", { value: "closed", text: t("trades.closed") }));
  const symbol = el("input", { type: "text", placeholder: "EURUSD", size: 10 });
  const csv = el("button", { class: "mini-btn", type: "button", text: t("common.csv") });
  const filters = el("div", { class: "filters" }, el("label", {}, t("trades.mode"), mode), el("label", {}, t("trades.status"), status),
    el("label", {}, t("trades.symbol"), symbol), csv);
  let rowsCache = [];
  const v = view(root, t("tab.trades"), "Clique numa linha com dossier para ver a decisão completa.", () => getJSON(
    `/api/trades?mode=${state.mode}&status=${state.status}&symbol=${encodeURIComponent(state.symbol)}&sort=${state.sort}&order=${state.order}&limit=200`),
  (body, data) => {
    const cols = [["open_ts", "Abertura", (x) => dateTime(x)], ["close_ts", "Fecho", (x) => (x ? dateTime(x) : "aberto")],
      ["symbol", "Símbolo"], ["side", "Lado"], ["strategy", "Estratégia"], ["volume", "Volume", (x) => num(x, 2)],
      ["entry", "Entrada", price], ["exit", "Saída", price], ["r", "R", (x) => signed(x, 2)], ["pnl", "P&L", (x) => num(x, 2)],
      ["mae_r", "MAE R", (x) => num(x, 2)], ["mfe_r", "MFE R", (x) => num(x, 2)], ["slippage_points", "Slip. pts", (x) => num(x, 1)],
      ["commission_estimate", "Comissão est.", (x) => num(x, 2)], ["regime", "Regime", (x) => tv("regime", x)], ["session", "Sessão"],
      ["team_multiplier", "x equipa", (x) => num(x, 2)], ["exit_reason", "Motivo"]];
    rowsCache = data.rows;
    if (!data.rows.length) { body.append(empty(t("common.nodata"))); return; }
    const head = el("tr", {}, cols.map(([key, label]) => {
      const th = el("th", { class: "sortable", text: `${label}${state.sort === key ? (state.order === "desc" ? " ▼" : " ▲") : ""}`, tabindex: 0 });
      const sort = () => { state.order = state.sort === key && state.order === "desc" ? "asc" : "desc"; state.sort = key; v.rerun(); };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", (ev) => { if (ev.key === "Enter") sort(); });
      return th;
    }));
    const tbody = el("tbody", {}, data.rows.map((r) => {
      const tr = el("tr", { class: r.dossier_id ? "clickable" : "", tabindex: r.dossier_id ? 0 : undefined },
        cols.map(([key, , fmt]) => el("td", { text: fmt ? fmt(r[key]) : r[key] ?? "" })));
      if (r.dossier_id) {
        tr.addEventListener("click", () => openDossier(r.dossier_id));
        tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openDossier(r.dossier_id); });
      }
      return tr;
    }));
    body.append(el("p", { class: "small muted", text: `${data.total} trades (${data.rows.length} mostrados). Swap: n/d no jornal.` }),
      el("div", { class: "table-wrap" }, el("table", { class: "data" }, el("thead", {}, head), tbody)));
  }, 30000, filters);
  mode.addEventListener("change", () => { state.mode = mode.value; v.rerun(); });
  status.addEventListener("change", () => { state.status = status.value; v.rerun(); });
  symbol.addEventListener("change", () => { state.symbol = symbol.value.trim().toUpperCase(); v.rerun(); });
  csv.addEventListener("click", () => downloadCsv("trades.csv", rowsCache));
  return v;
}

// ---------------------------------------------------------------------- team

export function mountTeam(root) {
  return view(root, t("tab.team"), "Pesos aprendidos devagar, limitados; o valor acrescentado é medido por ablação fora da amostra.",
    () => getJSON("/api/team"), (body, d) => {
      const ab = ((d.ablation || {}).payload) || {};
      body.append(el("div", { class: "grid tiles" },
        tile("Modo efectivo", t(`mode.${(d.mode || {}).effective_mode || "baseline"}`), reasonText((d.mode || {}).mode_reason)),
        tile("Sharpe por proposta: núcleo base", num((ab.baseline || {}).sharpe, 3), `${(ab.baseline || {}).n || 0} decisões`),
        tile("Sharpe por proposta: equipa", num((ab.team || {}).sharpe, 3), reasonText(ab.verdict) || "sem relatório"),
        tile("Conselheiro: valor", signed((ab.advisor || {}).value_added_sharpe, 4), `${(ab.advisor || {}).reviews || 0} revisões`)));
      const rows = d.leaderboard.map((a) => ({ agente: `${a.name} (${a.role})`, departamento: tv("dept", a.department), estado: t(`state.${a.state}`),
        votos: a.votes, acerto: a.accuracy === null ? "n/d" : pct(a.accuracy), brier: a.brier_cal === null ? "n/d" : num(a.brier_cal, 4),
        peso: a.weight === null || a.weight === undefined ? "1,00" : num(a.weight, 2),
        valor: a.value_added === null || a.value_added === undefined ? "n/d" : signed(a.value_added, 4),
        disponibilidade: a.uptime === null ? "n/d" : pct(a.uptime, 1) }));
      body.append(card(t("team.leaderboard"), "acerto: votos direccionais certos; brier: menor é melhor; valor: Sharpe com o agente menos sem ele",
        dataTable(rows)));
      const grid = el("div", { class: "grid cols-2" });
      const series = VOTERS.filter((id) => (d.weights_over_time || {})[id]).map((id, i) => ({
        name: (d.leaderboard.find((a) => a.id === id) || { name: id }).name, color: S[i],
        points: d.weights_over_time[id].map((p) => ({ x: p.ts, y: p.weight })) }));
      const flat = series.flatMap((s) => s.points.map((p) => ({ agente: s.name, utc: dateTime(p.x), peso: p.y })));
      grid.append(chartCard({ title: t("team.weights"), sub: "1,0 = uniforme; limitados entre w_min e w_max", rows: flat,
        csvName: "weights.csv", n: series.length ? Math.min(...series.map((s) => s.points.length)) : 0,
        render: (n) => lineChart(n, { series, xFormat: day, yFormat: (v) => num(v, 2) }) }));
      const benched = d.benched.map((a) => ({ agente: a.name, motivo: a.bench_reason || "" }));
      grid.append(card(t("team.benched"), "observam em sombra e podem regressar com evidência",
        benched.length ? dataTable(benched) : empty("Ninguém no banco.")));
      for (const dep of d.departments) {
        const members = d.leaderboard.filter((a) => a.department === dep.id).map((a) => ({ agente: a.name, função: a.role,
          estado: t(`state.${a.state}`), pode: a.can, "não pode": a.cannot }));
        grid.append(card(dep.pt, null, dataTable(members)));
      }
      body.append(grid);
    }, 30000);
}

// ---------------------------------------------------------------- strategies

export function mountStrategies(root) {
  return view(root, t("tab.strategies"), "Optimização walk-forward, portões anti-sobreajuste, campeão/desafiador e modo sombra.",
    () => getJSON("/api/strategies"), (body, d) => {
      const grid = el("div", { class: "grid cols-2" });
      for (const [sym, s] of Object.entries(d.symbols || {})) {
        const ch = (s.challengers || []).map((c) => meter({ label: c.candidate, value: c.shadow_trades, max: Math.max(30, c.shadow_trades),
          text: `${c.shadow_trades} trades sombra, ${signed(c.shadow_expectancy_r, 3)} R`, note: c.enough_evidence ? "evidência suficiente" : "a recolher" }));
        grid.append(card(`${sym}: ${s.champion}`, statusText(s.status_line), ...(ch.length ? ch : [el("p", { class: "small muted", text: "sem desafiadores" })])));
      }
      body.append(grid);
      const runs = (d.runs || []).map((r) => ({ id: r.id, símbolo: r.symbol, motivo: r.trigger, início: dateTime(r.started_at), estado: r.status,
        ensaios: r.trials, notas: r.notes || "" }));
      body.append(card(t("strat.runs"), null, runs.length ? dataTable(runs) : empty("Ainda sem ciclos de optimização (normal nos primeiros dias).")));
      const cands = (d.candidates || []).map((c) => ({ ciclo: c.run_id, estratégia: c.strategy, passou: c.passed ? "sim" : "não",
        score: num(c.score, 3), trades: c.metrics.trades, "exp. R": num(c.metrics.expectancy_r, 3), PF: num(c.metrics.profit_factor, 2),
        DSR: num(c.metrics.deflated_sharpe, 2), "janelas +": num(c.metrics.profitable_window_ratio, 2), ensaios: c.metrics.total_trials }));
      const lower = el("div", { class: "grid cols-2" });
      lower.append(card(t("strat.candidates"), null, cands.length ? dataTable(cands) : empty(t("common.nodata"))));
      lower.append(card(t("strat.rejected"), null, (d.rejected || []).length ? dataTable(d.rejected.map((r) => ({ ciclo: r.run_id,
        estratégia: r.strategy, portão: r.gate }))) : empty("Nenhuma rejeição registada.")));
      const best = (d.candidates || []).find((c) => Object.keys(c.stability || {}).length);
      if (best) {
        const params = Object.keys(best.stability);
        const values = [...new Set(params.flatMap((p) => Object.keys(best.stability[p])))];
        lower.append(chartCard({ title: `${t("strat.stability")}: ${best.strategy}`, sub: "quantas janelas escolheram cada valor",
          rows: params.flatMap((p) => Object.entries(best.stability[p]).map(([v, c]) => ({ parâmetro: p, valor: v, janelas: c }))),
          csvName: "stability.csv", n: params.length, min: 1, render: (n) => heatmap(n, { rows: params, cols: values,
            values: params.map((p) => values.map((v) => best.stability[p][v] ?? null)), format: (x) => String(x) }) }));
      }
      lower.append(card(t("strat.promotions"), null, (d.promotions || []).length ? dataTable(d.promotions.map((p) => ({ "#": p.id,
        data: dateTime(p.ts), símbolo: p.symbol, acção: tv("promo", p.action), de: p.old_desc, para: p.new_desc, motivo: p.reason }))) : empty(t("common.nodata"))));
      lower.append(card(t("strat.shadow"), null, (d.shadow_progress || []).length ? dataTable(d.shadow_progress, [
        { key: "param_set_id", label: t("col.param_set_id") }, { key: "symbol", label: t("col.symbol") },
        { key: "trades", label: t("col.trades"), num: true },
        { key: "mean_r", label: t("col.mean_r"), num: true, format: (x) => signed(x, 3) }]) : empty(t("common.nodata"))));
      lower.append(card(t("strat.hypotheses"), "vindas do conselheiro; passam pelos mesmos portões, sem atalhos",
        (d.hypotheses || []).length ? dataTable(d.hypotheses.map((h) => ({ data: dateTime(h.ts), estratégia: h.strategy, parâmetro: h.param,
          valor: h.value, estado: h.status, nota: h.note || "" }))) : empty("Sem hipóteses.")));
      body.append(lower);
    }, 30000);
}

// ---------------------------------------------------------------------- risk

export function mountRisk(root) {
  return view(root, t("tab.risk"), "Limites rígidos no código; o painel não os pode relaxar.", () => getJSON("/api/risk"), (body, d) => {
    const v = d.var || {};
    const w = d.worst_case_open_risk || {};
    body.append(el("div", { class: "grid tiles" },
      tile(t("risk.var"), v.var_pct === null || v.var_pct === undefined ? "n/d" : `${num(v.var_pct, 2)}%`, v.label || v.note),
      tile(t("risk.worst"), w.pct === null || w.pct === undefined ? "0%" : `${num(w.pct, 2)}%`, w.note),
      tile("Drawdown", `${num(d.drawdown_pct, 2)}%`, d.halted || d.entry_block || "sem bloqueios")));
    const grid = el("div", { class: "grid cols-2" });
    const bySym = Object.entries(d.by_symbol || {}).map(([s, x]) => ({ símbolo: s, posições: x.positions, risco_pct: x.risk_pct }));
    grid.append(chartCard({ title: t("risk.by_symbol"), sub: "risco até ao SL em % do capital", rows: bySym, csvName: "exposure_symbol.csv",
      n: bySym.length, min: 1, render: (n) => barChart(n, { bars: bySym.map((r) => ({ label: r.símbolo, value: r.risco_pct })), yFormat: (x) => `${num(x, 2)}%` }) }));
    const ccy = Object.entries(d.by_currency || {}).map(([c, x]) => ({ moeda: c, exposição: x, lado: x >= 0 ? t("risk.long") : t("risk.short") }));
    grid.append(chartCard({ title: t("risk.by_currency"), sub: "positivo = longo (azul), negativo = curto (vermelho)", rows: ccy,
      csvName: "exposure_currency.csv", n: ccy.length, min: 1, render: (n) => barChart(n, { bars: ccy.map((r) => ({ label: r.moeda,
        value: r.exposição, color: r.exposição >= 0 ? "var(--div-pos)" : "var(--div-neg)", note: r.lado })), yFormat: (x) => `${num(x, 2)}%` }) }));
    const corr = d.correlation || { symbols: [], matrix: [] };
    grid.append(chartCard({ title: t("risk.correlation"), sub: "retornos das últimas barras em cache", rows: corr.symbols.flatMap((a, i) =>
      corr.symbols.map((b, j) => ({ a, b, rho: corr.matrix[i][j] }))), csvName: "correlation.csv", n: corr.symbols.length, min: 2,
    render: (n) => heatmap(n, { rows: corr.symbols, cols: corr.symbols, values: corr.matrix, scale: "diverging", format: (x) => num(x, 2) }) }));
    const lim = el("section", { class: "card" }, el("h3", { text: t("risk.limits") }));
    for (const l of d.limits || []) {
      lim.append(meter({ label: l.name, value: l.current ?? l.config, max: l.ceiling,
        text: `${l.current === undefined || l.current === null ? "" : `actual ${num(l.current, 2)} · `}config ${num(l.config, 2)} · tecto ${num(l.ceiling, 2)}`,
        warnAt: 0.8, critAt: 0.95 }));
    }
    grid.append(lim);
    const b = d.breakers || {};
    grid.append(card(t("risk.breakers"), null, el("ul", {},
      el("li", {}, statusChip(b.halted ? `parado: ${b.halted}` : "perda diária e drawdown: ok", b.halted ? "critical" : "good")),
      el("li", {}, statusChip(b.guardian_freeze ? `Guardião: ${b.guardian_freeze}` : "Guardião: sem congelamento", b.guardian_freeze ? "serious" : "good")),
      el("li", {}, statusChip(b.entry_block ? `entradas bloqueadas: ${b.entry_block}` : "entradas permitidas", b.entry_block ? "warning" : "good")),
      el("li", {}, `Distância ao disjuntor de drawdown: ${num(b.drawdown_distance_pct, 2)} pontos percentuais`))));
    body.append(grid);
  }, 15000);
}

// ------------------------------------------------------------------- markets

function sessionsMap(sessions) {
  const node = el("div");
  const rows = Object.entries(sessions.detail || {}).map(([name, s]) => ({ label: t(`session.${name}`), start: s.start_utc, end: s.end_utc, open: s.open }));
  const hour = sessions.utc_hour || 0;
  const wrap = el("div", { class: "table-wrap" }, el("table", { class: "data" }, el("thead", {}, el("tr", {},
    el("th", { text: "Sessão" }), el("th", { text: "UTC" }), el("th", { text: "Estado" }))), el("tbody", {}, rows.map((r) =>
    el("tr", {}, el("td", { text: r.label }), el("td", { text: `${num(r.start, 0)}h-${num(r.end, 0)}h` }),
      el("td", {}, statusChip(r.open ? "aberta" : "fechada", r.open ? "good" : "")))))));
  node.append(el("p", { class: "small muted", text: `Agora: ${num(hour, 2)}h UTC${sessions.overlap ? " · sobreposição Londres/NY" : ""}${sessions.rollover ? " · rollover" : ""}` }), wrap);
  return node;
}

export function mountMarkets(root) {
  return view(root, t("tab.markets"), null, () => getJSON("/api/markets"), (body, d) => {
    const grid = el("div", { class: "grid cols-2" });
    grid.append(card(t("markets.sessions"), null, sessionsMap(d.sessions || {})));
    for (const [sym, m] of Object.entries(d.symbols || {})) {
      const probs = Object.entries(m.probs || {}).map(([k, v]) => ({ regime: k, probabilidade: v }));
      const c = el("section", { class: "card" }, el("h3", { text: `${sym}: ${tv("regime", m.regime) || "?"}` }),
        el("p", { class: "card-sub", text: `ER ${num(m.er, 2)} · ADX ${num(m.adx, 1)} · spread ${num(m.spread_points, 1)} pts (x${num(m.spread_ratio, 2)} do habitual)` }),
        meter({ label: t("markets.vol"), value: m.atr_pct, max: 100, text: `${num(m.atr_pct, 0)}º percentil`, warnAt: 0.85, critAt: 0.95 }));
      if (probs.length) barChart(c, { bars: probs.map((p) => ({ label: tv("regime", p.regime), value: p.probabilidade * 100 })), height: 150, yFormat: (x) => `${num(x, 0)}%` });
      const hours = Object.entries(m.spread_by_hour || {}).map(([h, v]) => ({ hora: Number(h), spread: v })).sort((a, b) => a.hora - b.hora);
      c.append(chartCard({ title: t("markets.spread"), rows: hours, csvName: `spread_${sym}.csv`, n: hours.length, min: 2,
        render: (n) => barChart(n, { bars: hours.map((h) => ({ label: `${h.hora}h`, value: h.spread })), height: 150, yFormat: (x) => num(x, 0) }) }));
      grid.append(c);
    }
    const news = d.news || {};
    grid.append(card(t("markets.news"), reasonText(news.calendar), (news.upcoming || []).length ? dataTable(news.upcoming.map((n) => ({
      hora: dateTime(n.ts), moeda: n.currency, impacto: n.impact, evento: n.title, minutos: n.minutes }))) : empty("Sem eventos (ou sem calendário configurado).")));
    body.append(grid);
  }, 20000);
}

// ------------------------------------------------------------------- advisor

const LEVEL = { ONLINE: "good", DEGRADED: "warning", BUDGET_CAPPED: "warning", OFFLINE_NO_CREDIT: "critical", OFFLINE_AUTH: "critical",
  NO_KEY: "", DISABLED: "" };

export function mountAdvisor(root) {
  return view(root, t("tab.advisor"), "Opcional. O bot é completo sem ele; só pode reduzir ou vetar, nunca aumentar nem negociar.",
    () => getJSON("/api/advisor"), (body, d) => {
      const caps = d.caps || {};
      body.append(el("div", { class: "grid tiles" },
        el("div", { class: "tile" }, el("div", { class: "label", text: t("advisor.state") }), el("div", { class: "value" },
          statusChip(t(`advisor.${d.state}`), LEVEL[d.state] || "")), el("div", { class: "note", text: reasonText(d.reason) })),
        tile(t("advisor.calls"), String(d.calls_today || 0), `máx. ${caps.max_calls_per_hour}/hora`),
        tile(t("advisor.latency"), d.latency_ms && d.latency_ms.p50 !== null ? `${num(d.latency_ms.p50, 0)} ms` : "n/d",
          `p95 ${d.latency_ms ? num(d.latency_ms.p95, 0) : "n/d"} ms · orçamento ${caps.latency_budget_ms} ms`),
        tile(t("advisor.value"), d.value_added ? signed(d.value_added.value_added_sharpe, 4) : "n/d", "Sharpe com conselheiro menos sem ele"),
        tile("Modelos", `${d.model_fast}`, `revisões profundas: ${d.model_deep} · fornecedor ${d.provider}`)));
      const grid = el("div", { class: "grid cols-2" });
      grid.append(el("section", { class: "card" }, el("h3", { text: t("advisor.cost") }), meter({ label: "USD", value: d.cost_today_usd,
        max: caps.max_daily_cost_usd || 0.0001, text: `${num(d.cost_today_usd, 4)} / ${num(caps.max_daily_cost_usd, 2)} USD`,
        note: "estimativa local a partir dos tokens; defina também um limite na consola Anthropic" })));
      const verdicts = Object.entries(d.verdicts || {}).map(([k, v]) => ({ veredicto: tv("verdict", k), n: v }));
      grid.append(chartCard({ title: t("advisor.verdicts"), rows: verdicts, csvName: "verdicts.csv", n: verdicts.length, min: 1,
        render: (n) => barChart(n, { bars: verdicts.map((x) => ({ label: x.veredicto, value: x.n })), height: 150 }) }));
      const errors = Object.entries(d.errors || {}).map(([k, v]) => ({ classe: k, n: v }));
      grid.append(chartCard({ title: t("advisor.errors"), rows: errors, csvName: "advisor_errors.csv", n: errors.length, min: 1,
        render: (n) => barChart(n, { bars: errors.map((x) => ({ label: x.classe, value: x.n })), height: 150 }) }));
      grid.append(card(t("advisor.history"), null, (d.history || []).length ? dataTable(d.history.slice(0, 30).map((h) => ({
        data: dateTime(h.ts), de: h.from_state, para: h.to_state, motivo: reasonText(h.reason) }))) : empty(t("common.nodata"))));
      const b = d.briefing;
      grid.append(card(t("advisor.briefing"), null, b ? el("div", {}, el("span", { class: "ai-label", text: t("common.ai") }),
        el("p", { class: "small muted", text: dateTime(b.ts) }), el("p", { text: (b.payload || {}).regime_view || "" }),
        el("p", { text: (b.payload || {}).narrative || "" })) : empty("Sem briefing (conselheiro desligado ou sem dados).")));
      grid.append(card(t("advisor.research"), null, (d.research || []).length ? el("div", {}, el("span", { class: "ai-label", text: t("common.ai") }),
        ...d.research.map((r) => el("p", { text: `${dateTime(r.ts)}: ${(r.payload || {}).lesson || ""}` }))) : empty(t("common.nodata"))));
      grid.append(card(t("advisor.recent"), null, (d.calls || []).length ? dataTable(d.calls.slice(0, 30).map((c) => ({ data: dateTime(c.ts),
        propósito: tv("purpose", c.purpose), modelo: c.model, resultado: c.outcome, erro: c.error_kind || "", "ms": num(c.latency_ms, 0),
        "USD": num(c.cost_usd, 5), "tokens in/out": `${c.input_tokens}/${c.output_tokens}` }))) : empty(t("common.nodata"))));
      body.append(grid);
    }, 20000);
}

// -------------------------------------------------------------------- health

export function mountHealth(root, app) {
  return view(root, t("tab.health"), null, () => getJSON("/api/health"), (body, d) => {
    const alive = d.bot_alive;
    body.append(el("div", { class: "grid tiles" },
      el("div", { class: "tile" }, el("div", { class: "label", text: t("health.bot") }), el("div", { class: "value" },
        statusChip(alive ? t("health.alive") : t("health.stale"), alive ? "good" : "critical")), el("div", { class: "note", text: `saúde há ${ago(d.health_age_s)}` })),
      tile("Último ciclo", d.last_loop_utc || "n/d"), tile("Último tick", d.last_tick_server || "n/d"),
      tile(t("health.restarts"), d.supervisor_restarts === null || d.supervisor_restarts === undefined ? "n/d" : String(d.supervisor_restarts)),
      tile("Fila de eventos", d.event_bus ? `${d.event_bus.queued} na fila` : "n/d", d.event_bus ? `${d.event_bus.dropped} descartados` : "")));
    const grid = el("div", { class: "grid cols-3" });
    const labels = { cpu_pct: "CPU %", rss_mb: "RAM MB", loop_ms: "Ciclo ms", mt5_latency_ms: "Latência MT5 ms", tick_age_s: "Idade do tick s",
      office_db_mb: "office.sqlite MB", registry_db_mb: "registry.sqlite MB", log_mb: "bot.log MB", disk_free_gb: "Disco livre GB" };
    for (const [key, label] of Object.entries(labels)) {
      const series = (d.series || {})[key] || [];
      const lastV = series.length ? series[series.length - 1].value : null;
      grid.append(chartCard({ title: label, sub: lastV === null ? "sem amostras" : `actual ${num(lastV, 2)}`, rows: series.map((p) => ({ utc: dateTime(p.ts), valor: p.value })),
        csvName: `${key}.csv`, n: series.length, render: (n) => lineChart(n, { series: [{ name: label, color: S[0],
          points: series.map((p) => ({ x: p.ts, y: p.value })) }], height: 150, xFormat: (v) => dateTime(v).slice(11, 16) }) }));
    }
    body.append(grid);
    const controls = el("section", { class: "card" }, el("h3", { text: t("health.controls") }));
    const st = (app.status || {}).controls || {};
    if (!st.enabled) controls.append(el("p", { class: "small muted", text: t("control.disabled", { reason: st.reason || "" }) }));
    controls.append(el("div", { class: "controls-row" }, ["pause", "resume", "kill"].map((action) => {
      const btn = el("button", { class: `btn ${action === "kill" ? "danger" : ""}`, type: "button", text: t(`control.${action}`), disabled: !st.enabled });
      btn.addEventListener("click", () => openControlModal(action));
      return btn;
    })));
    const lower = el("div", { class: "grid cols-2" });
    lower.append(controls);
    lower.append(card(t("health.alerts"), null, (d.alerts || []).length ? dataTable(d.alerts.slice(0, 40).map((a) => ({ data: dateTime(a.ts),
      gravidade: a.severity, tópico: a.topic, agente: a.agent || "", detalhe: JSON.stringify(a.payload).slice(0, 120) }))) : empty("Sem alertas.")));
    lower.append(card(t("health.audit"), null, (d.control_audit || []).length ? dataTable(d.control_audit.map((a) => ({ data: dateTime(a.ts),
      acção: a.action, cliente: a.client, ok: a.ok ? "sim" : "recusada", detalhe: a.detail }))) : empty("Sem acções de controlo.")));
    lower.append(card(t("health.log"), "segredos são ocultados nos registos", el("pre", { class: "log", text: (d.log_tail || []).join("\n") || "bot.log vazio ou ausente" })));
    body.append(lower);
  }, 15000);
}

// ----------------------------------------------------------------- decisions

export function mountDecisions(root) {
  const state = { q: "", symbol: "", action: "" };
  const q = el("input", { type: "search", placeholder: "id, estratégia, regime, motivo...", size: 28, "aria-label": t("decisions.search") });
  const symbol = el("input", { type: "text", placeholder: "EURUSD", size: 10, "aria-label": t("trades.symbol") });
  const action = el("select", { "aria-label": t("decisions.action") }, el("option", { value: "", text: t("office.all") }),
    el("option", { value: "enter", text: t("decisions.enter") }), el("option", { value: "no_trade", text: t("decisions.no_trade") }));
  const filters = el("div", { class: "filters" }, el("label", {}, t("decisions.search"), q), el("label", {}, t("trades.symbol"), symbol),
    el("label", {}, t("decisions.action"), action));
  let rows = [];
  const v = view(root, t("tab.decisions"), "Cada decisão guarda as entradas, todos os votos, os pesos, o conselheiro, o risco e o resultado.",
    () => getJSON(`/api/decisions?q=${encodeURIComponent(state.q)}&symbol=${encodeURIComponent(state.symbol)}&action=${state.action}&limit=200`),
    (body, d) => {
      rows = d.rows;
      if (!rows.length) { body.append(empty(t("common.nodata"))); return; }
      const csv = el("button", { class: "mini-btn", type: "button", text: t("common.csv") });
      csv.addEventListener("click", () => downloadCsv("decisions.csv", rows));
      const tbody = el("tbody", {}, rows.map((r) => {
        const tr = el("tr", { class: "clickable", tabindex: 0 }, [dateTime(r.ts), r.symbol, r.side, r.strategy, t(`mode.${r.mode}`), tv("regime", r.regime),
          r.action === "enter" ? t("decisions.enter") : t("decisions.no_trade"), num(r.team_multiplier, 2), tv("advstatus", r.advisor),
          r.risk_allowed === false ? `VETO: ${r.risk_reason}` : r.risk_allowed ? "ok" : "n/d",
          r.r_base === null || r.r_base === undefined ? "pendente" : signed(r.r_base, 2), (r.reasons || [])[0] || ""].map((x, i) => el("td", { text: x ?? "", class: i === 9 || i === 11 ? "wrap" : "" })));
        tr.addEventListener("click", () => openDossier(r.id));
        tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openDossier(r.id); });
        return tr;
      }));
      body.append(el("div", { class: "card-head" }, el("p", { class: "small muted", text: `${rows.length} decisões` }), csv),
        el("div", { class: "table-wrap" }, el("table", { class: "data" }, el("thead", {}, el("tr", {}, ["Hora", "Símbolo", "Lado", "Estratégia",
          "Modo", "Regime", "Acção", "x equipa", "Conselheiro", "Risco", "R da proposta", "Motivo"].map((h) => el("th", { text: h })))), tbody)));
    }, 20000, filters);
  q.addEventListener("change", () => { state.q = q.value.trim(); v.rerun(); });
  symbol.addEventListener("change", () => { state.symbol = symbol.value.trim().toUpperCase(); v.rerun(); });
  action.addEventListener("change", () => { state.action = action.value; v.rerun(); });
  return v;
}

export { sparkline };
