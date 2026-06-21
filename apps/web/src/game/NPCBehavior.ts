import Phaser from "phaser";
import { TILE_SIZE } from "./constants";

const DEFAULT_INTERACTION_DISTANCE = 20.8;

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
  label: Phaser.GameObjects.Text | null;
  baseY: number;
}

export class NPCBehaviorManager {
  private actors: NPCActor[] = [];

  add(
    anchor: NPCAnchorView,
    sprite: Phaser.GameObjects.Sprite,
    label?: Phaser.GameObjects.Text
  ): void {
    this.actors.push({ anchor, sprite, label: label ?? null, baseY: sprite.y });
  }

  update(time: number, playerX: number, playerY: number): void {
    for (const actor of this.actors) {
      const dx = playerX - actor.sprite.x;
      actor.sprite.setFlipX(dx < 0);
      actor.sprite.y = actor.baseY + Math.sin(time / 420 + actor.anchor.x) * 1.5;
      const depth = playerY > actor.sprite.y ? 9 : 7;
      actor.sprite.setDepth(depth);
      if (actor.label) {
        actor.label.setPosition(actor.sprite.x, actor.sprite.y - TILE_SIZE * 0.65);
        actor.label.setDepth(depth + 1);
      }
    }
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
    for (const actor of this.actors) {
      actor.sprite.destroy();
      actor.label?.destroy();
    }
    this.actors = [];
  }
}
