/**
 * Client-side map tile helpers.
 * The authoritative map comes from GET /api/runs/{id}/map (server-side deterministic).
 * These helpers are for client-side rendering convenience only.
 */

export const TILE_WALKABLE = 0;
export const TILE_BLOCKED = 1;

export function isTileBlocked(tiles: number[][], x: number, y: number): boolean {
  if (y < 0 || y >= tiles.length) return true;
  if (x < 0 || x >= tiles[0].length) return true;
  return tiles[y][x] === TILE_BLOCKED;
}
