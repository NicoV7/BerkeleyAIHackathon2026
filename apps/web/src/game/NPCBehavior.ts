import Phaser from "phaser";

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
  baseY: number;
}

export class NPCBehaviorManager {
  private actors: NPCActor[] = [];

  add(anchor: NPCAnchorView, sprite: Phaser.GameObjects.Sprite): void {
    this.actors.push({ anchor, sprite, baseY: sprite.y });
  }

  update(time: number, playerX: number, playerY: number): void {
    for (const actor of this.actors) {
      const dx = playerX - actor.sprite.x;
      actor.sprite.setFlipX(dx < 0);
      actor.sprite.y = actor.baseY + Math.sin(time / 420 + actor.anchor.x) * 1.5;
      actor.sprite.setDepth(playerY > actor.sprite.y ? 9 : 7);
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
    for (const actor of this.actors) actor.sprite.destroy();
    this.actors = [];
  }
}
