import { describe, expect, it } from "vitest";
import { WorldSim, WORLD_SIM_KNOBS } from "./WorldSim";

function makeOpenWorld(): WorldSim {
  return new WorldSim({
    tiles: Array.from({ length: 10 }, () => Array.from({ length: 10 }, () => 0)),
    width: 10,
    height: 10,
    startTileX: 1,
    startTileY: 1,
  });
}

describe("WorldSim running", () => {
  it("moves farther during the same frame window when running", () => {
    const walking = makeOpenWorld();
    const running = makeOpenWorld();
    const startX = walking.x;

    walking.update({ dx: 1, dy: 0 }, 1000);
    running.update({ dx: 1, dy: 0, running: true }, 1000);

    expect(running.x - startX).toBeCloseTo(
      (walking.x - startX) * WORLD_SIM_KNOBS.playerRunMultiplier
    );
  });
});
