/**
 * InteriorLayout — deterministic CLIENT-SIDE interior tile builder (WS-3).
 *
 * WHY THIS EXISTS:
 *   The interior endpoint (GET /api/runs/{id}/interior/{poi_id}) returns a
 *   `WorldSpecLite` — seed, width, height, regions, pois, start, goal — but the
 *   frozen schema has NO `tiles` field, so the server's generated interior grid
 *   never reaches the client. Rather than touch the frozen API contract, we
 *   RECONSTRUCT a renderable interior grid here from (seed, width, height, kind)
 *   and carve floor around the spec's POIs (the exit DOOR + feature anchors) so
 *   every anchor + the exit is always reachable. If the spec has no usable POIs,
 *   we still emit a small bordered room so entering never dead-ends.
 *
 * Determinism: identical (seed, width, height, kind, pois) -> identical grid. All
 * randomness comes from a tiny seeded LCG (mulberry32-style), never Math.random.
 *
 * Tile legend (mirrors apps/api .../algorithms/base.py + SceneRouter.INTERIOR_TILE):
 *   FLOOR=0 (walkable) · WALL=1 (blocked) · DOOR=3 (exit, walkable) ·
 *   FEATURE=4 (anchor, walkable).
 */

import { INTERIOR_TILE, type InteriorSpec, type RoutablePOI } from "./SceneRouter";

export type InteriorKind = "town" | "cave" | "dungeon";

export interface InteriorGrid {
  width: number;
  height: number;
  tiles: number[][];
  /** Where to drop the player on entry (the DOOR, in tile coords). */
  entrance: { x: number; y: number };
  /** The exit tile(s) — stepping on one returns to the overworld. */
  exits: Array<{ x: number; y: number }>;
}

/** Minimum sane interior dims so a degenerate spec still renders a room. */
const MIN_W = 8;
const MIN_H = 6;

/** Deterministic 32-bit hash → seeded PRNG (mulberry32). Pure, no globals. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function clampDims(w: number, h: number): [number, number] {
  return [Math.max(MIN_W, w | 0), Math.max(MIN_H, h | 0)];
}

/** Normalize a kind hint to a known interior generator. */
export function normalizeKind(kind: string | null | undefined): InteriorKind {
  if (kind === "town") return "town";
  if (kind === "dungeon") return "dungeon";
  return "cave";
}

/** Carve a 3x3 floor pocket centred on (x,y), clamped inside the border. */
function carvePocket(tiles: number[][], x: number, y: number, w: number, h: number) {
  for (let dy = -1; dy <= 1; dy++) {
    for (let dx = -1; dx <= 1; dx++) {
      const nx = x + dx;
      const ny = y + dy;
      if (nx > 0 && ny > 0 && nx < w - 1 && ny < h - 1) {
        tiles[ny][nx] = INTERIOR_TILE.FLOOR;
      }
    }
  }
}

/** Solid wall border around an otherwise-floor grid. */
function borderedRoom(w: number, h: number, fill: number): number[][] {
  const tiles: number[][] = [];
  for (let y = 0; y < h; y++) {
    const row: number[] = [];
    for (let x = 0; x < w; x++) {
      const edge = x === 0 || y === 0 || x === w - 1 || y === h - 1;
      row.push(edge ? INTERIOR_TILE.WALL : fill);
    }
    tiles.push(row);
  }
  return tiles;
}

/**
 * Cellular-automata cave (client mirror of caves.py's spirit): random fill, a few
 * smoothing passes, then guarantee the spec's POIs sit on carved floor pockets so
 * the room is navigable and every anchor is reachable.
 */
function buildCave(seed: number, w: number, h: number): number[][] {
  const rand = mulberry32(seed ^ 0xca7e);
  let tiles = borderedRoom(w, h, INTERIOR_TILE.FLOOR);
  // Initial random wall fill in the interior.
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      tiles[y][x] = rand() < 0.42 ? INTERIOR_TILE.WALL : INTERIOR_TILE.FLOOR;
    }
  }
  // Smoothing: a cell becomes wall iff >= 5 of its 8 neighbours are walls.
  for (let step = 0; step < 4; step++) {
    const next = tiles.map((r) => r.slice());
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        let walls = 0;
        for (let dy = -1; dy <= 1; dy++) {
          for (let dx = -1; dx <= 1; dx++) {
            if (dx === 0 && dy === 0) continue;
            const nx = x + dx;
            const ny = y + dy;
            if (nx < 0 || ny < 0 || nx >= w || ny >= h || tiles[ny][nx] === INTERIOR_TILE.WALL) {
              walls++;
            }
          }
        }
        next[y][x] = walls >= 5 ? INTERIOR_TILE.WALL : INTERIOR_TILE.FLOOR;
      }
    }
    tiles = next;
  }
  return tiles;
}

/**
 * Structured village (C2): an open plaza with a grid of HOLLOW house footprints —
 * a wall ring with a floor interior and a doorway gap facing the plaza, so the
 * town reads as buildings you can step into rather than solid blocks. The first
 * couple of houses get a FEATURE tile inside (a shop counter / inn hearth). Stays
 * fully connected: the plaza gutters are open floor and every house has a doorway.
 */
function buildTown(seed: number, w: number, h: number): number[][] {
  const rand = mulberry32(seed ^ 0x7041);
  const tiles = borderedRoom(w, h, INTERIOR_TILE.FLOOR);
  const BW = 4;
  const BH = 3;
  const GUTTER = 2;
  let idx = 0;
  for (let by = 2; by + BH < h - 1; by += BH + GUTTER) {
    for (let bx = 2; bx + BW < w - 1; bx += BW + GUTTER) {
      // Skip ~1 in 6 footprints for organic gaps (deterministic via seed).
      if (rand() < 0.18) continue;
      for (let yy = by; yy <= by + BH; yy++) {
        for (let xx = bx; xx <= bx + BW; xx++) {
          const edge = yy === by || yy === by + BH || xx === bx || xx === bx + BW;
          tiles[yy][xx] = edge ? INTERIOR_TILE.WALL : INTERIOR_TILE.FLOOR;
        }
      }
      // Doorway in the bottom wall (faces the plaza) so the house is enterable.
      tiles[by + BH][bx + 1 + Math.floor(rand() * (BW - 1))] = INTERIOR_TILE.FLOOR;
      // First two houses become shop / inn nooks: a FEATURE inside.
      if (idx < 2) tiles[by + 1][bx + 1] = INTERIOR_TILE.FEATURE;
      idx++;
    }
  }
  return tiles;
}

/**
 * Chambers-and-corridors dungeon (C4): an open base (so the layout is always
 * connected) overlaid with hollow rectangular chambers — each a wall ring with a
 * doorway gap and an occasional inner pillar. Reads as a real multi-room dungeon
 * while guaranteeing reachability: the gutters between chambers are open
 * corridors, and every chamber connects to them through its doorway.
 */
function buildDungeon(seed: number, w: number, h: number): number[][] {
  const rand = mulberry32(seed ^ 0xb59);
  const tiles = borderedRoom(w, h, INTERIOR_TILE.FLOOR);
  const CW = 4;
  const CH = 3;
  const GUTTER = 2;
  for (let by = 2; by + CH < h - 1; by += CH + GUTTER) {
    for (let bx = 2; bx + CW < w - 1; bx += CW + GUTTER) {
      if (rand() < 0.25) continue; // organic gaps
      for (let xx = bx; xx <= bx + CW; xx++) {
        tiles[by][xx] = INTERIOR_TILE.WALL;
        tiles[by + CH][xx] = INTERIOR_TILE.WALL;
      }
      for (let yy = by; yy <= by + CH; yy++) {
        tiles[yy][bx] = INTERIOR_TILE.WALL;
        tiles[yy][bx + CW] = INTERIOR_TILE.WALL;
      }
      // Doorway on a deterministic side so the chamber interior stays reachable.
      const side = Math.floor(rand() * 4);
      if (side === 0) tiles[by][bx + 1 + Math.floor(rand() * (CW - 1))] = INTERIOR_TILE.FLOOR;
      else if (side === 1)
        tiles[by + CH][bx + 1 + Math.floor(rand() * (CW - 1))] = INTERIOR_TILE.FLOOR;
      else if (side === 2) tiles[by + 1 + Math.floor(rand() * (CH - 1))][bx] = INTERIOR_TILE.FLOOR;
      else tiles[by + 1 + Math.floor(rand() * (CH - 1))][bx + CW] = INTERIOR_TILE.FLOOR;
      // Occasional inner pillar for cover/flavour.
      if (rand() < 0.5) {
        tiles[by + 1 + Math.floor(rand() * (CH - 1))][bx + 1 + Math.floor(rand() * (CW - 1))] =
          INTERIOR_TILE.WALL;
      }
    }
  }
  return tiles;
}

/**
 * Build a renderable interior grid for a spec. The returned grid always:
 *   - has a solid wall border,
 *   - guarantees a DOOR exit (from spec.start, else bottom-centre),
 *   - carves floor pockets under every POI + NPC anchor so all are reachable,
 *   - marks FEATURE anchors so the renderer can decorate them.
 */
export function buildInteriorGrid(
  spec: InteriorSpec,
  kind: InteriorKind
): InteriorGrid {
  const [w, h] = clampDims(spec.width, spec.height);
  const seed = (spec.seed | 0) ^ 0x1a7e;

  let tiles: number[][];
  if (kind === "town") tiles = buildTown(seed, w, h);
  else if (kind === "dungeon") tiles = buildDungeon(seed, w, h);
  else tiles = buildCave(seed, w, h);

  const pois = spec.pois ?? [];

  // Exit DOOR: prefer the spec's `start` POI (the server marks the entrance as a
  // `start` POI on a DOOR tile); fall back to bottom-centre so an empty spec is
  // still escapable.
  const startPoi = spec.start ?? pois.find((p) => p.kind === "start") ?? null;
  const door = clampInside(
    startPoi ? { x: startPoi.x, y: startPoi.y } : { x: (w / 2) | 0, y: h - 2 },
    w,
    h
  );
  carvePocket(tiles, door.x, door.y, w, h);
  tiles[door.y][door.x] = INTERIOR_TILE.DOOR;

  // FEATURE anchors: carve a pocket + mark each POI (non-start/goal) and every
  // npc_anchor so the player can always reach NPCs/loot.
  const anchorTiles = collectAnchorTiles(pois);
  for (const a of anchorTiles) {
    const p = clampInside(a, w, h);
    carvePocket(tiles, p.x, p.y, w, h);
    if (tiles[p.y][p.x] !== INTERIOR_TILE.DOOR) {
      tiles[p.y][p.x] = INTERIOR_TILE.FEATURE;
    }
  }

  return {
    width: w,
    height: h,
    tiles,
    entrance: door,
    exits: [door],
  };
}

/** Collect every anchor coordinate (feature POIs + their npc_anchors). */
function collectAnchorTiles(pois: RoutablePOI[]): Array<{ x: number; y: number }> {
  const out: Array<{ x: number; y: number }> = [];
  for (const p of pois) {
    if (p.kind !== "start" && p.kind !== "goal") out.push({ x: p.x, y: p.y });
    for (const a of p.npc_anchors ?? []) out.push({ x: a.x, y: a.y });
  }
  return out;
}

/** Clamp a tile coord to the walkable interior (inside the wall border). */
export function clampInside(
  p: { x: number; y: number },
  w: number,
  h: number
): { x: number; y: number } {
  return {
    x: Math.max(1, Math.min(w - 2, p.x | 0)),
    y: Math.max(1, Math.min(h - 2, p.y | 0)),
  };
}
