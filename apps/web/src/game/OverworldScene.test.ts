import { describe, expect, it } from "vitest";
import {
  chunkEdgeMarginTilesForViewport,
  chunkWindowTilesForViewport,
} from "./chunkWindow";

describe("overworld chunk window sizing", () => {
  it("keeps the minimum chunk for normal laptop viewports", () => {
    expect(chunkWindowTilesForViewport(1440, 900)).toBe(96);
  });

  it("expands chunks for wide viewports so the camera does not see past terrain", () => {
    expect(chunkWindowTilesForViewport(2560, 1440)).toBe(112);
    expect(chunkWindowTilesForViewport(3840, 2160)).toBe(152);
  });

  it("caps requests at the server-supported maximum", () => {
    expect(chunkWindowTilesForViewport(6000, 3200)).toBe(160);
  });

  it("refreshes before the visible camera reaches the loaded chunk edge", () => {
    expect(chunkEdgeMarginTilesForViewport(1440, 900, 96)).toBe(33);
    expect(chunkEdgeMarginTilesForViewport(2560, 1440, 112)).toBe(50);
    expect(chunkEdgeMarginTilesForViewport(3840, 2160, 152)).toBe(70);
  });
});
