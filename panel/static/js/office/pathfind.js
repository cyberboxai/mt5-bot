// A* on a tile grid (pure: no DOM, testable with `node --test`). Eight directions without
// corner cutting (a diagonal step needs both orthogonal neighbours free), octile heuristic.

export const SQRT2 = Math.SQRT2;

class Heap {
  constructor() { this.items = []; }
  get size() { return this.items.length; }
  push(node, f) {
    const a = this.items;
    a.push([f, node]);
    let i = a.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (a[p][0] <= a[i][0]) break;
      [a[p], a[i]] = [a[i], a[p]];
      i = p;
    }
  }
  pop() {
    const a = this.items;
    const top = a[0];
    const last = a.pop();
    if (a.length) {
      a[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1;
        const r = l + 1;
        let m = i;
        if (l < a.length && a[l][0] < a[m][0]) m = l;
        if (r < a.length && a[r][0] < a[m][0]) m = r;
        if (m === i) break;
        [a[m], a[i]] = [a[i], a[m]];
        i = m;
      }
    }
    return top[1];
  }
}

export function octile(ax, ay, bx, by) {
  const dx = Math.abs(ax - bx);
  const dy = Math.abs(ay - by);
  return Math.max(dx, dy) + (SQRT2 - 1) * Math.min(dx, dy);
}

const DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];

/**
 * Shortest path from `start` to `goal` ([x, y] tiles) or null when unreachable.
 * `passable(value, x, y)` decides which grid values can be walked (default: 0 floor, 3 door).
 * The returned list includes both ends. `maxNodes` bounds the work on huge grids.
 */
export function findPath(grid, cols, rows, start, goal, { passable = defaultPassable, diagonal = true,
  maxNodes = 200000 } = {}) {
  const [sx, sy] = start;
  const [gx, gy] = goal;
  const inside = (x, y) => x >= 0 && y >= 0 && x < cols && y < rows;
  if (!inside(sx, sy) || !inside(gx, gy)) return null;
  const free = (x, y) => inside(x, y) && passable(grid[y * cols + x], x, y);
  if (!free(gx, gy) || !free(sx, sy)) return null;
  if (sx === gx && sy === gy) return [[sx, sy]];
  const n = cols * rows;
  const g = new Float64Array(n).fill(Infinity);
  const from = new Int32Array(n).fill(-1);
  const closed = new Uint8Array(n);
  const open = new Heap();
  const s = sy * cols + sx;
  const goalIndex = gy * cols + gx;
  g[s] = 0;
  open.push(s, octile(sx, sy, gx, gy));
  let expanded = 0;
  while (open.size) {
    const cur = open.pop();
    if (closed[cur]) continue;
    if (cur === goalIndex) return rebuild(from, cur, cols);
    closed[cur] = 1;
    if (++expanded > maxNodes) return null;
    const cx = cur % cols;
    const cy = (cur - cx) / cols;
    for (const [dx, dy] of DIRS) {
      if (!diagonal && dx && dy) continue;
      const nx = cx + dx;
      const ny = cy + dy;
      if (!free(nx, ny)) continue;
      if (dx && dy && (!free(cx + dx, cy) || !free(cx, cy + dy))) continue; // no corner cutting
      const ni = ny * cols + nx;
      if (closed[ni]) continue;
      const cost = g[cur] + (dx && dy ? SQRT2 : 1);
      if (cost < g[ni] - 1e-9) {
        g[ni] = cost;
        from[ni] = cur;
        open.push(ni, cost + octile(nx, ny, gx, gy));
      }
    }
  }
  return null;
}

function rebuild(from, index, cols) {
  const out = [];
  for (let i = index; i !== -1; i = from[i]) out.push([i % cols, Math.floor(i / cols)]);
  return out.reverse();
}

export function defaultPassable(value) { return value === 0 || value === 3; }

export function pathLength(path) {
  let total = 0;
  for (let i = 1; i < path.length; i += 1) {
    total += path[i][0] !== path[i - 1][0] && path[i][1] !== path[i - 1][1] ? SQRT2 : 1;
  }
  return total;
}

/** Drop the intermediate points of straight runs (same direction): fewer waypoints to walk. */
export function compress(path) {
  if (!path || path.length < 3) return path;
  const out = [path[0]];
  for (let i = 1; i < path.length - 1; i += 1) {
    const [ax, ay] = out[out.length - 1];
    const [bx, by] = path[i];
    const [cx, cy] = path[i + 1];
    if (Math.sign(bx - ax) !== Math.sign(cx - bx) || Math.sign(by - ay) !== Math.sign(cy - by)) out.push(path[i]);
  }
  out.push(path[path.length - 1]);
  return out;
}
