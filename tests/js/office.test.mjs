// Unit tests of the office engine's pure modules: `node --test tests/js/` (Node >= 18, no npm).
import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_ZOOM, MIN_ZOOM, clampCamera, fitZoom, followStep, screenToWorld, visibleRect, worldToScreen, zoomAt }
  from "../../panel/static/js/office/camera.js";
import { COLS, DOOR, FLOOR, ROOMS, ROWS, buildMap, roomOf, worldSize } from "../../panel/static/js/office/map.js";
import { compress, defaultPassable, findPath, octile, pathLength } from "../../panel/static/js/office/pathfind.js";

// The roster as the server sends it (id + home room); kept in step by tests/test_panel.py.
const ROSTER = [
  ["data_clerk", "ops_room"], ["regime_detector", "trading_floor"], ["session_analyst", "newsroom"],
  ["news_sentinel", "newsroom"], ["correlation_analyst", "risk_office"], ["trend_analyst", "trading_floor"],
  ["mean_reversion_analyst", "trading_floor"], ["breakout_analyst", "trading_floor"], ["mtf_analyst", "trading_floor"],
  ["meta_labeler", "research_lab"], ["risk_manager", "risk_office"], ["guardian", "risk_office"],
  ["execution_trader", "execution_desk"], ["position_manager", "execution_desk"], ["quant_researcher", "research_lab"],
  ["validation_auditor", "research_lab"], ["performance_analyst", "analytics"], ["reporter", "analytics"],
  ["ops_engineer", "ops_room"], ["claude_strategist", "advisor_booth"], ["flow_doctor", "analytics"], ["cio", "boardroom"],
].map(([id, room]) => ({ id, room }));

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 2 ** 32; };
}

function bfsLength(grid, cols, rows, start, goal) {
  // Dijkstra with the same move rules, as an independent oracle for optimality.
  const n = cols * rows;
  const dist = new Float64Array(n).fill(Infinity);
  const done = new Uint8Array(n);
  const free = (x, y) => x >= 0 && y >= 0 && x < cols && y < rows && defaultPassable(grid[y * cols + x]);
  dist[start[1] * cols + start[0]] = 0;
  for (;;) {
    let best = -1;
    for (let i = 0; i < n; i += 1) if (!done[i] && dist[i] < Infinity && (best < 0 || dist[i] < dist[best])) best = i;
    if (best < 0) return Infinity;
    if (best === goal[1] * cols + goal[0]) return dist[best];
    done[best] = 1;
    const x = best % cols;
    const y = (best - x) / cols;
    for (let dx = -1; dx <= 1; dx += 1) for (let dy = -1; dy <= 1; dy += 1) {
      if ((!dx && !dy) || !free(x + dx, y + dy)) continue;
      if (dx && dy && (!free(x + dx, y) || !free(x, y + dy))) continue;
      const i = (y + dy) * cols + x + dx;
      dist[i] = Math.min(dist[i], dist[best] + (dx && dy ? Math.SQRT2 : 1));
    }
  }
}

// ------------------------------------------------------------------ pathfinding

test("straight corridor path", () => {
  const grid = new Uint8Array(10 * 3).fill(0);
  const path = findPath(grid, 10, 3, [0, 1], [9, 1]);
  assert.equal(path.length, 10);
  assert.deepEqual(path[0], [0, 1]);
  assert.deepEqual(path.at(-1), [9, 1]);
  assert.deepEqual(compress(path), [[0, 1], [9, 1]]);
});

test("routes around a wall and never cuts corners", () => {
  const cols = 7;
  const rows = 5;
  const grid = new Uint8Array(cols * rows);
  for (let y = 0; y < 4; y += 1) grid[y * cols + 3] = 1; // wall with a gap at the bottom
  const path = findPath(grid, cols, rows, [0, 0], [6, 0]);
  assert.ok(path, "a path exists through the gap");
  assert.ok(path.some(([x, y]) => x === 3 && y === 4), "it goes through the gap");
  for (let i = 1; i < path.length; i += 1) {
    const [ax, ay] = path[i - 1];
    const [bx, by] = path[i];
    assert.ok(Math.abs(ax - bx) <= 1 && Math.abs(ay - by) <= 1, "adjacent steps");
    assert.notEqual(grid[by * cols + bx], 1, "never on a wall");
    if (ax !== bx && ay !== by) {
      assert.equal(grid[ay * cols + bx], 0, "no corner cutting");
      assert.equal(grid[by * cols + ax], 0, "no corner cutting");
    }
  }
});

test("unreachable, blocked and degenerate goals", () => {
  const grid = new Uint8Array(5 * 5);
  for (let x = 0; x < 5; x += 1) grid[2 * 5 + x] = 1;
  assert.equal(findPath(grid, 5, 5, [0, 0], [0, 4]), null);
  assert.equal(findPath(grid, 5, 5, [0, 0], [0, 2]), null, "goal on a wall");
  assert.equal(findPath(grid, 5, 5, [0, 0], [9, 9]), null, "goal outside the grid");
  assert.deepEqual(findPath(grid, 5, 5, [1, 1], [1, 1]), [[1, 1]]);
});

test("A* is optimal on random grids (vs Dijkstra)", () => {
  const rand = rng(7);
  for (let trial = 0; trial < 40; trial += 1) {
    const cols = 14;
    const rows = 10;
    const grid = new Uint8Array(cols * rows).map(() => (rand() < 0.28 ? 1 : 0));
    grid[0] = 0;
    grid[cols * rows - 1] = 0;
    const path = findPath(grid, cols, rows, [0, 0], [cols - 1, rows - 1]);
    const oracle = bfsLength(grid, cols, rows, [0, 0], [cols - 1, rows - 1]);
    if (oracle === Infinity) assert.equal(path, null);
    else assert.ok(Math.abs(pathLength(path) - oracle) < 1e-9, `trial ${trial}: ${pathLength(path)} vs ${oracle}`);
  }
});

test("octile heuristic is admissible", () => {
  assert.equal(octile(0, 0, 3, 0), 3);
  assert.ok(Math.abs(octile(0, 0, 2, 2) - 2 * Math.SQRT2) < 1e-12);
});

// ------------------------------------------------------------------ floor plan

test("the floor plan has every room, doors and walls", () => {
  const map = buildMap(ROSTER);
  assert.equal(map.cols, COLS);
  assert.equal(map.rows, ROWS);
  assert.equal(Object.keys(ROOMS).length, 10);
  for (const [id, doors] of Object.entries(map.doors)) {
    assert.ok(doors.length >= 2, `${id} has a door`);
    for (const [x, y] of doors) assert.equal(map.grid[y * COLS + x], DOOR);
  }
  for (let x = 0; x < COLS; x += 1) assert.notEqual(map.grid[x], FLOOR, "outer wall on top");
  assert.equal(roomOf(5, 5), "trading_floor");
  assert.equal(roomOf(30, 35), "break_room");
  assert.equal(roomOf(10, 14), null, "corridor");
});

test("every agent has a desk seat and every seat is reachable from every other", () => {
  const map = buildMap(ROSTER);
  assert.equal(map.seats.size, ROSTER.length);
  const all = [...map.seats.values(), ...map.meetingSeats, ...map.breakSeats];
  const keys = new Set(all.map((s) => `${s.x},${s.y}`));
  assert.equal(keys.size, all.length, "no two seats share a tile");
  const hub = [10, 14]; // a corridor tile
  for (const s of all) {
    const path = findPath(map.grid, map.cols, map.rows, hub, [s.x, s.y]);
    assert.ok(path, `seat ${s.x},${s.y} reachable`);
  }
  assert.ok(map.meetingSeats.length >= 10, "room for a full meeting");
  assert.ok(map.breakSeats.length >= 12, "room on the bench");
});

test("desk seats sit in their agent's home room", () => {
  const map = buildMap(ROSTER);
  for (const a of ROSTER) assert.equal(roomOf(map.seats.get(a.id).x, map.seats.get(a.id).y), a.room, a.id);
});

// ------------------------------------------------------------------ camera

const VP = { w: 800, h: 500 };

test("world/screen transforms round-trip", () => {
  const cam = { x: 300, y: 200, zoom: 1.7 };
  for (const [wx, wy] of [[0, 0], [300, 200], [1234, 567]]) {
    const [sx, sy] = worldToScreen(cam, VP, wx, wy);
    const [bx, by] = screenToWorld(cam, VP, sx, sy);
    assert.ok(Math.abs(bx - wx) < 1e-9 && Math.abs(by - wy) < 1e-9);
  }
  assert.deepEqual(worldToScreen(cam, VP, 300, 200), [400, 250], "camera centre at viewport centre");
});

test("zooming keeps the point under the cursor fixed and respects limits", () => {
  const cam = { x: 500, y: 300, zoom: 1 };
  const anchor = screenToWorld(cam, VP, 120, 80);
  const zoomed = zoomAt(cam, VP, 1.5, 120, 80);
  const after = screenToWorld(zoomed, VP, 120, 80);
  assert.ok(Math.abs(after[0] - anchor[0]) < 1e-9 && Math.abs(after[1] - anchor[1]) < 1e-9);
  assert.equal(zoomAt(cam, VP, 100, 0, 0).zoom, MAX_ZOOM);
  assert.equal(zoomAt(cam, VP, 0.001, 0, 0).zoom, MIN_ZOOM);
});

test("clamping keeps the view on the office and centres a small world", () => {
  const world = worldSize();
  const far = clampCamera({ x: -5000, y: 99999, zoom: 2 }, VP, world);
  const [x, y, w, h] = visibleRect(far, VP);
  assert.ok(x >= -1e-9 && y + h <= world.h + 1e-9 && w > 0);
  const tiny = clampCamera({ x: 0, y: 0, zoom: 0.2 }, VP, world);
  assert.deepEqual([tiny.x, tiny.y], [world.w / 2, world.h / 2]);
  const z = fitZoom(VP, world);
  assert.ok(z * world.w <= VP.w && z * world.h <= VP.h);
});

test("following converges without overshoot, independent of the frame rate", () => {
  let a = { x: 0, y: 0, zoom: 1 };
  let b = { x: 0, y: 0, zoom: 1 };
  for (let i = 0; i < 60; i += 1) a = followStep(a, 100, 50, 1 / 60);
  for (let i = 0; i < 30; i += 1) b = followStep(b, 100, 50, 1 / 30);
  assert.ok(a.x > 99 && a.x <= 100 && Math.abs(a.x - b.x) < 1e-6);
});
