import { describe, expect, it } from "vitest";
import {
  activeQuestTargets,
  clampToRectEdge,
  edgeArrowToTarget,
  isActiveQuest,
  isOnScreen,
  nearestTarget,
  questTargetXY,
  type RunQuest,
} from "./questPin";

describe("questTargetXY", () => {
  it("parses coords from a kind:x:y poi key", () => {
    expect(questTargetXY({ target: "den:13:9" })).toEqual({ x: 13, y: 9 });
    expect(questTargetXY({ target: "town:160:480" })).toEqual({ x: 160, y: 480 });
  });

  it("prefers an explicit target_xy when present", () => {
    expect(questTargetXY({ target: "den:1:1", target_xy: { x: 7, y: 8 } })).toEqual({
      x: 7,
      y: 8,
    });
  });

  it("returns null for an unparseable target", () => {
    expect(questTargetXY({ target: "boss_zorg" })).toBeNull();
    expect(questTargetXY({})).toBeNull();
  });
});

describe("isActiveQuest / activeQuestTargets", () => {
  it("excludes completed quests and unparseable targets", () => {
    const quests: RunQuest[] = [
      { quest_id: "a", target: "den:10:20", status: "accepted" },
      { quest_id: "b", target: "town:5:5", status: "completed" },
      { quest_id: "c", target: "garbage", status: "accepted" },
    ];
    expect(isActiveQuest(quests[0])).toBe(true);
    expect(isActiveQuest(quests[1])).toBe(false);
    const targets = activeQuestTargets(quests);
    expect(targets).toEqual([{ quest_id: "a", x: 10, y: 20 }]);
  });
});

describe("isOnScreen", () => {
  const rect = { originX: 100, originY: 100, width: 50, height: 50 };
  it("true inside, false outside the viewport rect", () => {
    expect(isOnScreen(rect, 120, 120)).toBe(true);
    expect(isOnScreen(rect, 100, 100)).toBe(true);
    expect(isOnScreen(rect, 99, 120)).toBe(false);
    expect(isOnScreen(rect, 150, 120)).toBe(false); // 150 == origin+width (exclusive)
  });
});

describe("edgeArrowToTarget (direction math)", () => {
  it("points straight east for a target to the right", () => {
    const a = edgeArrowToTarget(0, 0, 10, 0)!;
    expect(a.dx).toBeCloseTo(1);
    expect(a.dy).toBeCloseTo(0);
    expect(a.angle).toBeCloseTo(0);
  });

  it("points straight south (screen +y is down) for a target below", () => {
    const a = edgeArrowToTarget(0, 0, 0, 10)!;
    expect(a.dy).toBeCloseTo(1);
    expect(a.angle).toBeCloseTo(Math.PI / 2);
  });

  it("points north-east at 45deg up-right", () => {
    const a = edgeArrowToTarget(0, 0, 5, -5)!;
    expect(a.dx).toBeCloseTo(Math.SQRT1_2);
    expect(a.dy).toBeCloseTo(-Math.SQRT1_2);
    expect(a.angle).toBeCloseTo(-Math.PI / 4);
    expect(a.distanceTiles).toBe(Math.round(Math.hypot(5, 5)));
  });

  it("returns null when the target coincides with the player", () => {
    expect(edgeArrowToTarget(4, 4, 4, 4)).toBeNull();
  });
});

describe("nearestTarget", () => {
  it("picks the closest active target by squared tile distance", () => {
    const targets = [
      { quest_id: "far", x: 100, y: 100 },
      { quest_id: "near", x: 3, y: 4 },
    ];
    expect(nearestTarget(0, 0, targets)?.quest_id).toBe("near");
  });
  it("returns null with no targets", () => {
    expect(nearestTarget(0, 0, [])).toBeNull();
  });
});

describe("clampToRectEdge", () => {
  it("pins a rightward arrow to the right edge (x = halfW)", () => {
    const p = clampToRectEdge(1, 0, 320, 320, 48);
    expect(p.x).toBeCloseTo(320 / 2 - 48);
    expect(p.y).toBeCloseTo(0);
  });

  it("a 45deg direction clamps to whichever edge is reached first", () => {
    const p = clampToRectEdge(Math.SQRT1_2, Math.SQRT1_2, 320, 320, 48);
    // Square box → diagonal hits the corner: |x| == |y| == halfW.
    expect(Math.abs(p.x)).toBeCloseTo(320 / 2 - 48);
    expect(Math.abs(p.y)).toBeCloseTo(320 / 2 - 48);
  });

  it("zero direction stays centred", () => {
    expect(clampToRectEdge(0, 0, 320, 320, 48)).toEqual({ x: 0, y: 0 });
  });
});
