import { describe, expect, it } from "vitest";
import {
  chunkKey,
  nearChunkEdge,
  neighborPrefetchCentres,
  shouldSwapChunk,
  tileWithinSafeMargin,
  windowCoversViewport,
  windowsWithinRenderHalo,
  type ChunkWindow,
} from "./ChunkStream";

const WIN: ChunkWindow = { originX: 100, originY: 100, width: 96, height: 96 };

describe("tileWithinSafeMargin / nearChunkEdge", () => {
  it("a tile well inside the window is within the safe margin", () => {
    // local (48,48) — dead centre of a 96-wide window, margin 18.
    expect(tileWithinSafeMargin(WIN, 148, 148, 18)).toBe(true);
    expect(nearChunkEdge(WIN, 148, 148, 18)).toBe(false);
  });

  it("a tile inside the edge margin is flagged as near the edge", () => {
    // local x = 10 (< margin 18) → near the west edge.
    expect(tileWithinSafeMargin(WIN, 110, 148, 18)).toBe(false);
    expect(nearChunkEdge(WIN, 110, 148, 18)).toBe(true);
  });

  it("treats all four edges symmetrically", () => {
    expect(nearChunkEdge(WIN, 148, 110, 18)).toBe(true); // north
    expect(nearChunkEdge(WIN, 188, 148, 18)).toBe(true); // east (local 88 >= 96-18)
    expect(nearChunkEdge(WIN, 148, 188, 18)).toBe(true); // south
  });
});

describe("shouldSwapChunk (the double-buffer swap decision)", () => {
  it("always swaps in when there is no live window yet (first load)", () => {
    expect(shouldSwapChunk(null, WIN)).toBe(true);
  });

  it("does NOT swap when the incoming window is identical to the live one", () => {
    // Re-fetching the SAME chunk (e.g. enemy refresh) must not trigger a swap —
    // this is what keeps drawMap from clearing+redrawing the visible terrain.
    expect(shouldSwapChunk(WIN, { ...WIN })).toBe(false);
  });

  it("swaps when the origin moves (the player crossed into a new chunk)", () => {
    expect(shouldSwapChunk(WIN, { ...WIN, originX: 196 })).toBe(true);
    expect(shouldSwapChunk(WIN, { ...WIN, originY: 4 })).toBe(true);
  });

  it("swaps when the window is resized (edge-clamped chunk near the world border)", () => {
    expect(shouldSwapChunk(WIN, { ...WIN, width: 64 })).toBe(true);
    expect(shouldSwapChunk(WIN, { ...WIN, height: 50 })).toBe(true);
  });
});

describe("windowCoversViewport", () => {
  it("true when the viewport fits inside the chunk window", () => {
    expect(windowCoversViewport(WIN, 120, 120, 30, 30)).toBe(true);
  });
  it("false when the viewport spills past the chunk edge", () => {
    expect(windowCoversViewport(WIN, 180, 120, 30, 30)).toBe(false);
  });
});

describe("windowsWithinRenderHalo", () => {
  it("keeps overlapping chunk windows eligible for neighbor rendering", () => {
    expect(
      windowsWithinRenderHalo(WIN, { originX: 148, originY: 100, width: 96, height: 96 })
    ).toBe(true);
  });

  it("uses halo tiles to keep just-ahead chunks rendered before the camera exposes them", () => {
    expect(
      windowsWithinRenderHalo(WIN, { originX: 198, originY: 100, width: 96, height: 96 }, 4)
    ).toBe(true);
    expect(
      windowsWithinRenderHalo(WIN, { originX: 220, originY: 100, width: 96, height: 96 }, 4)
    ).toBe(false);
  });
});

describe("neighborPrefetchCentres", () => {
  it("returns the 8 surrounding chunk centres (cardinals + diagonals)", () => {
    const centres = neighborPrefetchCentres(200, 200, 48);
    expect(centres).toHaveLength(8);
    // Must include both a cardinal and a diagonal at one stride out.
    expect(centres).toContainEqual({ x: 248, y: 200 }); // east
    expect(centres).toContainEqual({ x: 152, y: 152 }); // north-west diagonal
    // Never includes the player's own chunk centre.
    expect(centres).not.toContainEqual({ x: 200, y: 200 });
  });

  it("clamps centres to the world bounds so we never prefetch off-world", () => {
    const centres = neighborPrefetchCentres(5, 5, 48, 1000, 1000);
    for (const c of centres) {
      expect(c.x).toBeGreaterThanOrEqual(0);
      expect(c.y).toBeGreaterThanOrEqual(0);
    }
  });
});

describe("chunkKey", () => {
  it("is stable + collision-free per origin", () => {
    expect(chunkKey(100, 200)).toBe("100:200");
    expect(chunkKey(100, 200)).not.toBe(chunkKey(200, 100));
  });
});
