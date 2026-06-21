import type Phaser from "phaser";
import { describe, expect, it, vi } from "vitest";
import {
  NPCBehaviorManager,
  QUEST_MARKER_OFFSET,
  type NPCAnchorView,
} from "./NPCBehavior";

function makeAnchor(overrides: Partial<NPCAnchorView> = {}): NPCAnchorView {
  return {
    npc_id: "quest-giver",
    archetype: "quest_giver",
    x: 0,
    y: 0,
    ...overrides,
  };
}

function makeSprite(x = 32, y = 48): Phaser.GameObjects.Sprite {
  return {
    x,
    y,
    setFlipX: vi.fn(),
    setDepth: vi.fn(),
    destroy: vi.fn(),
  } as unknown as Phaser.GameObjects.Sprite;
}

function makeMarker(x = 32, y = 30): Phaser.GameObjects.Text {
  return {
    x,
    y,
    setPosition: vi.fn(function (
      this: Phaser.GameObjects.Text,
      nextX: number,
      nextY: number
    ) {
      this.x = nextX;
      this.y = nextY;
      return this;
    }),
    setDepth: vi.fn(),
    destroy: vi.fn(),
  } as unknown as Phaser.GameObjects.Text;
}

describe("NPCBehaviorManager quest markers", () => {
  it("keeps quest markers above their NPC while bobbing", () => {
    const manager = new NPCBehaviorManager();
    const sprite = makeSprite();
    const marker = makeMarker();

    manager.add(makeAnchor(), sprite, null, marker);
    manager.update(0, 64, 80, 80, () => false);

    expect(marker.x).toBe(sprite.x);
    expect(marker.y).toBe(sprite.y - QUEST_MARKER_OFFSET);
    expect(marker.setDepth).toHaveBeenCalledWith(15);
  });

  it("destroys quest markers with their NPC sprites", () => {
    const manager = new NPCBehaviorManager();
    const sprite = makeSprite();
    const marker = makeMarker();

    manager.add(makeAnchor(), sprite, null, marker);
    manager.destroy();

    expect(sprite.destroy).toHaveBeenCalledTimes(1);
    expect(marker.destroy).toHaveBeenCalledTimes(1);
  });

  it("keeps unmarked NPCs on the normal lifecycle", () => {
    const manager = new NPCBehaviorManager();
    const sprite = makeSprite();

    manager.add(makeAnchor({ npc_id: "villager", archetype: "villager" }), sprite);
    manager.update(0, 16, 16, 16, () => false);
    manager.destroy();

    expect(sprite.setDepth).toHaveBeenCalled();
    expect(sprite.destroy).toHaveBeenCalledTimes(1);
  });
});
