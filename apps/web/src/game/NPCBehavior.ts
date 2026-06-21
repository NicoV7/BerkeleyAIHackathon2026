import Phaser from "phaser";
import { TILE_SIZE } from "./constants";

const DEFAULT_INTERACTION_DISTANCE = 20.8;

// C3 — wandering NPCs. Cosmetic, client-seeded (per NPC), non-authoritative: the
// server never sees these positions, so they never touch replay determinism or
// add latency. NPCs amble within a small radius of their home anchor, pausing
// between strolls, and refuse to step onto blocked tiles.
const NPC_WANDER_RADIUS_PX = TILE_SIZE * 2.5;
const NPC_SPEED_PX_S = 22;
const NPC_PAUSE_MIN_MS = 900;
const NPC_PAUSE_MAX_MS = 2600;
const ARRIVE_EPS_PX = 2;

export interface NPCAnchorView {
  npc_id: string;
  archetype: "villager" | "merchant" | "quest_giver" | "figure" | "innkeeper";
  x: number;
  y: number;
  name?: string;
  figure_id?: string | null;
}

interface NPCActor {
  anchor: NPCAnchorView;
  sprite: Phaser.GameObjects.Sprite;
  homeX: number;
  homeY: number;
  curX: number;
  curY: number;
  targetX: number;
  targetY: number;
  pauseUntilMs: number;
  rng: () => number;
}

/** Deterministic [0,1) PRNG seeded per NPC so wander is stable, not jittery. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hashStr(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export class NPCBehaviorManager {
  private actors: NPCActor[] = [];

  add(anchor: NPCAnchorView, sprite: Phaser.GameObjects.Sprite): void {
    this.actors.push({
      anchor,
      sprite,
      homeX: sprite.x,
      homeY: sprite.y,
      curX: sprite.x,
      curY: sprite.y,
      targetX: sprite.x,
      targetY: sprite.y,
      pauseUntilMs: 0,
      rng: mulberry32(hashStr(anchor.npc_id || `${anchor.x},${anchor.y}`)),
    });
  }

  /**
   * Step each NPC's wander + bob. `isBlocked(tileX, tileY)` (global tile coords)
   * keeps cosmetic wander on walkable ground. NPCs face their movement while
   * strolling and the player while paused.
   */
  update(
    time: number,
    deltaMs: number,
    playerX: number,
    playerY: number,
    isBlocked: (tileX: number, tileY: number) => boolean
  ): void {
    const dt = Math.min(deltaMs, 50) / 1000;
    for (const actor of this.actors) {
      const dxT = actor.targetX - actor.curX;
      const dyT = actor.targetY - actor.curY;
      const dist = Math.hypot(dxT, dyT);
      let moving = false;

      if (time >= actor.pauseUntilMs) {
        if (dist <= ARRIVE_EPS_PX) {
          // Arrived → pause, then choose a fresh target near home.
          actor.pauseUntilMs =
            time + NPC_PAUSE_MIN_MS + actor.rng() * (NPC_PAUSE_MAX_MS - NPC_PAUSE_MIN_MS);
          actor.targetX = actor.homeX + (actor.rng() * 2 - 1) * NPC_WANDER_RADIUS_PX;
          actor.targetY = actor.homeY + (actor.rng() * 2 - 1) * NPC_WANDER_RADIUS_PX;
        } else {
          const step = NPC_SPEED_PX_S * dt;
          const nx = actor.curX + (dxT / dist) * step;
          const ny = actor.curY + (dyT / dist) * step;
          if (isBlocked(Math.floor(nx / TILE_SIZE), Math.floor(ny / TILE_SIZE))) {
            // Blocked → drop this target and pause briefly before re-picking.
            actor.pauseUntilMs = time + 300;
            actor.targetX = actor.curX;
            actor.targetY = actor.curY;
          } else {
            actor.curX = nx;
            actor.curY = ny;
            moving = true;
          }
        }
      }

      actor.sprite.setFlipX(moving ? dxT < 0 : playerX - actor.curX < 0);
      const bob = Math.sin(time / 420 + actor.anchor.x) * 1.2;
      actor.sprite.x = actor.curX;
      actor.sprite.y = actor.curY + bob;
      actor.sprite.setDepth(playerY > actor.sprite.y ? 9 : 7);
    }
  }

  /** Visit each NPC's current world position (used to draw blob shadows). */
  forEach(cb: (x: number, y: number) => void): void {
    for (const actor of this.actors) cb(actor.sprite.x, actor.sprite.y);
  }

  nearest(
    playerX: number,
    playerY: number,
    maxDistance = DEFAULT_INTERACTION_DISTANCE
  ): NPCAnchorView | null {
    let best: { anchor: NPCAnchorView; distance: number } | null = null;
    for (const actor of this.actors) {
      const distance = Phaser.Math.Distance.Between(playerX, playerY, actor.sprite.x, actor.sprite.y);
      if (distance > maxDistance) continue;
      if (!best || distance < best.distance) best = { anchor: actor.anchor, distance };
    }
    return best?.anchor ?? null;
  }

  destroy(): void {
    for (const actor of this.actors) actor.sprite.destroy();
    this.actors = [];
  }
}
