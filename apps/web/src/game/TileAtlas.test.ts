import { describe, expect, it } from "vitest";
import {
  FRAME,
  SHEET_COLS,
  SHEET_ROWS,
  TILE,
  baseFrameFor,
  bridgeFrameFor,
  forestBorderEdges,
  frameAt,
  hasAtlasFrame,
  overlayFor,
  shorelineEdges,
  type Neighbors,
} from "./TileAtlas";

/** Helper: build a Neighbors record, defaulting unset sides to the tile itself
 * (matches OverworldScene's out-of-bounds = "no edge" convention). */
function nb(self: number, sides: Partial<Neighbors> = {}): Neighbors {
  return { n: self, s: self, e: self, w: self, ...sides };
}

describe("TileAtlas frame geometry", () => {
  it("matches the verified 57x31 Kenney sheet grid", () => {
    expect(SHEET_COLS).toBe(57);
    expect(SHEET_ROWS).toBe(31);
  });

  it("frameAt converts (col,row) into a row-major spritesheet index", () => {
    expect(frameAt(0, 0)).toBe(0);
    expect(frameAt(5, 0)).toBe(5);
    expect(frameAt(0, 1)).toBe(SHEET_COLS);
    expect(frameAt(20, 13)).toBe(13 * 57 + 20);
  });

  it("every mapped frame index falls inside the atlas bounds", () => {
    const max = SHEET_COLS * SHEET_ROWS - 1;
    for (const idx of Object.values(FRAME)) {
      expect(idx).toBeGreaterThanOrEqual(0);
      expect(idx).toBeLessThanOrEqual(max);
    }
  });
});

describe("baseFrameFor", () => {
  it("maps each known terrain tile-int to an atlas frame", () => {
    for (const t of [
      TILE.GRASS,
      TILE.BLOCKED,
      TILE.CAMP,
      TILE.ROAD,
      TILE.FEATURE,
      TILE.FOREST,
      TILE.WATER,
      TILE.MOUNTAIN,
      TILE.TOWN,
      TILE.CAVE,
    ]) {
      expect(baseFrameFor(t)).not.toBeNull();
      expect(hasAtlasFrame(t)).toBe(true);
    }
  });

  it("forest and camp render on a grass base (overlay tiles)", () => {
    expect(baseFrameFor(TILE.FOREST)).toBe(FRAME.GRASS);
    expect(baseFrameFor(TILE.CAMP)).toBe(FRAME.GRASS);
  });

  it("returns null for an unmapped tile-int so the caller falls back", () => {
    expect(baseFrameFor(99)).toBeNull();
    expect(hasAtlasFrame(99)).toBe(false);
  });
});

describe("overlayFor", () => {
  it("adds a tree on forest, a campfire on camp, a structure on town", () => {
    expect(overlayFor(TILE.FOREST, 0)?.frame).toBe(FRAME.TREE);
    expect(overlayFor(TILE.FOREST, 0x40)?.frame).toBe(FRAME.TREE_DARK);
    expect(overlayFor(TILE.CAMP, 0)?.frame).toBe(FRAME.CAMPFIRE);
    expect(overlayFor(TILE.TOWN, 0)?.frame).toBe(FRAME.STRUCTURE);
  });

  it("plain ground tiles have no overlay", () => {
    expect(overlayFor(TILE.GRASS, 0)).toBeNull();
    expect(overlayFor(TILE.WATER, 0)).toBeNull();
    expect(overlayFor(TILE.ROAD, 0)).toBeNull();
  });
});

describe("shoreline autotiling", () => {
  it("adds a sand edge on every land-facing side of a water tile", () => {
    const edges = shorelineEdges(TILE.WATER, nb(TILE.WATER, { n: TILE.GRASS, e: TILE.ROAD }));
    const sides = edges.map((e) => e.side).sort();
    expect(sides).toEqual(["e", "n"]);
    expect(edges.every((e) => e.frame === FRAME.SAND)).toBe(true);
  });

  it("open water (all-water neighbours) gets no shoreline", () => {
    expect(shorelineEdges(TILE.WATER, nb(TILE.WATER))).toEqual([]);
  });

  it("non-water tiles never produce shorelines", () => {
    expect(shorelineEdges(TILE.GRASS, nb(TILE.GRASS, { n: TILE.WATER }))).toEqual([]);
  });
});

describe("forest/grass border autotiling", () => {
  it("adds a darker-green seam on grass sides facing forest", () => {
    const edges = forestBorderEdges(TILE.GRASS, nb(TILE.GRASS, { s: TILE.FOREST }));
    expect(edges).toHaveLength(1);
    expect(edges[0].side).toBe("s");
    expect(edges[0].tint).toBeDefined();
  });

  it("only grass tiles get the forest seam", () => {
    expect(forestBorderEdges(TILE.FOREST, nb(TILE.FOREST, { n: TILE.GRASS }))).toEqual([]);
  });
});

describe("bridge selection", () => {
  it("turns a road into a bridge when it crosses water (opposite sides)", () => {
    expect(bridgeFrameFor(TILE.ROAD, nb(TILE.ROAD, { n: TILE.WATER, s: TILE.WATER }))).toBe(
      FRAME.BRIDGE
    );
    expect(bridgeFrameFor(TILE.ROAD, nb(TILE.ROAD, { e: TILE.WATER, w: TILE.WATER }))).toBe(
      FRAME.BRIDGE
    );
  });

  it("an approach road merely touching water also becomes a plank", () => {
    expect(bridgeFrameFor(TILE.ROAD, nb(TILE.ROAD, { e: TILE.WATER }))).toBe(FRAME.BRIDGE);
  });

  it("a landlocked road keeps the normal road frame", () => {
    expect(bridgeFrameFor(TILE.ROAD, nb(TILE.ROAD, { n: TILE.GRASS }))).toBeNull();
  });

  it("non-road tiles are never bridges", () => {
    expect(bridgeFrameFor(TILE.WATER, nb(TILE.WATER, { n: TILE.ROAD }))).toBeNull();
  });
});
