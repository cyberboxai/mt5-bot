// REST + live stream. The stream uses EventSource (which resumes with Last-Event-ID); when
// it keeps failing it backs off and falls back to polling /api/events, so the office never
// silently freezes.

export async function getJSON(path, timeoutMs = 12000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(path, { signal: ctrl.signal, credentials: "same-origin", headers: { Accept: "application/json" } });
    if (res.status === 401) { window.location.href = "/login"; throw new Error("auth"); }
    if (!res.ok) throw new Error(`${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function postControl(action, token) {
  const { csrf } = await getJSON("/api/csrf");
  const res = await fetch(`/api/control/${encodeURIComponent(action)}`, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, Authorization: `Bearer ${token}` },
    body: JSON.stringify({ confirm: true }),
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, body };
}

const listeners = new Set();
let source = null;
let lastId = 0;
let failures = 0;
let pollTimer = null;
let status = "connecting";
const statusListeners = new Set();

function setStatus(value) {
  status = value;
  for (const fn of statusListeners) fn(value);
}

export function onStreamStatus(fn) { statusListeners.add(fn); fn(status); return () => statusListeners.delete(fn); }
export function onEvent(fn) { listeners.add(fn); return () => listeners.delete(fn); }

function dispatch(event) {
  if (event && event.id > lastId) lastId = event.id;
  for (const fn of listeners) {
    try { fn(event); } catch (e) { console.warn("event listener failed", e); }
  }
}

function startPolling() {
  if (pollTimer) return;
  setStatus("polling");
  const tick = async () => {
    try {
      const events = await getJSON(`/api/events?after=${lastId}&limit=500`);
      events.forEach(dispatch);
    } catch (e) { /* keep trying */ }
  };
  tick();
  pollTimer = setInterval(tick, 3000);
}

export function startStream() {
  if (!("EventSource" in window)) { startPolling(); return; }
  if (source) return;
  source = new EventSource(lastId ? `/api/stream?after=${lastId}` : "/api/stream");
  source.addEventListener("open", () => { failures = 0; setStatus("live"); });
  source.addEventListener("office", (msg) => {
    try { dispatch(JSON.parse(msg.data)); } catch (e) { console.warn("bad event", e); }
  });
  source.addEventListener("error", () => {
    failures += 1;
    setStatus("reconnecting");
    if (source && source.readyState === EventSource.CLOSED) {
      source = null;
      const delay = Math.min(30000, 1000 * 2 ** Math.min(failures, 5));
      if (failures > 6) startPolling();
      else setTimeout(startStream, delay);
    }
  });
}

export function lastEventId() { return lastId; }
export function seedLastEventId(id) { if (id > lastId) lastId = id; }
