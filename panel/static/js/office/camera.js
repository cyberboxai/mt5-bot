// Camera maths for the office canvas (pure: no DOM, testable with `node --test`).
// A camera is { x, y, zoom } with (x, y) the world point at the centre of the viewport.

export const MIN_ZOOM = 0.35;
export const MAX_ZOOM = 3;

export function worldToScreen(cam, vp, wx, wy) {
  return [(wx - cam.x) * cam.zoom + vp.w / 2, (wy - cam.y) * cam.zoom + vp.h / 2];
}

export function screenToWorld(cam, vp, sx, sy) {
  return [(sx - vp.w / 2) / cam.zoom + cam.x, (sy - vp.h / 2) / cam.zoom + cam.y];
}

/** Zoom by `factor` keeping the world point under the screen point (sx, sy) fixed. */
export function zoomAt(cam, vp, factor, sx, sy, min = MIN_ZOOM, max = MAX_ZOOM) {
  const zoom = Math.min(max, Math.max(min, cam.zoom * factor));
  const [wx, wy] = screenToWorld(cam, vp, sx, sy);
  return { x: wx - (sx - vp.w / 2) / zoom, y: wy - (sy - vp.h / 2) / zoom, zoom };
}

/** Keep the view over the world: centred when the world is smaller than the view. */
export function clampCamera(cam, vp, world) {
  const halfW = vp.w / 2 / cam.zoom;
  const halfH = vp.h / 2 / cam.zoom;
  const axis = (c, half, size) => (2 * half >= size ? size / 2 : Math.min(size - half, Math.max(half, c)));
  return { x: axis(cam.x, halfW, world.w), y: axis(cam.y, halfH, world.h), zoom: cam.zoom };
}

/** Zoom that shows the whole world inside the viewport (with a margin in pixels). */
export function fitZoom(vp, world, margin = 16) {
  const z = Math.min((vp.w - 2 * margin) / world.w, (vp.h - 2 * margin) / world.h);
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
}

/** One frame of smooth following: exponential approach, frame-rate independent. */
export function followStep(cam, tx, ty, dt, rate = 6) {
  const k = 1 - Math.exp(-rate * Math.max(0, dt));
  return { x: cam.x + (tx - cam.x) * k, y: cam.y + (ty - cam.y) * k, zoom: cam.zoom };
}

/** The world rectangle visible through the camera: [x, y, w, h]. */
export function visibleRect(cam, vp) {
  const w = vp.w / cam.zoom;
  const h = vp.h / cam.zoom;
  return [cam.x - w / 2, cam.y - h / 2, w, h];
}
