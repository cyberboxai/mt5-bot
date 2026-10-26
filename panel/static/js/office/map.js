// The office floor plan as a tile grid (pure: no DOM, testable with `node --test`).
//
// Values: 0 floor, 1 wall, 2 furniture (blocked), 3 door. Ten rooms around two corridors;
// every room has at least one door on a corridor. Seats are walkable tiles in front of a
// desk (desk agents), around the boardroom table (meetings) and in the break room.

export const TILE = 24;
export const COLS = 60;
export const ROWS = 40;
export const FLOOR = 0;
export const WALL = 1;
export const FURNITURE = 2;
export const DOOR = 3;

// [x0, y0, x1, y1] inclusive, walls on the perimeter (neighbours share a wall column).
export const ROOMS = {
  trading_floor: [0, 0, 21, 13], research_lab: [21, 0, 34, 13], risk_office: [34, 0, 46, 13],
  advisor_booth: [46, 0, 59, 13],
  newsroom: [0, 16, 13, 28], boardroom: [13, 16, 33, 28], execution_desk: [33, 16, 46, 28], ops_room: [46, 16, 59, 28],
  analytics: [0, 31, 21, 39], break_room: [21, 31, 59, 39],
};
export const CORRIDORS = [[1, 14, 58, 15], [1, 29, 58, 30]];

// Desk rows per room: [first seat x, seat y, step x]. Seats face the desk above them (north).
const DESK_ROWS = {
  trading_floor: [[3, 5, 4]], research_lab: [[24, 5, 4]], risk_office: [[36, 5, 4]], advisor_booth: [[52, 6, 4]],
  newsroom: [[4, 21, 5]], execution_desk: [[37, 21, 5]], ops_room: [[50, 21, 5]], analytics: [[4, 35, 6]],
};
const TABLE = [18, 20, 28, 23]; // boardroom table (blocked)
export const CIO_SEAT = [17, 21];

export function roomOf(x, y) {
  for (const [id, [x0, y0, x1, y1]] of Object.entries(ROOMS)) {
    if (x > x0 && x < x1 && y > y0 && y < y1) return id;
  }
  return null;
}

export function roomCenter(id) {
  const [x0, y0, x1, y1] = ROOMS[id];
  return [Math.floor((x0 + x1) / 2), Math.floor((y0 + y1) / 2)];
}

function doorsFor(id) {
  const [x0, y0, x1, y1] = ROOMS[id];
  const mid = Math.floor((x0 + x1) / 2);
  const out = [];
  const touchesTop = CORRIDORS.some(([, , , cy1]) => cy1 + 1 === y0);
  const touchesBottom = CORRIDORS.some(([, cy0]) => cy0 - 1 === y1);
  if (touchesTop) out.push([mid, y0], [mid + 1, y0]);
  if (touchesBottom) out.push([mid, y1], [mid + 1, y1]);
  if ((x1 - x0) > 24) { // wide rooms get a second pair of doors
    const q = Math.floor(x0 + (x1 - x0) * 0.25);
    if (touchesTop) out.push([q, y0], [q + 1, y0]);
    if (touchesBottom) out.push([q, y1], [q + 1, y1]);
  }
  return out;
}

/**
 * Build the grid and the seats for a roster ([{id, room, ...}]). Returns
 * { cols, rows, tile, grid, rooms, doors, seats: Map(id -> {x, y, face, desk}), meetingSeats,
 *   breakSeats, furniture: [{kind, x, y, w, h, room}] }.
 */
export function buildMap(roster) {
  const grid = new Uint8Array(COLS * ROWS).fill(WALL);
  const set = (x, y, v) => { grid[y * COLS + x] = v; };
  const get = (x, y) => grid[y * COLS + x];
  const furniture = [];
  const block = (kind, x, y, w, h, room) => {
    furniture.push({ kind, x, y, w, h, room });
    for (let j = y; j < y + h; j += 1) for (let i = x; i < x + w; i += 1) set(i, j, FURNITURE);
  };
  for (const [x0, y0, x1, y1] of Object.values(ROOMS)) {
    for (let y = y0 + 1; y < y1; y += 1) for (let x = x0 + 1; x < x1; x += 1) set(x, y, FLOOR);
  }
  for (const [x0, y0, x1, y1] of CORRIDORS) {
    for (let y = y0; y <= y1; y += 1) for (let x = x0; x <= x1; x += 1) set(x, y, FLOOR);
  }
  const doors = {};
  for (const id of Object.keys(ROOMS)) {
    doors[id] = doorsFor(id);
    for (const [x, y] of doors[id]) set(x, y, DOOR);
  }

  // Desks: one per home agent (the CIO's seat is at the head of the boardroom table).
  const byRoom = {};
  for (const a of roster) (byRoom[a.room] ||= []).push(a.id);
  const seats = new Map();
  for (const [room, ids] of Object.entries(byRoom)) {
    if (room === "boardroom") {
      for (const id of ids) seats.set(id, { x: CIO_SEAT[0], y: CIO_SEAT[1], face: "right", desk: null, room });
      continue;
    }
    const rows = DESK_ROWS[room] || [[ROOMS[room][0] + 3, ROOMS[room][1] + 5, 4]];
    ids.forEach((id, i) => {
      const [fx, fy, step] = rows[0];
      const [x0, , x1] = ROOMS[room];
      const perRow = Math.max(1, Math.floor((x1 - 1 - fx) / step) + 1);
      const x = fx + (i % perRow) * step;
      const y = fy + Math.floor(i / perRow) * 4;
      if (x <= x0 || x >= x1) return;
      block("desk", x - 1, y - 1, 3, 1, room);
      seats.set(id, { x, y, face: "up", desk: [x - 1, y - 1], room });
    });
  }

  // Boardroom table and its seats (north and south sides, then the far end).
  const [tx0, ty0, tx1, ty1] = TABLE;
  block("table", tx0, ty0, tx1 - tx0 + 1, ty1 - ty0 + 1, "boardroom");
  const meetingSeats = [];
  for (let x = tx0 + 1; x <= tx1 - 1; x += 2) meetingSeats.push({ x, y: ty0 - 1, face: "down" });
  for (let x = tx0 + 1; x <= tx1 - 1; x += 2) meetingSeats.push({ x, y: ty1 + 1, face: "up" });
  meetingSeats.push({ x: tx1 + 1, y: ty0 + 1, face: "left" }, { x: tx1 + 1, y: ty0 + 2, face: "left" });
  block("screen_wall", 26, 17, 6, 1, "boardroom");

  // Break room: sofas, coffee counter, plants; seats in front of the sofas and at the tables.
  block("sofa", 25, 37, 10, 1, "break_room");
  block("sofa", 38, 37, 10, 1, "break_room");
  block("coffee", 52, 32, 5, 1, "break_room");
  block("table_small", 29, 33, 2, 2, "break_room");
  block("table_small", 43, 33, 2, 2, "break_room");
  block("plant", 57, 37, 1, 1, "break_room");
  const breakSeats = [];
  for (let x = 26; x <= 34; x += 2) breakSeats.push({ x, y: 36, face: "down" });
  for (let x = 39; x <= 47; x += 2) breakSeats.push({ x, y: 36, face: "down" });
  for (const [x, y, face] of [[28, 33, "right"], [31, 34, "left"], [42, 33, "right"], [45, 34, "left"],
    [53, 33, "up"], [55, 33, "up"]]) breakSeats.push({ x, y, face });

  // Room furniture (decor that also blocks: racks, whiteboards, console, TV wall, screens).
  block("rack", 48, 25, 2, 2, "ops_room");
  block("rack", 51, 25, 2, 2, "ops_room");
  block("rack", 54, 25, 2, 2, "ops_room");
  block("tv_wall", 2, 17, 10, 1, "newsroom");
  block("whiteboard", 23, 10, 6, 1, "research_lab");
  block("console", 50, 3, 6, 1, "advisor_booth");
  block("lamp", 56, 9, 1, 1, "advisor_booth");
  block("vault", 42, 10, 3, 2, "risk_office");
  block("ticker_board", 2, 10, 18, 1, "trading_floor");
  block("printer", 43, 25, 2, 1, "execution_desk");
  block("screens", 3, 38, 17, 1, "analytics");
  block("plant", 1, 1, 1, 1, "trading_floor");
  block("plant", 1, 38, 1, 1, "analytics");
  for (const s of [...seats.values(), ...meetingSeats, ...breakSeats]) {
    if (get(s.x, s.y) !== FLOOR) throw new Error(`seat on a blocked tile at ${s.x},${s.y}`);
  }
  return { cols: COLS, rows: ROWS, tile: TILE, grid, rooms: ROOMS, doors, seats, meetingSeats, breakSeats, furniture };
}

export function tileCenter(x, y) { return [(x + 0.5) * TILE, (y + 0.5) * TILE]; }

export function worldSize() { return { w: COLS * TILE, h: ROWS * TILE }; }
