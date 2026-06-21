import { TILE_SIZE } from "./constants";

const MIN_CHUNK_WINDOW_TILES = 96;
const MAX_CHUNK_WINDOW_TILES = 160;
const CHUNK_VIEWPORT_BUFFER_TILES = 32;
const CHUNK_REFRESH_BUFFER_TILES = 10;

export function chunkWindowTilesForViewport(widthPx: number, heightPx: number): number {
  const viewportTiles = Math.ceil(Math.max(widthPx, heightPx) / TILE_SIZE);
  return Math.max(
    MIN_CHUNK_WINDOW_TILES,
    Math.min(MAX_CHUNK_WINDOW_TILES, viewportTiles + CHUNK_VIEWPORT_BUFFER_TILES)
  );
}

export function chunkEdgeMarginTilesForViewport(
  widthPx: number,
  heightPx: number,
  chunkTiles: number
): number {
  const halfViewportTiles = Math.ceil(Math.max(widthPx, heightPx) / TILE_SIZE / 2);
  const maxUsefulMargin = Math.max(8, Math.floor(chunkTiles / 2) - 4);
  return Math.min(maxUsefulMargin, halfViewportTiles + CHUNK_REFRESH_BUFFER_TILES);
}
