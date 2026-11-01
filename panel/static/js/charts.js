// SVG charts with no library. Specs (dataviz method): 2px lines, bars <= 24px with 4px rounded
// data ends, solid hairline grid, one y axis, crosshair tooltip listing every series, keyboard
// access, legend for 2+ series, a table view and CSV for every chart, honest empty states.
// Colours are CSS custom properties (validated palette); text never wears a series colour.
// Charts draw at their container's real pixel width (re-drawn on resize), so axis text keeps
// its size on phones instead of being scaled down with the whole picture.

import { clamp, clear, cssVar, downloadCsv, el, num, svg } from "./util.js";
import { t } from "./i18n.js";

const W = 640; // fallback width before the container is laid out
let gradientSeq = 0;

// Mounts a chart that re-draws at its real width. draw(width) returns the <svg>.
function fitted(container, draw) {
  const wrap = el("div", { class: "chart" });
  container.append(wrap);
  let drawn = 0;
  const render = (width) => {
    drawn = width;
    clear(wrap);
    wrap.append(draw(width));
  };
  render(Math.round(container.clientWidth) || W);
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => {
      if (!wrap.isConnected) { ro.disconnect(); return; }
      const width = Math.round(wrap.clientWidth);
      if (width > 0 && Math.abs(width - drawn) > 4) render(width);
    });
    ro.observe(wrap);
  }
  return wrap;
}

// Labels for evenly spaced x positions, skipping a label equal to the one before it (a one-day
// series formatted by date would otherwise print the same date five times).
function xTickLabels(x0, x1, count, format) {
  const out = [];
  let previous = null;
  for (let i = 0; i <= count; i += 1) {
    const value = count ? x0 + ((x1 - x0) * i) / count : x0;
    const text = format(value);
    if (text !== previous) out.push({ value, text, i });
    previous = text;
  }
  return out;
}

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
  const toggle = el("button", { class: "mini-btn", type: "button", text: t("common.table"), "aria-pressed": "false" });
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
    toggle.setAttribute("aria-pressed", String(tableMode));
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
    num: c.num ?? isNumericColumn(rows, c.key),
    wrap: c.wrap ?? rows.some((r) => typeof r[c.key] === "string" && r[c.key].length > 32) }));
  return el("div", { class: "table-wrap" }, el("table", { class: "data" },
    el("thead", {}, el("tr", {}, cols.map((c) => el("th", { text: c.label, class: c.num ? "num" : "" })))),
    el("tbody", {}, rows.map((r) => el("tr", {}, cols.map((c) => el("td", { class: c.num ? "num" : c.wrap ? "wrap" : "" },
      c.format ? c.format(r[c.key], r) : String(r[c.key] ?? ""))))))));
}

// A column is numeric (right-aligned, monospaced) when every filled cell is a number or a
// formatted number such as "-2,86", "+0,50 R", "12%" or "n/d".
const NUMERIC = /^(n\/d|[+\-−]?[\d\s.,]+\s?(%|R|ms)?)$/;
function isNumericColumn(rows, key) {
  let seen = 0;
  for (const r of rows) {
    const v = r[key];
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "number") { seen += 1; continue; }
    if (typeof v !== "string" || !NUMERIC.test(v.trim())) return false;
    if (v !== "n/d") seen += 1;
  }
  return seen > 0;
}

// ------------------------------------------------------------------ line chart

export function lineChart(container, { series, height = 220, yFormat = null, xFormat = (v) => String(v),
  xTip = null, zeroLine = false, area = false, refLine = null }) {
  const tipTitle = xTip || xFormat;
  const valueFormat = yFormat || ((v) => num(v, 2));
  const drawn = series.filter((s) => s.points.length);
  const all = drawn.flatMap((s) => s.points);
  const xs = all.map((p) => p.x);
  const ys = all.map((p) => p.y).filter((v) => Number.isFinite(v));
  let yMin = Math.min(...ys);
  let yMax = Math.max(...ys);
  if (zeroLine) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
  if (refLine && Number.isFinite(refLine.value)) { yMin = Math.min(yMin, refLine.value); yMax = Math.max(yMax, refLine.value); }
  const ticks = niceTicks(yMin, yMax, height < 180 ? 3 : 4);
  // Without an explicit format, axis labels carry only the decimals the tick step needs.
  const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
  const tickFormat = yFormat || ((v) => num(v, step >= 1 ? 0 : step >= 0.1 ? 1 : 2));
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const longest = drawn.reduce((acc, s) => (s.points.length > acc.points.length ? s : acc), drawn[0] || { points: [] });
  let index = longest.points.length - 1;
  let hideTipNow = () => {};
  const l = legend(series);
  if (l) container.append(l);
  fitted(container, (width) => {
    const H = height;
    const yLabelWidth = Math.max(...ticks.map((v) => tickFormat(v).length)) * 6.6 + 10;
    const endWidth = Math.max(...drawn.map((s) => valueFormat(s.points[s.points.length - 1].y).length)) * 6.6 + 20;
    const pad = { l: Math.max(36, yLabelWidth), r: Math.max(48, endWidth), t: 12, b: 26 };
    const x = linear(x0, x1, pad.l, width - pad.r);
    const y = linear(ticks[0], ticks[ticks.length - 1], H - pad.b, pad.t);
    const root = svg("svg", { viewBox: `0 0 ${width} ${H}`, width, height: H, role: "img", tabindex: 0,
      "aria-label": series.map((s) => s.name).join(", ") });
    const defs = svg("defs");
    root.append(defs);
    for (const v of ticks) {
      root.append(svg("line", { x1: pad.l, x2: width - pad.r, y1: y(v), y2: y(v), class: v === 0 && zeroLine ? "axis-line" : "grid-line" }));
      const label = svg("text", { x: pad.l - 8, y: y(v) + 3.5, "text-anchor": "end" });
      label.textContent = tickFormat(v);
      root.append(label);
    }
    const count = Math.max(1, Math.min(6, Math.floor((width - pad.l - pad.r) / 110)));
    for (const tick of xTickLabels(x0, x1, x0 === x1 ? 0 : count, xFormat)) {
      const anchor = tick.i === 0 ? "start" : tick.i === count ? "end" : "middle";
      const label = svg("text", { x: x(tick.value), y: H - 7, "text-anchor": anchor });
      label.textContent = tick.text;
      root.append(label);
    }
    root.append(svg("line", { x1: pad.l, x2: width - pad.r, y1: H - pad.b, y2: H - pad.b, class: "axis-line" }));
    if (refLine && Number.isFinite(refLine.value)) {
      root.append(svg("line", { x1: pad.l, x2: width - pad.r, y1: y(refLine.value), y2: y(refLine.value), class: "ref-line" }));
      if (refLine.label) {
        const label = svg("text", { x: pad.l + 4, y: y(refLine.value) - 5 });
        label.textContent = refLine.label;
        root.append(label);
      }
    }
    const ends = [];
    // Secondary (dashed) series go underneath so they never hatch over the main line.
    [...drawn].sort((p, q) => Number(Boolean(q.secondary)) - Number(Boolean(p.secondary))).forEach((s) => {
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
      // Area under the first (main) series: a soft vertical fade toward the baseline.
      if (area && s === drawn[0]) {
        const id = `cbx-area-${(gradientSeq += 1)}`;
        const grad = svg("linearGradient", { id, x1: 0, x2: 0, y1: 0, y2: 1 });
        const top = svg("stop", { offset: "0%" });
        top.style.stopColor = s.color;
        top.style.stopOpacity = "var(--area-top)";
        const bottom = svg("stop", { offset: "100%" });
        bottom.style.stopColor = s.color;
        bottom.style.stopOpacity = "0";
        grad.append(top, bottom);
        defs.append(grad);
        const base = y(zeroLine ? 0 : ticks[0]);
        const fill = svg("path", { d: `${d}L${x(s.points[s.points.length - 1].x).toFixed(1)},${base}L${x(s.points[0].x).toFixed(1)},${base}Z`,
          fill: `url(#${id})` });
        root.append(fill);
      }
      const path = svg("path", { d, class: `series-line${s.secondary ? " secondary" : ""}` });
      path.style.stroke = s.color;
      root.append(path);
      const last = s.points[s.points.length - 1];
      ends.push({ s, cx: x(last.x), cy: y(last.y), text: valueFormat(last.y) });
    });
    // End labels: value of each series at its last point, nudged apart so they never overlap.
    ends.sort((p, q) => p.cy - q.cy);
    for (let i = 1; i < ends.length; i += 1) ends[i].ly = Math.max(ends[i].cy, (ends[i - 1].ly ?? ends[i - 1].cy) + 16);
    for (const e of ends) {
      const dot = svg("circle", { cx: e.cx, cy: e.cy, r: 3.5, class: "hover-dot" });
      dot.style.fill = e.s.color;
      root.append(dot);
      const ly = clamp(e.ly ?? e.cy, pad.t + 4, H - pad.b - 4);
      const pillW = e.text.length * 6.6 + 10;
      root.append(svg("rect", { x: e.cx + 8, y: ly - 9, width: pillW, height: 18, rx: 4, class: "end-pill" }));
      const end = svg("text", { x: e.cx + 13, y: ly + 3.5, class: "end-label" });
      end.textContent = e.text;
      root.append(end);
    }
    const cross = svg("line", { y1: pad.t, y2: H - pad.b, class: "crosshair", visibility: "hidden" });
    root.append(cross);
    const dots = drawn.map((s) => {
      const dot = svg("circle", { r: 4, class: "hover-dot", visibility: "hidden" });
      dot.style.fill = s.color;
      root.append(dot);
      return dot;
    });
    const hit = svg("rect", { x: pad.l, y: pad.t, width: Math.max(0, width - pad.l - pad.r), height: H - pad.t - pad.b, fill: "transparent" });
    root.append(hit);
    const nearest = (s, px) => s.points.reduce((best, q) => (Math.abs(q.x - px) < Math.abs(best.x - px) ? q : best), s.points[0]);
    const showAt = (i, cx, cy) => {
      if (!longest.points.length) return;
      index = clamp(i, 0, longest.points.length - 1);
      const px = longest.points[index].x;
      cross.setAttribute("x1", x(px));
      cross.setAttribute("x2", x(px));
      cross.setAttribute("visibility", "visible");
      const rows = drawn.map((s, k) => {
        const p = nearest(s, px);
        dots[k].setAttribute("cx", x(p.x));
        dots[k].setAttribute("cy", y(p.y));
        dots[k].setAttribute("visibility", "visible");
        return { color: s.color, value: valueFormat(p.y), label: s.name };
      });
      const rect = root.getBoundingClientRect();
      showTip(cx ?? rect.left + (x(px) / width) * rect.width, cy ?? rect.top + rect.height / 2, tipTitle(px), rows);
    };
    hit.addEventListener("pointermove", (ev) => {
      const rect = root.getBoundingClientRect();
      const vx = ((ev.clientX - rect.left) / rect.width) * width;
      let best = 0;
      for (let i = 0; i < longest.points.length; i += 1) {
        if (Math.abs(x(longest.points[i].x) - vx) < Math.abs(x(longest.points[best].x) - vx)) best = i;
      }
      showAt(best, ev.clientX, ev.clientY);
    });
    const hide = () => {
      cross.setAttribute("visibility", "hidden");
      for (const dot of dots) dot.setAttribute("visibility", "hidden");
      hideTip();
    };
    hideTipNow = hide;
    hit.addEventListener("pointerleave", hide);
    root.addEventListener("blur", hide);
    root.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowLeft") { showAt(index - 1); ev.preventDefault(); }
      if (ev.key === "ArrowRight") { showAt(index + 1); ev.preventDefault(); }
      if (ev.key === "Home") { showAt(0); ev.preventDefault(); }
      if (ev.key === "End") { showAt(longest.points.length - 1); ev.preventDefault(); }
      if (ev.key === "Escape") hide();
    });
    root.addEventListener("focus", () => showAt(index));
    return root;
  });
  return { hide: () => hideTipNow() };
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
  const values = bars.map((b) => b.value);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 4);
  fitted(container, (width) => {
    const H = height;
    const yLabelWidth = Math.max(...ticks.map((v) => yFormat(v).length)) * 6.6 + 10;
    const pad = { l: Math.max(34, yLabelWidth), r: 8, t: 14, b: 30 };
    const y = linear(ticks[0], ticks[ticks.length - 1], H - pad.b, pad.t);
    const slot = (width - pad.l - pad.r) / Math.max(1, bars.length);
    const barWidth = Math.min(24, Math.max(3, slot * 0.68));
    const root = svg("svg", { viewBox: `0 0 ${width} ${H}`, width, height: H, role: "img", "aria-label": title });
    for (const v of ticks) {
      root.append(svg("line", { x1: pad.l, x2: width - pad.r, y1: y(v), y2: y(v), class: v === 0 ? "axis-line" : "grid-line" }));
      const label = svg("text", { x: pad.l - 8, y: y(v) + 3.5, "text-anchor": "end" });
      label.textContent = yFormat(v);
      root.append(label);
    }
    const longestLabel = Math.max(1, ...bars.map((b) => String(b.label).length));
    const labelEvery = Math.max(1, Math.ceil((longestLabel * 6.6 + 10) / Math.max(1, slot)));
    bars.forEach((b, i) => {
      const cx = pad.l + slot * i + slot / 2;
      const path = svg("path", { d: barPath(cx - barWidth / 2, barWidth, y(0), y(b.value)), class: "mark", tabindex: 0,
        "aria-label": `${b.label}: ${yFormat(b.value)}` });
      path.style.fill = b.color || color;
      const show = (ev) => {
        const rect = path.getBoundingClientRect();
        showTip(ev && ev.clientX ? ev.clientX : rect.right, ev && ev.clientY ? ev.clientY : rect.top, b.label,
          [{ color: b.color || color, value: yFormat(b.value), label: b.note || "" }]);
      };
      path.addEventListener("pointermove", show);
      path.addEventListener("focus", () => show());
      path.addEventListener("pointerleave", hideTip);
      path.addEventListener("blur", hideTip);
      root.append(path);
      if (i % labelEvery === 0) {
        const label = svg("text", { x: cx, y: H - pad.b + 16, "text-anchor": "middle" });
        label.textContent = b.label;
        root.append(label);
      }
    });
    return root;
  });
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
  fitted(container, (width) => {
    const padL = Math.min(96, Math.max(48, Math.max(...rows.map((r) => String(r).length)) * 6.6 + 10));
    const padT = 22;
    const cw = Math.max(14, Math.min(cell * 2, (width - padL - 8) / Math.max(1, cols.length) - 2));
    const ch = 26;
    const H = padT + rows.length * (ch + 2) + 26;
    const root = svg("svg", { viewBox: `0 0 ${width} ${H}`, width, height: H, role: "img", "aria-label": title });
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
    return root;
  });
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

export function sparkline(values, color = "var(--series-1)", height = 40) {
  const vals = values.filter((v) => Number.isFinite(v));
  const root = svg("svg", { viewBox: `0 0 120 ${height}`, preserveAspectRatio: "none", role: "img", "aria-hidden": "true" });
  if (vals.length < 2) return root;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const x = linear(0, vals.length - 1, 1, 119);
  const y = linear(lo, hi === lo ? lo + 1 : hi, height - 2, 3);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const fill = svg("path", { d: `${d}L119,${height}L1,${height}Z`, class: "area" });
  fill.style.fill = color;
  const path = svg("path", { d, class: "series-line", "vector-effect": "non-scaling-stroke" });
  path.style.stroke = color;
  path.style.fill = "none";
  path.style.strokeWidth = "1.75";
  root.append(fill, path);
  return el("div", { class: "spark" }, root);
}
