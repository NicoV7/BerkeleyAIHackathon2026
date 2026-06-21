/**
 * TileAtlas — maps the overworld's tile-type ints onto frames of the Kenney
 * Roguelike/RPG sheet (`/tiles/roguelikeSheet_transparent.png`, CC0) and
 * computes simple edge autotiling (water shorelines, forest/grass borders) plus
 * bridge selection for ROAD tiles crossing WATER.
 *
 * Pure helpers only (no Phaser): OverworldScene consumes the returned frame
 * descriptors and stamps them into a RenderTexture. Keeping the math here makes
 * it unit-testable and keeps OverworldScene's render loop thin.
 *
 * SHEET GEOMETRY (verified by inspecting the PNG, 968×526):
 *   tile = 16px, spacing = 1px, margin = 0  →  57 columns × 31 rows.
 *   Phaser numbers spritesheet frames left→right, top→bottom, so a tile at
 *   grid (col,row) has frame index `row * SHEET_COLS + col`.
 */

/** Columns in the Kenney roguelike sheet (57 × 31 grid of 16px tiles). */
export const SHEET_COLS = 57;
export const SHEET_ROWS = 31;

/** Tile-type ints (mirror of OverworldScene.TILE; duplicated to stay Phaser-free). */
export const TILE = {
  GRASS: 0,
  BLOCKED: 1,
  CAMP: 2,
  ROAD: 3,
  FEATURE: 4,
  FOREST: 5,
  WATER: 6,
  MOUNTAIN: 7,
  TOWN: 8,
  CAVE: 9,
} as const;

/** Convert a (col,row) grid coordinate into a Phaser spritesheet frame index. */
export function frameAt(col: number, row: number): number {
  return row * SHEET_COLS + col;
}

/**
 * Hand-picked frames from the Kenney sheet (verified visually against the PNG).
 * Each entry is the [col, row] of a clean, mostly-opaque tile.
 */
export const FRAME = {
  GRASS: frameAt(5, 0), // bright solid grass               idx 5
  WATER: frameAt(1, 4), // clean cyan water                 idx 229
  DIRT: frameAt(6, 0), // brown dirt                        idx 6
  ROAD: frameAt(6, 4), // brown brick / cobble path         idx 234
  COBBLE: frameAt(7, 0), // light grey cobble (FEATURE)     idx 7
  STONE: frameAt(20, 13), // grey rock face (MOUNTAIN)      idx 761
  TOWN_FLOOR: frameAt(14, 13), // tan sandstone (TOWN base) idx 755
  CAVE: frameAt(36, 2), // dark charcoal floor (CAVE)       idx 150
  SAND: frameAt(8, 0), // pale sand (shoreline edge tint)   idx 8
  BRIDGE: frameAt(45, 13), // horizontal wood planks        idx 786
  // Decorative overlays (transparent background, stamped on top of a base):
  TREE: frameAt(13, 9), // round leafy tree (FOREST)        idx 526
  TREE_DARK: frameAt(15, 9), // darker tree variant         idx 528
  CAMPFIRE: frameAt(14, 8), // lit campfire (CAMP)          idx 470
  STRUCTURE: frameAt(15, 14), // light stone block (TOWN)   idx 813
} as const;

/**
 * Per-tile-int base ground frame. Overlay-style tiles (FOREST/CAMP/TOWN) render
 * a grass/floor base here and add their feature on top via overlayFor().
 * Returns `null` for any tile-int with no mapping → caller uses the procedural
 * color-jitter fallback so nothing renders blank.
 */
export function baseFrameFor(tile: number): number | null {
  switch (tile) {
    case TILE.GRASS:
      return FRAME.GRASS;
    case TILE.FOREST:
      return FRAME.GRASS; // tree drawn on grass
    case TILE.CAMP:
      return FRAME.GRASS; // campfire drawn on grass
    case TILE.ROAD:
      return FRAME.ROAD;
    case TILE.FEATURE:
      return FRAME.COBBLE;
    case TILE.WATER:
      return FRAME.WATER;
    case TILE.MOUNTAIN:
      return FRAME.STONE;
    case TILE.TOWN:
      return FRAME.TOWN_FLOOR;
    case TILE.CAVE:
      return FRAME.CAVE;
    case TILE.BLOCKED:
      return FRAME.CAVE; // generic dark wall
    default:
      return null; // unknown → procedural fallback
  }
}

export interface Overlay {
  frame: number;
  /** Stamp alpha (0..1). */
  alpha: number;
  /** Optional tint (0xRRGGBB), WebGL only. */
  tint?: number;
}

/**
 * Feature overlay stamped on top of the base frame, or `null` for plain ground.
 * FOREST → tree, CAMP → campfire, TOWN → small stone structure. The tree variant
 * alternates by a deterministic per-tile bit so a forest doesn't look uniform.
 */
export function overlayFor(tile: number, jitter: number): Overlay | null {
  switch (tile) {
    case TILE.FOREST:
      return { frame: jitter & 0x40 ? FRAME.TREE_DARK : FRAME.TREE, alpha: 1 };
    case TILE.CAMP:
      return { frame: FRAME.CAMPFIRE, alpha: 1 };
    // TOWN structures are drawn by the #24 detail overlay (house/shop/inn/barn),
    // so we no longer stamp a generic Kenney structure tile here.
    default:
      return null;
  }
}

/** A directional edge accent stamped along one side of a tile. */
export interface EdgeStamp {
  /** Which side of the tile this accent hugs. */
  side: "n" | "s" | "e" | "w";
  /** Atlas frame to stamp (currently the SAND frame for shorelines). */
  frame: number;
  alpha: number;
  tint?: number;
}

/** Neighbour tile-ints (out-of-bounds reported as the tile itself = no edge). */
export interface Neighbors {
  n: number;
  s: number;
  e: number;
  w: number;
}

/**
 * WATER shoreline autotiling: for a WATER tile, return a sand-coloured accent on
 * every side whose neighbour is NOT water (and not itself a bridge crossing).
 * The accent is a translucent sand strip clipped to the tile edge by the caller,
 * giving a beach/foam read where water meets land without needing the sheet's
 * full Wang-tile shoreline set.
 */
export function shorelineEdges(tile: number, nb: Neighbors): EdgeStamp[] {
  if (tile !== TILE.WATER) return [];
  const edges: EdgeStamp[] = [];
  const land = (t: number) => t !== TILE.WATER;
  if (land(nb.n)) edges.push({ side: "n", frame: FRAME.SAND, alpha: 0.55 });
  if (land(nb.s)) edges.push({ side: "s", frame: FRAME.SAND, alpha: 0.55 });
  if (land(nb.e)) edges.push({ side: "e", frame: FRAME.SAND, alpha: 0.55 });
  if (land(nb.w)) edges.push({ side: "w", frame: FRAME.SAND, alpha: 0.55 });
  return edges;
}

/**
 * Forest/grass border accent: for a GRASS tile bordering FOREST, return a faint
 * darker-green tinted grass accent on the forest-facing side so biome borders
 * blend instead of cutting hard. Returns [] for non-grass tiles.
 */
export function forestBorderEdges(tile: number, nb: Neighbors): EdgeStamp[] {
  if (tile !== TILE.GRASS) return [];
  const edges: EdgeStamp[] = [];
  const forest = (t: number) => t === TILE.FOREST;
  const accent = { frame: FRAME.GRASS, alpha: 0.35, tint: 0x3f6c36 };
  if (forest(nb.n)) edges.push({ side: "n", ...accent });
  if (forest(nb.s)) edges.push({ side: "s", ...accent });
  if (forest(nb.e)) edges.push({ side: "e", ...accent });
  if (forest(nb.w)) edges.push({ side: "w", ...accent });
  return edges;
}

/**
 * Bridge detection: a ROAD tile becomes a wooden bridge when it touches WATER —
 * i.e. it has WATER neighbours on an opposite pair (a true crossing) OR is
 * directly adjacent to water at all (approach plank). Returns the bridge frame
 * to use in place of the road frame, or `null` to keep the normal road.
 */
export function bridgeFrameFor(tile: number, nb: Neighbors): number | null {
  if (tile !== TILE.ROAD) return null;
  const water = (t: number) => t === TILE.WATER;
  const crossingNS = water(nb.n) && water(nb.s);
  const crossingEW = water(nb.e) && water(nb.w);
  const touchesWater = water(nb.n) || water(nb.s) || water(nb.e) || water(nb.w);
  return crossingNS || crossingEW || touchesWater ? FRAME.BRIDGE : null;
}

/** True if this tile-int is rendered purely from the atlas (has a base frame). */
export function hasAtlasFrame(tile: number): boolean {
  return baseFrameFor(tile) !== null;
}

// ---------------------------------------------------------------------------
// V1 cohesion: client-side terrain blending (pure, tilemap-friendly — full-tile
// frame choices, no half-tile offsets). All deterministic per-tile via jitter.
// ---------------------------------------------------------------------------

const _waterAdjacent = (nb: Neighbors) =>
  nb.n === TILE.WATER || nb.s === TILE.WATER || nb.e === TILE.WATER || nb.w === TILE.WATER;

const _forestAdjacent = (nb: Neighbors) =>
  nb.n === TILE.FOREST || nb.s === TILE.FOREST || nb.e === TILE.FOREST || nb.w === TILE.FOREST;

/**
 * Shoreline blend: a walkable land tile directly touching WATER renders a SAND
 * base, producing a 1-tile beach ring that softens the hard water/land border.
 * Returns the sand frame to use in place of the normal base, or null to keep it.
 */
export function shorelineBaseFrame(tile: number, nb: Neighbors): number | null {
  if (tile === TILE.WATER || tile === TILE.MOUNTAIN || tile === TILE.BLOCKED) return null;
  return _waterAdjacent(nb) ? FRAME.SAND : null;
}

/**
 * Forest feathering: scatter sparse edge trees onto GRASS tiles that border a
 * FOREST so the woods thin into the plains instead of beginning at a hard wall
 * ("grass before the big trees"). Deterministic per-tile (~37% of border grass),
 * frame varied for texture. Returns an overlay or null. Pure.
 */
export function forestFeatherOverlay(
  tile: number,
  nb: Neighbors,
  jitter: number
): Overlay | null {
  if (tile !== TILE.GRASS || !_forestAdjacent(nb)) return null;
  if ((jitter & 0x7) >= 3) return null; // ~3/8 density
  return { frame: jitter & 0x100 ? FRAME.TREE_DARK : FRAME.TREE, alpha: 1 };
}

/**
 * Subtle per-tile tint for GRASS so large fields aren't a flat color. Cycles a
 * few near-white green shades (multiplied with the grass frame). Returns a tint
 * or undefined (no tint) for non-grass. Pure.
 */
export function grassTint(tile: number, jitter: number): number | undefined {
  if (tile !== TILE.GRASS && tile !== TILE.CAMP) return undefined;
  const shades = [0xffffff, 0xeaf2dc, 0xdcecc6, 0xf2f0da];
  return shades[(jitter >>> 9) & 0x3];
}
