/**
 * EnemyAI — roaming wild-enemy runtime for the overworld (Track B, Wave 1).
 *
 * Today's enemies are static (drawn once from /map). This module turns each into
 * a live entity driven by a small finite-state machine that runs every frame in
 * the scene's update loop:
 *
 *   IDLE   → stand still for a beat, then pick a wander target.
 *   WANDER → drift toward a random nearby walkable tile at reduced speed.
 *   CHASE  → if the player enters the aggro radius, home in at full speed.
 *
 * Collision with the player still triggers the encounter (POST /api/encounters
 * via EncounterTrigger) — the scene checks proximity and calls onEncounter.
 *
 * DETERMINISM NOTE (per design doc): the real-time FSM is intentionally NOT
 * frame-deterministic and is OUT OF SCOPE for the world determinism guarantee.
 * What stays replayable is the world LAYOUT (tiles/POIs) and the SPAWN positions,
 * which are seeded. Runtime motion is free to vary.
 */

import type Phaser from "phaser";
import { TILE_SIZE } from "./constants";

export type EnemyFsmState = "idle" | "wander" | "chase";

export interface EnemySpawn {
  id: string;
  /** Spawn position in TILE coords (seeded server-side via /map). */
  tileX: number;
  tileY: number;
}

/**
 * KNOBS — tune for difficulty / game-feel. A teammate can later make these
 * per-enemy-archetype (e.g. fast skittish vs slow tanky) or per-biome.
 *
 * TODO(teammate): lift into a per-archetype table keyed by monster type once the
 * party/monster type metadata is plumbed through /map enemies.
 */
export const ENEMY_AI_KNOBS = {
  /** Chase speed (px/s). Should be < player speed so the player can escape. */
  chaseSpeed: 110,
  /** Wander drift speed (px/s). */
  wanderSpeed: 45,
  /** Player must be within this many PIXELS to trigger CHASE. */
  aggroRadiusPx: 4 * TILE_SIZE,
  /** Once chasing, give up if the player gets this far away (hysteresis). */
  deAggroRadiusPx: 6 * TILE_SIZE,
  /** How long to sit IDLE before picking a new wander target (ms). */
  wanderIntervalMs: 1400,
  /** Max wander hop distance in tiles from current position. */
  wanderRangeTiles: 3,
  /** Distance (px) at which a wander target counts as "reached". */
  arriveEpsilonPx: 4,
  /** Player proximity (px) that fires the encounter. */
  encounterRadiusPx: TILE_SIZE * 0.7,
};

/** Pluggable blocked-tile test (WorldSim provides this so collision rules match). */
export type BlockedTileFn = (tileX: number, tileY: number) => boolean;

export class Enemy {
  readonly id: string;
  /** Center position in WORLD (pixel) space. */
  x: number;
  y: number;
  state: EnemyFsmState = "idle";

  private stateTimer = 0; // ms accumulated in current state
  private targetX = 0; // wander target (px)
  private targetY = 0;

  constructor(spawn: EnemySpawn) {
    this.id = spawn.id;
    this.x = spawn.tileX * TILE_SIZE + TILE_SIZE / 2;
    this.y = spawn.tileY * TILE_SIZE + TILE_SIZE / 2;
    this.targetX = this.x;
    this.targetY = this.y;
  }

  private dist(px: number, py: number): number {
    return Math.hypot(this.x - px, this.y - py);
  }

  /**
   * Advance the FSM one frame.
   *
   * @param deltaMs   frame delta (ms).
   * @param playerX/Y player center (px) — for aggro/chase.
   * @param isBlocked blocked-tile predicate (from WorldSim).
   * @param rng       0..1 source (Math.random by default; injectable for tests).
   */
  update(
    deltaMs: number,
    playerX: number,
    playerY: number,
    isBlocked: BlockedTileFn,
    rng: () => number = Math.random
  ): void {
    this.stateTimer += deltaMs;
    const dt = deltaMs / 1000;
    const toPlayer = this.dist(playerX, playerY);

    // --- Transition: aggro / de-aggro ---
    if (this.state === "chase") {
      if (toPlayer > ENEMY_AI_KNOBS.deAggroRadiusPx) {
        this.enter("idle");
      }
    } else if (toPlayer <= ENEMY_AI_KNOBS.aggroRadiusPx) {
      this.enter("chase");
    }

    switch (this.state) {
      case "idle":
        // Sit still, then promote to WANDER and pick a target.
        if (this.stateTimer >= ENEMY_AI_KNOBS.wanderIntervalMs) {
          this.pickWanderTarget(isBlocked, rng);
          this.enter("wander");
        }
        break;

      case "wander":
        this.moveToward(
          this.targetX,
          this.targetY,
          ENEMY_AI_KNOBS.wanderSpeed,
          dt,
          isBlocked
        );
        if (
          this.dist(this.targetX, this.targetY) <=
          ENEMY_AI_KNOBS.arriveEpsilonPx
        ) {
          this.enter("idle");
        }
        break;

      case "chase":
        this.moveToward(
          playerX,
          playerY,
          ENEMY_AI_KNOBS.chaseSpeed,
          dt,
          isBlocked
        );
        break;
    }
  }

  /** True if the player is close enough to start a battle. */
  touchesPlayer(playerX: number, playerY: number): boolean {
    return this.dist(playerX, playerY) <= ENEMY_AI_KNOBS.encounterRadiusPx;
  }

  private enter(state: EnemyFsmState): void {
    this.state = state;
    this.stateTimer = 0;
  }

  /**
   * Pick a random nearby walkable tile as the next wander target.
   * Falls back to staying put if no walkable tile is found in a few tries.
   * TODO(teammate): bias wander toward biome-appropriate terrain / away from POIs.
   */
  private pickWanderTarget(isBlocked: BlockedTileFn, rng: () => number): void {
    const curTx = Math.floor(this.x / TILE_SIZE);
    const curTy = Math.floor(this.y / TILE_SIZE);
    const range = ENEMY_AI_KNOBS.wanderRangeTiles;
    for (let attempt = 0; attempt < 8; attempt++) {
      const ox = Math.round((rng() * 2 - 1) * range);
      const oy = Math.round((rng() * 2 - 1) * range);
      const tx = curTx + ox;
      const ty = curTy + oy;
      if (!isBlocked(tx, ty)) {
        this.targetX = tx * TILE_SIZE + TILE_SIZE / 2;
        this.targetY = ty * TILE_SIZE + TILE_SIZE / 2;
        return;
      }
    }
    // No free tile found — target self (will fall back to idle next frame).
    this.targetX = this.x;
    this.targetY = this.y;
  }

  /**
   * Move toward (tx, ty) at `speed` px/s, resolving each axis independently so
   * the enemy slides along walls instead of sticking (mirrors WorldSim).
   */
  private moveToward(
    tx: number,
    ty: number,
    speed: number,
    dt: number,
    isBlocked: BlockedTileFn
  ): void {
    let dx = tx - this.x;
    let dy = ty - this.y;
    const len = Math.hypot(dx, dy);
    if (len < 1e-3) return;
    dx /= len;
    dy /= len;

    const nx = this.x + dx * speed * dt;
    if (!this.tileBlockedAtPixel(nx, this.y, isBlocked)) this.x = nx;

    const ny = this.y + dy * speed * dt;
    if (!this.tileBlockedAtPixel(this.x, ny, isBlocked)) this.y = ny;
  }

  private tileBlockedAtPixel(
    px: number,
    py: number,
    isBlocked: BlockedTileFn
  ): boolean {
    return isBlocked(Math.floor(px / TILE_SIZE), Math.floor(py / TILE_SIZE));
  }
}

/**
 * EnemyManager — owns the live enemy entities + their sprites, ticks the FSM
 * each frame, and reports the first enemy currently touching the player (so the
 * scene can fire the encounter).
 */
export class EnemyManager {
  private enemies: Map<string, Enemy> = new Map();
  private sprites: Map<string, Phaser.GameObjects.Sprite> = new Map();
  private labels: Map<string, Phaser.GameObjects.Text> = new Map();

  /** Build entities from seeded spawns. Pass a sprite factory from the scene. */
  spawn(
    spawns: EnemySpawn[],
    makeSprite: (enemy: Enemy) => Phaser.GameObjects.Sprite,
    makeLabel?: (enemy: Enemy) => Phaser.GameObjects.Text
  ): void {
    for (const s of spawns) {
      if (this.enemies.has(s.id)) continue;
      const enemy = new Enemy(s);
      this.enemies.set(s.id, enemy);
      this.sprites.set(s.id, makeSprite(enemy));
      if (makeLabel) this.labels.set(s.id, makeLabel(enemy));
    }
  }

  /**
   * Tick all enemies. Returns the id of the first enemy touching the player, or
   * null. The scene removes that enemy (battle started) via remove().
   */
  update(
    deltaMs: number,
    playerX: number,
    playerY: number,
    isBlocked: BlockedTileFn
  ): string | null {
    let collided: string | null = null;
    for (const enemy of this.enemies.values()) {
      enemy.update(deltaMs, playerX, playerY, isBlocked);
      const spr = this.sprites.get(enemy.id);
      if (spr) spr.setPosition(enemy.x, enemy.y);
      const lbl = this.labels.get(enemy.id);
      if (lbl) {
        lbl.setPosition(enemy.x, enemy.y - TILE_SIZE * 0.65);
        lbl.setColor(enemy.state === "chase" ? "#ff5d6c" : "#e8e6d8");
      }
      if (collided === null && enemy.touchesPlayer(playerX, playerY)) {
        collided = enemy.id;
      }
    }
    return collided;
  }

  /** Visit each live enemy's world position (used to draw blob shadows). */
  forEach(cb: (x: number, y: number) => void): void {
    for (const enemy of this.enemies.values()) cb(enemy.x, enemy.y);
  }

  /** Remove an enemy + its sprite (e.g. after it triggers an encounter). */
  remove(id: string): void {
    const spr = this.sprites.get(id);
    if (spr) spr.destroy();
    this.sprites.delete(id);
    const lbl = this.labels.get(id);
    if (lbl) lbl.destroy();
    this.labels.delete(id);
    this.enemies.delete(id);
  }

  /** Tear everything down (scene shutdown). */
  destroy(): void {
    for (const spr of this.sprites.values()) spr.destroy();
    this.sprites.clear();
    for (const lbl of this.labels.values()) lbl.destroy();
    this.labels.clear();
    this.enemies.clear();
  }
}
