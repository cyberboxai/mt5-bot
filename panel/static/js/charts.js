// SVG charts with no library. Specs (dataviz method): 2px lines, bars <= 24px with 4px rounded
// data ends, solid hairline grid, one y axis, crosshair tooltip listing every series, keyboard
// access, legend for 2+ series, a table view and CSV for every chart, honest empty states.
// Colours are CSS custom properties (validated palette); text never wears a series colour.

import { clamp, clear, cssVar, downloadCsv, el, num, svg } from "./util.js";
import { t } from "./i18n.js";

const W = 640;

// ------------------------------------------------------------------- tooltip

let tipNode = null;
function tip() {
  if (!tipNode) tipNode = document.getElementById("tooltip");
  return tipNode;
}

export function showTip(clientX, clientY, title, rows) {
  const node = tip();
  if (!node) return;
  clear(node);
  if (title) node.append(el("div", { class: "tt-title", text: title }));
  for (const row of rows) {
    const key = el("span", { class: "tt-key" });
    if (row.color) key.style.background = row.color;
    node.append(el("div", { class: "tt-row" }, row.color ? key : null, el("strong", { text: row.value }),
      row.label ? el("span", { class: "muted", text: row.label }) : null));
  }
  node.hidden = false;
  const box = node.getBoundingClientRect();
  const x = clientX + 14 + box.width > window.innerWidth ? clientX - box.width - 14 : clientX + 14;
  const y = clamp(clientY - box.height / 2, 6, window.innerHeight - box.height - 6);
  node.style.left = `${x}px`;
  node.style.top = `${y}px`;
}

export function hideTip() { const node = tip(); if (node) node.hidden = true; }

// --------------------------------------------------------------------- scales

function niceTicks(min, max, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) { const d = Math.abs(min) || 1; min -= d * 0.5; max += d * 0.5; }
  const span = max - min;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const norm = step0 / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step * 0.5; v += step) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

function linear(d0, d1, r0, r1) {
  const span = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
}

// ---------------------------------------------------------------------- legend

export function legend(series, shape = "line") {
  if (!series || series.length < 2) return null; // one series: the title already names it
  return el("div", { class: "legend" }, series.map((s) => {
    const key = el("span", { class: `key${shape === "rect" ? " rect" : ""}` });
    key.style.background = s.color;
    return el("span", {}, key, s.name);
  }));
}

// ---------------------------------------------------------------------- card

export function chartCard({ title, sub, rows, csvName, columns, n, min = 2, render }) {
  const body = el("div", { class: "chart-body" });
  const toggle = el("button", { class: "mini-btn", type: "button", text: t("common.table") });
  const csv = el("button", { class: "mini-btn", type: "button", text: t("common.csv") });
  const head = el("div", { class: "card-head" }, el("div", {}, el("h3", { text: title }),
    sub ? el("p", { class: "card-sub", text: sub }) : null), el("div", { class: "card-actions" }, toggle, csv));
  const node = el("section", { class: "card" }, head, body);
  let tableMode = false;
  const enough = (n ?? (rows ? rows.length : 0)) >= min;
  const draw = () => {
    clear(body);
    if (!enough) {
      body.append(el("div", { class: "empty", text: t("common.insufficient", { n: n ?? (rows ? rows.length : 0), min }) }));
      return;
    }
    if (tableMode) body.append(dataTable(rows || [], columns));
    else render(body);
  };
  toggle.addEventListener("click", () => {
    tableMode = !tableMode;
    toggle.textContent = tableMode ? t("common.chart") : t("common.table");
    draw();
  });
  csv.addEventListener("click", () => downloadCsv(csvName || "cyberbox.csv", rows || []));
  csv.disabled = !rows || !rows.length;
  draw();
  return node;
}

export function dataTable(rows, columns) {
  if (!rows.length) return el("div", { class: "empty", text: t("common.nodata") });
  const cols = (columns || Object.keys(rows[0]).map((k) => ({ key: k, label: k }))).map((c) => ({ ...c,
    wrap: c.wrap ?? rows.some((r) => typeof r[c.key] === "string" && r[c.key].length > 32) }));
  return el("div", { class: "table-wrap" }, el("table", { class: "data" },
    el("thead", {}, el("tr", {}, cols.map((c) => el("th", { text: c.label, class: c.num ? "num" : "" })))),
    el("tbody", {}, rows.map((r) => el("tr", {}, cols.map((c) => el("td", {
      class: c.num ? "num" : c.wrap ? "wrap" : "", text: c.format ? c.format(r[c.key], r) : r[c.key] ?? "",
    })))))));
}

// ------------------------------------------------------------------ line chart

export function lineChart(container, { series, height = 220, yFormat = (v) => num(v, 2), xFormat = (v) => String(v),
  zeroLine = false, area = false }) {
  const H = height;
  const pad = { l: 52, r: 64, t: 12, b: 26 };
  const all = series.flatMap((s) => s.points);
  const xs = all.map((p) => p.x);
  const ys = all.map((p) => p.y).filter((v) => Number.isFinite(v));
  let yMin = Math.min(...ys);
  let yMax = Math.max(...ys);
  if (zeroLine) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
  const ticks = niceTicks(yMin, yMax, 4);
  const x = linear(Math.min(...xs), Math.max(...xs), pad.l, W - pad.r);
  const y = linear(ticks[0], ticks[ticks.length - 1], H - pad.b, pad.t);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: 0,
    "aria-label": series.map((s) => s.name).join(", ") });
  for (const v of ticks) {
    root.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: v === 0 && zeroLine ? "axis-line" : "grid-line" }));
    const label = svg("text", { x: pad.l - 6, y: y(v) + 4, "text-anchor": "end" });
    label.textContent = yFormat(v);
    root.append(label);
  }
  const xTicks = 5;
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  for (let i = 0; i <= xTicks; i += 1) {
    const xv = x0 + ((x1 - x0) * i) / xTicks;
    const label = svg("text", { x: x(xv), y: H - 8, "text-anchor": i === 0 ? "start" : i === xTicks ? "end" : "middle" });
    label.textContent = xFormat(xv);
    root.append(label);
  }
  root.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: H - pad.b, y2: H - pad.b, class: "axis-line" }));
  for (const s of series) {
    if (!s.points.length) continue;
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
    if (area && series.length === 1) {
      const fill = svg("path", { d: `${d}L${x(s.points[s.points.length - 1].x)},${y(Math.max(ticks[0], 0))}L${x(s.points[0].x)},${y(Math.max(ticks[0], 0))}Z` });
      fill.style.fill = s.color;
      fill.style.opacity = "0.1";
      root.append(fill);
    }
    const path = svg("path", { d, class: "series-line" });
    path.style.stroke = s.color;
    root.append(path);
    const last = s.points[s.points.length - 1];
    const dot = svg("circle", { cx: x(last.x), cy: y(last.y), r: 4 });
    dot.style.fill = s.color;
    dot.style.stroke = "var(--surface-1)";
    dot.style.strokeWidth = "2";
    root.append(dot);
    const end = svg("text", { x: x(last.x) + 8, y: y(last.y) + 4, class: "end-label" });
    end.textContent = yFormat(last.y);
    root.append(end);
  }
  const cross = svg("line", { y1: pad.t, y2: H - pad.b, class: "crosshair", visibility: "hidden" });
  root.append(cross);
  const hit = svg("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.t - pad.b, fill: "transparent" });
  root.append(hit);
  const longest = series.reduce((a, s) => (s.points.length > a.points.length ? s : a), series[0]);
  let index = longest.points.length - 1;
  const showAt = (i, cx, cy) => {
    index = clamp(i, 0, longest.points.length - 1);
    const px = longest.points[index].x;
    cross.setAttribute("x1", x(px));
    cross.setAttribute("x2", x(px));
    cross.setAttribute("visibility", "visible");
    const rows = series.map((s) => {
      const p = s.points.reduce((best, q) => (Math.abs(q.x - px) < Math.abs(best.x - px) ? q : best), s.points[0]);
      return { color: s.color, value: yFormat(p.y), label: s.name };
    });
    const rect = root.getBoundingClientRect();
    showTip(cx ?? rect.left + (x(px) / W) * rect.width, cy ?? rect.top + rect.height / 2, xFormat(px), rows);
  };
  hit.addEventListener("pointermove", (ev) => {
    const rect = root.getBoundingClientRect();
    const vx = ((ev.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 0; i < longest.points.length; i += 1) {
      if (Math.abs(x(longest.points[i].x) - vx) < Math.abs(x(longest.points[best].x) - vx)) best = i;
    }
    showAt(best, ev.clientX, ev.clientY);
  });
  const hide = () => { cross.setAttribute("visibility", "hidden"); hideTip(); };
  hit.addEventListener("pointerleave", hide);
  root.addEventListener("blur", hide);
  root.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") { showAt(index - 1); ev.preventDefault(); }
    if (ev.key === "ArrowRight") { showAt(index + 1); ev.preventDefault(); }
    if (ev.key === "Escape") hide();
  });
  root.addEventListener("focus", () => showAt(index));
  const l = legend(series);
  if (l) container.append(l);
  container.append(el("div", { class: "chart" }, root));
}

// ------------------------------------------------------------------- bar chart

function barPath(x0, width, yBase, yTop) {
  const r = Math.min(4, width / 2, Math.abs(yBase - yTop));
  if (yTop <= yBase) { // positive: rounded top, square base
    return `M${x0},${yBase}V${yTop + r}Q${x0},${yTop} ${x0 + r},${yTop}H${x0 + width - r}Q${x0 + width},${yTop} ${x0 + width},${yTop + r}V${yBase}Z`;
  }
  return `M${x0},${yBase}V${yTop - r}Q${x0},${yTop} ${x0 + r},${yTop}H${x0 + width - r}Q${x0 + width},${yTop} ${x0 + width},${yTop - r}V${yBase}Z`;
}

export function barChart(container, { bars, height = 200, yFormat = (v) => num(v, 0), color = "var(--series-1)",
  title = "" }) {
  const H = height;
  const pad = { l: 46, r: 12, t: 14, b: 34 };
  const values = bars.map((b) => b.value);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 4);
  const y = linear(ticks[0], ticks[ticks.length - 1], H - pad.b, pad.t);
  const slot = (W - pad.l - pad.r) / Math.max(1, bars.length);
  const width = Math.min(24, Math.max(4, slot - 2));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  for (const v of ticks) {
    root.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: v === 0 ? "axis-line" : "grid-line" }));
    const label = svg("text", { x: pad.l - 6, y: y(v) + 4, "text-anchor": "end" });
    label.textContent = yFormat(v);
    root.append(label);
  }
  const labelEvery = Math.ceil(bars.length / 12);
  bars.forEach((b, i) => {
    const cx = pad.l + slot * i + slot / 2;
    const path = svg("path", { d: barPath(cx - width / 2, width, y(0), y(b.value)), class: "mark", tabindex: 0,
      "aria-label": `${b.label}: ${yFormat(b.value)}` });
    path.style.fill = b.color || color;
    const show = (ev) => {
      const rect = path.getBoundingClientRect();
      showTip(ev && ev.clientX ? ev.clientX : rect.right, ev && ev.clientY ? ev.clientY : rect.top, b.label,
        [{ value: yFormat(b.value), label: b.note || "" }]);
    };
    path.addEventListener("pointermove", show);
    path.addEventListener("focus", () => show());
    path.addEventListener("pointerleave", hideTip);
    path.addEventListener("blur", hideTip);
    root.append(path);
    if (i % labelEvery === 0) {
      const label = svg("text", { x: cx, y: H - pad.b + 14, "text-anchor": "middle" });
      label.textContent = b.label;
      root.append(label);
    }
  });
  container.append(el("div", { class: "chart" }, root));
}

// --------------------------------------------------------------------- heatmap

function hexToRgb(hex) {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
}

function mix(a, b, tval) {
  const [r1, g1, b1] = hexToRgb(a);
  const [r2, g2, b2] = hexToRgb(b);
  const f = clamp(tval, 0, 1);
  return `rgb(${Math.round(r1 + (r2 - r1) * f)},${Math.round(g1 + (g2 - g1) * f)},${Math.round(b1 + (b2 - b1) * f)})`;
}

export function scaleColor(value, scale, domain) {
  if (value === null || value === undefined || Number.isNaN(value)) return cssVar("--surface-3");
  if (scale === "diverging") {
    const m = Math.max(Math.abs(domain[0]), Math.abs(domain[1])) || 1;
    const f = value / m;
    return f < 0 ? mix(cssVar("--div-mid"), cssVar("--div-neg"), -f) : mix(cssVar("--div-mid"), cssVar("--div-pos"), f);
  }
  const span = domain[1] - domain[0] || 1;
  return mix(cssVar("--seq-low"), cssVar("--seq-high"), (value - domain[0]) / span);
}

export function heatmap(container, { rows, cols, values, scale = "sequential", format = (v) => num(v, 2),
  title = "", cell = 34 }) {
  const flat = values.flat().filter((v) => v !== null && v !== undefined && Number.isFinite(v));
  const domain = scale === "diverging" ? [-Math.max(...flat.map(Math.abs), 1e-9), Math.max(...flat.map(Math.abs), 1e-9)]
    : [Math.min(...flat), Math.max(...flat)];
  const padL = 96;
  const padT = 22;
  const cw = Math.max(18, Math.min(cell * 2, (W - padL - 8) / Math.max(1, cols.length)));
  const ch = 26;
  const H = padT + rows.length * (ch + 2) + 26;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  cols.forEach((c, j) => {
    const label = svg("text", { x: padL + j * (cw + 2) + cw / 2, y: 14, "text-anchor": "middle" });
    label.textContent = c;
    root.append(label);
  });
  rows.forEach((r, i) => {
    const label = svg("text", { x: padL - 6, y: padT + i * (ch + 2) + ch / 2 + 4, "text-anchor": "end" });
    label.textContent = r;
    root.append(label);
    cols.forEach((c, j) => {
      const v = values[i][j];
      const rect = svg("rect", { x: padL + j * (cw + 2), y: padT + i * (ch + 2), width: cw, height: ch, rx: 3,
        class: "mark", tabindex: 0, "aria-label": `${r} ${c}: ${v === null || v === undefined ? "n/d" : format(v)}` });
      rect.style.fill = scaleColor(v, scale, domain);
      const show = (ev) => {
        const box = rect.getBoundingClientRect();
        showTip(ev && ev.clientX ? ev.clientX : box.right, ev && ev.clientY ? ev.clientY : box.top, `${r} · ${c}`,
          [{ value: v === null || v === undefined ? "n/d" : format(v) }]);
      };
      rect.addEventListener("pointermove", show);
      rect.addEventListener("focus", () => show());
      rect.addEventListener("pointerleave", hideTip);
      rect.addEventListener("blur", hideTip);
      root.append(rect);
    });
  });
  const scaleNote = svg("text", { x: padL, y: H - 6 });
  scaleNote.textContent = scale === "diverging" ? `${format(domain[0])} … 0 … ${format(domain[1])}`
    : `${format(domain[0])} … ${format(domain[1])}`;
  root.append(scaleNote);
  container.append(el("div", { class: "chart" }, root));
}

// ----------------------------------------------------------------- meter, spark

export function meter({ label, value, max, text, note, warnAt = 0.7, critAt = 0.9 }) {
  const ratio = max > 0 && value !== null && value !== undefined ? clamp(value / max, 0, 1) : 0;
  const fill = el("div", { class: `fill${ratio >= critAt ? " critical" : ratio >= warnAt ? " warning" : ""}` });
  fill.style.width = `${(ratio * 100).toFixed(1)}%`;
  return el("div", { class: "meter", role: "meter", "aria-valuemin": 0, "aria-valuemax": max, "aria-valuenow": value ?? 0,
    "aria-label": label },
  el("div", { class: "meter-head" }, el("span", { text: label }), el("strong", { text: text ?? `${num(value, 2)} / ${num(max, 2)}` })),
  el("div", { class: "track" }, fill), note ? el("div", { class: "meter-note", text: note }) : null);
}

export function sparkline(values, color = "var(--series-1)", height = 36) {
  const vals = values.filter((v) => Number.isFinite(v));
  const root = svg("svg", { viewBox: `0 0 120 ${height}`, role: "img", "aria-hidden": "true" });
  if (vals.length < 2) return root;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const x = linear(0, vals.length - 1, 2, 118);
  const y = linear(lo, hi === lo ? lo + 1 : hi, height - 3, 3);
  const path = svg("path", { d: vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""),
    class: "series-line" });
  path.style.stroke = color;
  path.style.fill = "none";
  path.style.strokeWidth = "2";
  root.append(path);
  return el("div", { class: "chart" }, root);
}
