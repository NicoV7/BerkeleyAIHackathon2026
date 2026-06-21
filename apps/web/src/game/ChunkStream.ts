/**
 * ChunkStream — pure helpers for the overworld's double-buffered chunk streaming
 * (WS-8). Kept Phaser-free so the swap / prefetch / coverage math is unit-testable
 * and OverworldScene's render loop stays thin.
 *
 * THE BLACK-GAP BUG (root cause, confirmed in review):
 *   The old path overwrote `this.mapData` wholesale and `drawMap()` did a single
 *   RenderTexture `clear()` + redraw of ONLY the new window. While a chunk fetch
 *   was in flight at a chunk edge — and especially when the new window was a
 *   DIFFERENT size/position so the RT was destroyed+recreated — there was a frame
 *   (or several) where the terrain under the player was cleared but the
 *   replacement had not been stamped yet → a visible black gap.
 *
 * THE FIX (double buffering):
 *   Two RenderTextures (front = visible, back = hidden). A new chunk is fully
 *   stamped into the BACK buffer first; only once it is completely drawn do we
 *   swap depths/visibility so the back becomes front. The old terrain stays on
 *   screen the entire time the new chunk is fetched + stamped, so the player
 *   never sees a clear. We also PREFETCH the 8 neighbour chunks (incl. diagonals)
 *   ahead of the edge margin so the swap is usually instant (data already cached).
 */

/** Axis-aligned chunk window in GLOBAL tile coords (inclusive origin). */
export interface ChunkWindow {
  originX: number;
  originY: number;
  width: number;
  height: number;
}

/** A chunk request centred on a tile (what /map?center_x&center_y consumes). */
export interface ChunkRequest {
  x: number;
  y: number;
}

/**
 * True if the global tile (tx,ty) lies INSIDE the window's safe interior — i.e.
 * at least `margin` tiles from every edge. Used to decide when the player is
 * close enough to a chunk edge that we must stream the next chunk.
 */
export function tileWithinSafeMargin(
  win: ChunkWindow,
  tx: number,
  ty: number,
  margin: number
): boolean {
  const lx = tx - win.originX;
  const ly = ty - win.originY;
  return (
    lx >= margin &&
    ly >= margin &&
    lx < win.width - margin &&
    ly < win.height - margin
  );
}

/**
 * Whether the player at global (tx,ty) is near `win`'s edge (within `margin`),
 * meaning a re-centre fetch should be kicked off. Inverse of the safe-margin test.
 */
export function nearChunkEdge(
  win: ChunkWindow,
  tx: number,
  ty: number,
  margin: number
): boolean {
  return !tileWithinSafeMargin(win, tx, ty, margin);
}

/**
 * True if `win` fully covers the camera/viewport AABB (in GLOBAL tile coords).
 * The viewport is described by its top-left tile and tile dimensions. We only
 * need a re-centre when the visible area would spill outside the current chunk —
 * this lets the prefetch keep the swap instant without thrashing fetches.
 */
export function windowCoversViewport(
  win: ChunkWindow,
  viewOriginX: number,
  viewOriginY: number,
  viewWidth: number,
  viewHeight: number
): boolean {
  return (
    viewOriginX >= win.originX &&
    viewOriginY >= win.originY &&
    viewOriginX + viewWidth <= win.originX + win.width &&
    viewOriginY + viewHeight <= win.originY + win.height
  );
}

/**
 * The set of neighbour-chunk centre requests to PREFETCH around a player tile.
 * Returns the centres one chunk-stride away in all 8 directions (cardinals +
 * diagonals), so whichever edge the player crosses next, that chunk's data is
 * already warmed in the browser/server cache and the swap is instant.
 *
 * `stride` is the chunk side length in tiles (the request's chunk_size). Centres
 * are clamped to the world bounds when provided so we never prefetch off-world.
 */
export function neighborPrefetchCentres(
  playerTileX: number,
  playerTileY: number,
  stride: number,
  worldWidth?: number,
  worldHeight?: number
): ChunkRequest[] {
  const out: ChunkRequest[] = [];
  const maxX = worldWidth ? worldWidth - 1 : Infinity;
  const maxY = worldHeight ? worldHeight - 1 : Infinity;
  for (let dy = -1; dy <= 1; dy++) {
    for (let dx = -1; dx <= 1; dx++) {
      if (dx === 0 && dy === 0) continue;
      const cx = Math.max(0, Math.min(maxX, playerTileX + dx * stride));
      const cy = Math.max(0, Math.min(maxY, playerTileY + dy * stride));
      out.push({ x: cx, y: cy });
    }
  }
  return out;
}

/** Stable key for a fetched/prefetched chunk window (origin identifies it). */
export function chunkKey(originX: number, originY: number): string {
  return `${originX}:${originY}`;
}

/**
 * Decide whether a freshly-arrived chunk should be SWAPPED in (i.e. is it
 * different terrain from what is currently displayed?). Returns false when the
 * incoming window is identical to the live one — re-stamping the same chunk would
 * be wasted work and a needless (if invisible) buffer swap.
 */
export function shouldSwapChunk(
  current: ChunkWindow | null,
  incoming: ChunkWindow
): boolean {
  if (!current) return true;
  return (
    current.originX !== incoming.originX ||
    current.originY !== incoming.originY ||
    current.width !== incoming.width ||
    current.height !== incoming.height
  );
}
