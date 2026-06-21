import { afterEach, describe, expect, it, vi } from "vitest";
import { SceneRouter, type InteriorSpec, type RoutablePOI } from "./SceneRouter";

function scenePluginSpy() {
  const calls: Array<{ name: string; key?: string; data?: object }> = [];
  const plugin = {
    calls,
    launch: vi.fn((key?: string, data?: object) => {
      calls.push({ name: "launch", key, data });
      return plugin;
    }),
    pause: vi.fn((key?: string, data?: object) => {
      calls.push({ name: "pause", key, data });
      return plugin;
    }),
    resume: vi.fn((key?: string, data?: object) => {
      calls.push({ name: "resume", key, data });
      return plugin;
    }),
    start: vi.fn((key?: string, data?: object) => {
      calls.push({ name: "start", key, data });
      return plugin;
    }),
    stop: vi.fn((key?: string, data?: object) => {
      calls.push({ name: "stop", key, data });
      return plugin;
    }),
  };
  return plugin as unknown as Phaser.Scenes.ScenePlugin & { calls: typeof calls };
}

function interiorSpec(): InteriorSpec {
  return {
    seed: 1,
    width: 16,
    height: 12,
    regions: [],
    pois: [],
    start: null,
    goal: null,
  };
}

function enterablePoi(): RoutablePOI {
  return {
    kind: "den",
    x: 10,
    y: 20,
    name: "Trial Den",
    interior_seed: 123,
    interior_kind: "dungeon",
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SceneRouter interior transitions", () => {
  it("launches interiors over a paused overworld and resumes the cached overworld on exit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => interiorSpec(),
      }))
    );
    const overworldScene = scenePluginSpy();
    const interiorScene = scenePluginSpy();
    const onExitInterior = vi.fn();
    const router = new SceneRouter({
      runId: "run-1",
      scenePlugin: overworldScene,
      onExitInterior,
    });

    await router.enter(enterablePoi());
    router.exit(interiorScene);

    expect(overworldScene.calls.map((call) => call.name)).toEqual([
      "launch",
      "pause",
      "resume",
    ]);
    expect(overworldScene.launch).toHaveBeenCalledWith(
      "DungeonInteriorScene",
      expect.objectContaining({ runId: "run-1", router })
    );
    expect(overworldScene.resume).toHaveBeenCalledWith("OverworldScene", {
      returnTile: { x: 10, y: 20 },
    });
    expect(overworldScene.start).not.toHaveBeenCalled();
    expect(onExitInterior).toHaveBeenCalledWith({ x: 10, y: 20 });
    expect(interiorScene.calls).toEqual([{ name: "stop", key: undefined, data: undefined }]);
    expect(interiorScene.resume).not.toHaveBeenCalled();
  });
});
