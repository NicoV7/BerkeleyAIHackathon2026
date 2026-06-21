import { describe, expect, it } from "vitest";
import {
  animationFrameFor,
  playerTextureKey,
  resolvePlayerPose,
  type PlayerDirection,
} from "./PlayerAnimator";

function poseFor(params: {
  dx?: number;
  dy?: number;
  vx?: number;
  vy?: number;
  running?: boolean;
  previousDirection?: PlayerDirection;
}) {
  return resolvePlayerPose({
    intent: {
      dx: params.dx ?? 0,
      dy: params.dy ?? 0,
      running: params.running ?? false,
    },
    velocity: {
      vx: params.vx ?? 0,
      vy: params.vy ?? 0,
    },
    previousDirection: params.previousDirection ?? "down",
  });
}

describe("PlayerAnimator", () => {
  it("faces the camera when moving down", () => {
    const pose = poseFor({ dy: 1, vy: 150 });

    expect(pose).toEqual({ direction: "down", motion: "walk" });
  });

  it("shows the player's back when moving up", () => {
    const pose = poseFor({ dy: -1, vy: -150 });

    expect(pose).toEqual({ direction: "up", motion: "walk" });
  });

  it("faces right or left based on horizontal movement", () => {
    expect(poseFor({ dx: 1, vx: 150 }).direction).toBe("right");
    expect(poseFor({ dx: -1, vx: -150 }).direction).toBe("left");
  });

  it("uses run motion when the run key is held during movement", () => {
    const pose = poseFor({ dx: 1, vx: 225, running: true });

    expect(pose).toEqual({ direction: "right", motion: "run" });
  });

  it("keeps the previous facing direction while idle", () => {
    const pose = poseFor({ previousDirection: "up" });

    expect(pose).toEqual({ direction: "up", motion: "idle" });
  });

  it("cycles running frames faster than walking frames", () => {
    expect(animationFrameFor("walk", 145)).toBe(1);
    expect(animationFrameFor("run", 82)).toBe(1);
    expect(animationFrameFor("run", 164)).toBe(2);
  });

  it("mirrors left-facing sprites from the right-facing texture", () => {
    expect(playerTextureKey("left", 1)).toBe(playerTextureKey("right", 1));
  });
});
