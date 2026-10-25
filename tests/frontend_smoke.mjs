// Headless smoke test of the CYBERBOX panel over the Chrome DevTools Protocol (Node >= 22,
// no npm packages): loads every tab, fails on any console error or uncaught exception,
// checks the title and saves screenshots. Usage: node frontend_smoke.mjs <url> <outDir> [chrome]
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url, outDir, chromeBin = "google-chrome"] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });
const TABS = ["office", "overview", "performance", "trades", "team", "strategies", "risk", "markets", "advisor", "health", "decisions"];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const step = (text) => process.stderr.write(`[smoke] ${text}\n`);

const profile = mkdtempSync(join(tmpdir(), "cbx-chrome-"));
const chrome = spawn(chromeBin, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--no-first-run",
  "--no-default-browser-check", "--disable-gpu", "--window-size=1440,1000", "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
process.on("exit", () => { try { chrome.kill("SIGKILL"); } catch (e) { /* already gone */ } });
process.on("unhandledRejection", (err) => { console.error(`[smoke] ${err}`); process.exit(2); });
const wsUrl = await new Promise((resolve, reject) => {
  let buf = "";
  const timer = setTimeout(() => reject(new Error("chrome did not start")), 20000);
  chrome.stderr.on("data", (d) => {
    buf += d.toString();
    const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
    if (m) { clearTimeout(timer); resolve(m[1]); }
  });
});

const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener("open", r));
let seq = 0;
const pending = new Map();
const errors = [];
let sessionId = null;
ws.addEventListener("message", (msg) => {
  const data = JSON.parse(msg.data);
  if (data.id && pending.has(data.id)) { pending.get(data.id)(data); pending.delete(data.id); return; }
  if (data.method === "Runtime.exceptionThrown") errors.push(`exception: ${data.params.exceptionDetails.exception?.description || data.params.exceptionDetails.text}`);
  if (data.method === "Runtime.consoleAPICalled" && (data.params.type === "error" || data.params.type === "assert")) {
    errors.push(`console.${data.params.type}: ${data.params.args.map((a) => a.value ?? a.description).join(" ")}`);
  }
  if (data.method === "Log.entryAdded" && data.params.entry.level === "error") errors.push(`log: ${data.params.entry.text} ${data.params.entry.url || ""}`);
});
function send(method, params = {}, session = sessionId) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30000);
    pending.set(id, (data) => { clearTimeout(timer); resolve(data); });
  });
}

const target = await send("Target.createTarget", { url: "about:blank" }, null);
const attached = await send("Target.attachToTarget", { targetId: target.result.targetId, flatten: true }, null);
sessionId = attached.result.sessionId;
await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

async function evaluate(expression) {
  const res = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  return res.result?.result?.value;
}
async function shot(name, fullPage = true) {
  let clip;
  if (fullPage) {
    const metrics = await send("Page.getLayoutMetrics");
    const size = metrics.result.cssContentSize || metrics.result.contentSize;
    clip = { x: 0, y: 0, width: Math.min(size.width, 1440), height: Math.min(size.height, 4000), scale: 1 };
  }
  const res = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, ...(clip ? { clip } : {}) });
  writeFileSync(join(outDir, `${name}.png`), Buffer.from(res.result.data, "base64"));
}

const report = { url, title: null, tabs: {}, errors };
step("navigating");
await send("Page.navigate", { url: `${url}#/office` });
await sleep(7000);
report.title = await evaluate("document.title");
await shot("office", false);
for (const tab of TABS) {
  step(`tab ${tab}`);
  await evaluate(`window.location.hash = "#/${tab}"`);
  await sleep(tab === "office" ? 5000 : 3500);
  const info = await evaluate(`({ title: document.querySelector("#view .view-title")?.textContent || "",
    cards: document.querySelectorAll("#view .card, #view .tile").length,
    selected: document.querySelector('.tab[aria-selected="true"]')?.id || "",
    text: document.querySelector("#view")?.innerText.length || 0 })`);
  report.tabs[tab] = info;
  await shot(`tab_${tab}`, tab !== "office");
}
await evaluate(`window.location.hash = "#/office"`);
for (let i = 0; i < 20 && !(await evaluate(`!!document.querySelector(".agent-btn")`)); i += 1) await sleep(500);
await evaluate(`document.querySelector(".agent-btn")?.click()`);
for (let i = 0; i < 20 && !(await evaluate(`document.getElementById("drawer-title").textContent !== "" &&
  !document.getElementById("drawer-title").textContent.startsWith("A carregar")`)); i += 1) await sleep(250);
report.drawer = await evaluate(`!document.getElementById("drawer").hidden && document.getElementById("drawer-title").textContent`);
await shot("agent_drawer", false);
await evaluate(`document.getElementById("drawer-close").click(); document.getElementById("theme-btn").click(); window.location.hash = "#/overview"`);
await sleep(3500);
await shot("overview_light");
await evaluate(`document.getElementById("theme-btn").click()`);
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await evaluate(`window.location.hash = "#/office"`);
await sleep(4000);
report.mobile_overflow = await evaluate("document.documentElement.scrollWidth > window.innerWidth + 1");
await shot("office_mobile", false);
writeFileSync(join(outDir, "report.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
ws.close();
chrome.kill();
const failed = errors.length || report.title !== "CYBERBOX Trading Office" || !report.drawer || report.mobile_overflow
  || TABS.some((tab) => !report.tabs[tab] || !report.tabs[tab].title || report.tabs[tab].selected !== `tab-${tab}`);
process.exit(failed ? 1 : 0);
