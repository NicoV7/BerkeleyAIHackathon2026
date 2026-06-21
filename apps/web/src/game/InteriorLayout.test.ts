import { describe, expect, it } from "vitest";
import { INTERIOR_TILE, type InteriorSpec } from "./SceneRouter";
import {
  buildInteriorGrid,
  clampInside,
  normalizeKind,
  type InteriorKind,
} from "./InteriorLayout";

function spec(overrides: Partial<InteriorSpec> = {}): InteriorSpec {
  return {
    seed: 12345,
    width: 16,
    height: 12,
    regions: [],
    pois: [],
    start: null,
    goal: null,
    ...overrides,
  };
}

/** 4-connected flood-fill: is `target` reachable from `from` over walkable tiles? */
function reachable(
  tiles: number[][],
  from: { x: number; y: number },
  target: { x: number; y: number }
): boolean {
  const h = tiles.length;
  const w = tiles[0].length;
  const walkable = (x: number, y: number) =>
    x >= 0 && y >= 0 && x < w && y < h && tiles[y][x] !== INTERIOR_TILE.WALL;
  const seen = new Set<string>();
  const stack = [from];
  while (stack.length) {
    const { x, y } = stack.pop()!;
    const k = `${x}:${y}`;
    if (seen.has(k) || !walkable(x, y)) continue;
    seen.add(k);
    if (x === target.x && y === target.y) return true;
    stack.push({ x: x + 1, y }, { x: x - 1, y }, { x, y: y + 1 }, { x, y: y - 1 });
  }
  return false;
}

describe("normalizeKind", () => {
  it("clamps unknown hints to cave", () => {
    expect(normalizeKind("town")).toBe("town");
    expect(normalizeKind("dungeon")).toBe("dungeon");
    expect(normalizeKind("cave")).toBe("cave");
    expect(normalizeKind(null)).toBe("cave");
    expect(normalizeKind("haunted-castle")).toBe("cave");
  });
});

describe("buildInteriorGrid", () => {
  const kinds: InteriorKind[] = ["town", "cave", "dungeon"];

  it.each(kinds)("is deterministic for %s (same seed -> identical grid)", (kind) => {
    const a = buildInteriorGrid(spec(), kind);
    const b = buildInteriorGrid(spec(), kind);
    expect(b.tiles).toEqual(a.tiles);
    expect(b.entrance).toEqual(a.entrance);
  });

  it.each(kinds)("%s always has a solid wall border", (kind) => {
    const g = buildInteriorGrid(spec(), kind);
    for (let x = 0; x < g.width; x++) {
      expect(g.tiles[0][x]).toBe(INTERIOR_TILE.WALL);
      expect(g.tiles[g.height - 1][x]).toBe(INTERIOR_TILE.WALL);
    }
    for (let y = 0; y < g.height; y++) {
      expect(g.tiles[y][0]).toBe(INTERIOR_TILE.WALL);
      expect(g.tiles[y][g.width - 1]).toBe(INTERIOR_TILE.WALL);
    }
  });

  it.each(kinds)("%s places a DOOR exit at the entrance (never a dead end)", (kind) => {
    const g = buildInteriorGrid(spec(), kind);
    expect(g.exits.length).toBeGreaterThan(0);
    expect(g.tiles[g.entrance.y][g.entrance.x]).toBe(INTERIOR_TILE.DOOR);
  });

  it("uses the spec's start POI as the entrance/exit when provided", () => {
    const s = spec({
      start: { kind: "start", x: 4, y: 7, name: "Gate", npc_anchors: [] },
    });
    const g = buildInteriorGrid(s, "town");
    expect(g.entrance).toEqual({ x: 4, y: 7 });
    expect(g.tiles[7][4]).toBe(INTERIOR_TILE.DOOR);
  });

  it("carves floor under feature POIs + NPC anchors so all are reachable", () => {
    const s = spec({
      pois: [
        {
          kind: "landmark",
          x: 11,
          y: 9,
          name: "Loot",
          npc_anchors: [
            { npc_id: "n1", archetype: "villager", x: 6, y: 3, name: "Bram" },
          ],
        },
      ],
    });
    const g = buildInteriorGrid(s, "cave");
    const anchorReachable = reachable(g.tiles, g.entrance, { x: 11, y: 9 });
    const npcReachable = reachable(g.tiles, g.entrance, { x: 6, y: 3 });
    expect(anchorReachable).toBe(true);
    expect(npcReachable).toBe(true);
  });

  it.each(kinds)("%s keeps every anchor reachable from the entrance", (kind) => {
    // Deeper interiors (hollow town houses, chambers-and-corridors dungeons) must
    // never strand the player or an anchor behind walls.
    const s = spec({
      width: 20,
      height: 16,
      pois: [
        {
          kind: "landmark",
          x: 15,
          y: 12,
          name: "Loot",
          npc_anchors: [
            { npc_id: "n1", archetype: "merchant", x: 4, y: 3, name: "Sella" },
            { npc_id: "n2", archetype: "innkeeper", x: 17, y: 4, name: "Bram" },
          ],
        },
      ],
    });
    const g = buildInteriorGrid(s, kind);
    for (const t of [{ x: 15, y: 12 }, { x: 4, y: 3 }, { x: 17, y: 4 }]) {
      const p = clampInside(t, g.width, g.height);
      expect(reachable(g.tiles, g.entrance, p)).toBe(true);
    }
  });

  it("emits a navigable room even for a degenerate (tiny/empty) spec", () => {
    const g = buildInteriorGrid(spec({ width: 1, height: 1, pois: [] }), "cave");
    // Clamped up to the minimum dims so it is still a real room.
    expect(g.width).toBeGreaterThanOrEqual(8);
    expect(g.height).toBeGreaterThanOrEqual(6);
    expect(g.tiles[g.entrance.y][g.entrance.x]).toBe(INTERIOR_TILE.DOOR);
  });
});

describe("clampInside", () => {
  it("keeps a coord inside the wall border", () => {
    expect(clampInside({ x: 0, y: 0 }, 10, 8)).toEqual({ x: 1, y: 1 });
    expect(clampInside({ x: 99, y: 99 }, 10, 8)).toEqual({ x: 8, y: 6 });
    expect(clampInside({ x: 5, y: 4 }, 10, 8)).toEqual({ x: 5, y: 4 });
  });
});
