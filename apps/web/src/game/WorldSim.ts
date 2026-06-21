/**
 * WorldSim — client-side world simulation for the overworld (Track B, Wave 1).
 *
 * Replaces the old per-step `POST /api/runs/{id}/move` + 150ms throttle with
 * smooth, velocity-based movement integrated every frame. The server stops being
 * the per-step movement gatekeeper; it remains the seed + persistence authority
 * (position is pushed back debounced via POST /api/runs/{id}/sync — see
 * OverworldScene).
 *
 * Responsibilities:
 *   - Read input intent (handed in each frame) → velocity.
 *   - Integrate position in WORLD (pixel) space, dt-scaled.
 *   - Collide against the tile array already received from /map
 *     (0 = walkable, 1 = blocked, 2 = campsite overlay → still walkable).
 *   - Drive a Phaser camera to follow the player.
 *
 * Coordinates: the sim works in pixel space; tile <-> pixel conversion uses
 * TILE_SIZE. The player's collision box is a square slightly smaller than a tile
 * so corners feel forgiving (classic top-down game-feel).
 */

import type Phaser from "phaser";
import { TILE_SIZE } from "./OverworldScene";

/** Movement intent for a single frame, normalised to [-1, 1] per axis. */
export interface MoveIntent {
  dx: number;
  dy: number;
}

export interface WorldSimConfig {
  /** Tile grid: 0 = walkable, 1 = blocked, 2 = campsite (walkable overlay). */
  tiles: number[][];
  width: number; // tiles
  height: number; // tiles
  /** Start position in TILE coords. */
  startTileX: number;
  startTileY: number;
}

/**
 * KNOBS — tune for game-feel. Exposed as a const so a teammate can lift these
 * into a settings panel / per-biome modifier later.
 */
export const WORLD_SIM_KNOBS = {
  /** Player speed in pixels/second. */
  playerSpeed: 150,
  /** Collision box inset (px) on each side vs a full tile — corner forgiveness. */
  collisionInset: 6,
  /** Camera lerp factor (0..1); higher = snappier follow. */
  cameraLerp: 0.12,
};

export class WorldSim {
  private tiles: number[][];
  private readonly widthPx: number;
  private readonly heightPx: number;

  /** Player center in WORLD (pixel) space. */
  public x: number;
  public y: number;

  /** Last integrated velocity (px/s) — exposed for facing/animation hooks. */
  public vx = 0;
  public vy = 0;

  constructor(cfg: WorldSimConfig) {
    this.tiles = cfg.tiles;
    this.widthPx = cfg.width * TILE_SIZE;
    this.heightPx = cfg.height * TILE_SIZE;
    this.x = cfg.startTileX * TILE_SIZE + TILE_SIZE / 2;
    this.y = cfg.startTileY * TILE_SIZE + TILE_SIZE / 2;
  }

  /** Hot-swap tiles (e.g. after a scene transition reloads the map). */
  setTiles(tiles: number[][]) {
    this.tiles = tiles;
  }

  /** Current player position in TILE coords (rounded to the occupied tile). */
  get tileX(): number {
    return Math.floor(this.x / TILE_SIZE);
  }
  get tileY(): number {
    return Math.floor(this.y / TILE_SIZE);
  }

  /**
   * True if the given TILE is blocked (1) or out of bounds. Campsite tiles (2)
   * and walkable tiles (0) are passable.
   */
  isBlockedTile(tx: number, ty: number): boolean {
    if (ty < 0 || ty >= this.tiles.length) return true;
    if (tx < 0 || tx >= this.tiles[0].length) return true;
    return this.tiles[ty][tx] === 1;
  }

  /**
   * True if a player-sized AABB centered at (cx, cy) in pixel space overlaps any
   * blocked tile. Samples the four corners of the inset collision box — enough
   * for a box smaller than one tile.
   */
  private collidesAt(cx: number, cy: number): boolean {
    const half = TILE_SIZE / 2 - WORLD_SIM_KNOBS.collisionInset;
    const corners: [number, number][] = [
      [cx - half, cy - half],
      [cx + half, cy - half],
      [cx - half, cy + half],
      [cx + half, cy + half],
    ];
    for (const [px, py] of corners) {
      const tx = Math.floor(px / TILE_SIZE);
      const ty = Math.floor(py / TILE_SIZE);
      if (this.isBlockedTile(tx, ty)) return true;
    }
    return false;
  }

  /**
   * Integrate one frame.
   *
   * @param intent  normalised movement intent for this frame.
   * @param deltaMs Phaser frame delta in milliseconds.
   *
   * Axes are resolved INDEPENDENTLY (move X, then Y) so sliding along walls
   * works — a diagonal into a wall keeps the unblocked component instead of
   * dead-stopping. Diagonal input is length-normalised so diagonal speed matches
   * cardinal speed.
   */
  update(intent: MoveIntent, deltaMs: number): void {
    const dt = deltaMs / 1000;

    let { dx, dy } = intent;
    const len = Math.hypot(dx, dy);
    if (len > 1e-3) {
      dx /= len;
      dy /= len;
    } else {
      dx = 0;
      dy = 0;
    }

    const speed = WORLD_SIM_KNOBS.playerSpeed;
    this.vx = dx * speed;
    this.vy = dy * speed;

    // --- X axis ---
    let nx = this.x + this.vx * dt;
    nx = Math.max(TILE_SIZE / 2, Math.min(this.widthPx - TILE_SIZE / 2, nx));
    if (!this.collidesAt(nx, this.y)) {
      this.x = nx;
    } else {
      this.vx = 0;
    }

    // --- Y axis ---
    let ny = this.y + this.vy * dt;
    ny = Math.max(TILE_SIZE / 2, Math.min(this.heightPx - TILE_SIZE / 2, ny));
    if (!this.collidesAt(this.x, ny)) {
      this.y = ny;
    } else {
      this.vy = 0;
    }
  }

  /** Apply the player's world position to a sprite (call after update). */
  applyToSprite(sprite: Phaser.GameObjects.Sprite): void {
    sprite.setPosition(this.x, this.y);
  }

  /**
   * Wire a Phaser camera to follow a sprite within the world bounds. Call once
   * after the player sprite exists; Phaser's startFollow handles per-frame lerp.
   */
  attachCamera(
    camera: Phaser.Cameras.Scene2D.Camera,
    target: Phaser.GameObjects.Sprite
  ): void {
    camera.setBounds(0, 0, this.widthPx, this.heightPx);
    camera.startFollow(
      target,
      true,
      WORLD_SIM_KNOBS.cameraLerp,
      WORLD_SIM_KNOBS.cameraLerp
    );
  }
}
