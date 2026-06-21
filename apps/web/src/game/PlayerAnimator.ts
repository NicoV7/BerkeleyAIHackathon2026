/**
 * PlayerAnimator centralizes top-down player pose selection and pixel-frame
 * drawing so movement, running, and facing direction stay in one place.
 */

import Phaser from "phaser";

export type PlayerDirection = "down" | "up" | "right" | "left";
export type PlayerMotion = "idle" | "walk" | "run";

export interface PlayerMoveIntent {
  dx: number;
  dy: number;
  running?: boolean;
}

export interface PlayerVelocity {
  vx: number;
  vy: number;
}

export interface PlayerPose {
  direction: PlayerDirection;
  motion: PlayerMotion;
}

const PLAYER_TEXTURE_PREFIX = "player-adventurer";
const FRAME_SEQUENCE = [0, 1, 2, 1] as const;
const IDLE_FRAME = 1;
const WALK_FRAME_MS = 145;
const RUN_FRAME_MS = 82;
const MOVEMENT_EPSILON = 1;
const PLAYER_PALETTE = {
  cyan: 0x5cc8ff,
  cyanDark: 0x2385a8,
  cyanLight: 0x91e4ff,
  skin: 0xe6b58d,
  hair: 0x3d2c22,
  ink: 0x0e1018,
  boot: 0x1b2430,
  gold: 0xffcf3f,
} as const;

type PixelPainter = (color: number, x: number, y: number, w?: number, h?: number) => void;

/** Returns the generated texture key for a directional player frame. */
export function playerTextureKey(direction: PlayerDirection, frame: number): string {
  const sourceDirection = direction === "left" ? "right" : direction;
  return `${PLAYER_TEXTURE_PREFIX}-${sourceDirection}-${frame}`;
}

/** Resolve the direction/motion the player should display this frame. */
export function resolvePlayerPose(input: {
  intent: PlayerMoveIntent;
  velocity: PlayerVelocity;
  previousDirection: PlayerDirection;
}): PlayerPose {
  const velocityMagnitude = Math.hypot(input.velocity.vx, input.velocity.vy);
  const intentMagnitude = Math.hypot(input.intent.dx, input.intent.dy);
  const directionVector =
    velocityMagnitude > MOVEMENT_EPSILON
      ? { dx: input.velocity.vx, dy: input.velocity.vy }
      : { dx: input.intent.dx, dy: input.intent.dy };

  const direction =
    Math.hypot(directionVector.dx, directionVector.dy) > MOVEMENT_EPSILON ||
    intentMagnitude > MOVEMENT_EPSILON
      ? dominantDirection(directionVector, input.previousDirection)
      : input.previousDirection;
  const moving = velocityMagnitude > MOVEMENT_EPSILON || intentMagnitude > MOVEMENT_EPSILON;

  return {
    direction,
    motion: moving ? (input.intent.running ? "run" : "walk") : "idle",
  };
}

/** Pick the current animation frame for a motion state and elapsed time. */
export function animationFrameFor(motion: PlayerMotion, elapsedMs: number): number {
  if (motion === "idle") return IDLE_FRAME;
  const frameMs = motion === "run" ? RUN_FRAME_MS : WALK_FRAME_MS;
  const sequenceIndex = Math.floor(elapsedMs / frameMs) % FRAME_SEQUENCE.length;
  return FRAME_SEQUENCE[sequenceIndex];
}

/**
 * Create the generated pixel-art player frames used by OverworldScene.
 * The left-facing animation mirrors the right-facing texture at runtime.
 */
export function createPlayerTextures(scene: Phaser.Scene): void {
  if (scene.textures.exists(playerTextureKey("down", IDLE_FRAME))) return;

  const graphics = scene.make.graphics({ x: 0, y: 0 }, false);
  for (const direction of ["down", "up", "right"] as const) {
    for (let frame = 0; frame < 3; frame += 1) {
      graphics.clear();
      drawPlayerFrame(graphics, direction, frame);
      graphics.generateTexture(playerTextureKey(direction, frame), 16, 16);
    }
  }
  graphics.destroy();
}

export class PlayerSpriteAnimator {
  private direction: PlayerDirection = "down";
  private motion: PlayerMotion = "idle";
  private elapsedMs = 0;

  constructor(private readonly sprite: Phaser.GameObjects.Sprite) {}

  /** Advance animation state and apply the correct texture to the sprite. */
  update(input: {
    intent: PlayerMoveIntent;
    velocity: PlayerVelocity;
    deltaMs: number;
  }): void {
    const pose = resolvePlayerPose({
      intent: input.intent,
      velocity: input.velocity,
      previousDirection: this.direction,
    });
    if (pose.direction !== this.direction || pose.motion !== this.motion) {
      this.elapsedMs = 0;
    } else {
      this.elapsedMs += input.deltaMs;
    }

    this.direction = pose.direction;
    this.motion = pose.motion;

    const frame = animationFrameFor(this.motion, this.elapsedMs);
    const textureKey = playerTextureKey(this.direction, frame);
    const flipX = this.direction === "left";
    if (this.sprite.texture.key !== textureKey) this.sprite.setTexture(textureKey);
    if (this.sprite.flipX !== flipX) this.sprite.setFlipX(flipX);
  }
}

function dominantDirection(
  vector: { dx: number; dy: number },
  fallback: PlayerDirection
): PlayerDirection {
  const absX = Math.abs(vector.dx);
  const absY = Math.abs(vector.dy);
  if (absX < MOVEMENT_EPSILON && absY < MOVEMENT_EPSILON) return fallback;
  if (absX >= absY) return vector.dx < 0 ? "left" : "right";
  return vector.dy < 0 ? "up" : "down";
}

function drawPlayerFrame(
  graphics: Phaser.GameObjects.Graphics,
  direction: Exclude<PlayerDirection, "left">,
  frame: number
): void {
  const legShift = frame === 0 ? -1 : frame === 2 ? 1 : 0;
  const px: PixelPainter = (color, x, y, w = 1, h = 1) => {
    graphics.fillStyle(color, 1);
    graphics.fillRect(x, y, w, h);
  };

  if (direction === "down") drawFrontFrame(px, legShift);
  else if (direction === "up") drawBackFrame(px, legShift);
  else drawSideFrame(px, legShift);
}

function drawFrontFrame(px: PixelPainter, legShift: number): void {
  const { boot, cyan, cyanDark, cyanLight, gold, hair, ink, skin } = PLAYER_PALETTE;
  px(hair, 4, 1, 8, 2);
  px(skin, 5, 3, 6, 4);
  px(ink, 6, 5);
  px(ink, 9, 5);
  px(cyanDark, 4, 8, 8, 5);
  px(cyan, 5, 7, 6, 6);
  px(cyanLight, 6, 8, 4, 1);
  px(skin, 3, 9 + Math.max(0, legShift), 1, 3);
  px(skin, 12, 9 + Math.max(0, -legShift), 1, 3);
  px(boot, 5 + legShift, 13, 2, 2);
  px(boot, 9 - legShift, 13, 2, 2);
  px(gold, 11, 7, 1, 5);
}

function drawBackFrame(px: PixelPainter, legShift: number): void {
  const { boot, cyan, cyanDark, cyanLight, gold, hair, skin } = PLAYER_PALETTE;
  px(hair, 4, 1, 8, 5);
  px(skin, 5, 5, 6, 2);
  px(cyanDark, 4, 8, 8, 5);
  px(cyan, 5, 7, 6, 6);
  px(cyanLight, 6, 7, 4, 1);
  px(hair, 6, 3, 4, 2);
  px(skin, 3, 9 + Math.max(0, -legShift), 1, 3);
  px(skin, 12, 9 + Math.max(0, legShift), 1, 3);
  px(boot, 5 + legShift, 13, 2, 2);
  px(boot, 9 - legShift, 13, 2, 2);
  px(gold, 4, 8, 1, 4);
}

function drawSideFrame(px: PixelPainter, legShift: number): void {
  const { boot, cyan, cyanDark, cyanLight, gold, hair, ink, skin } = PLAYER_PALETTE;
  px(hair, 5, 1, 6, 2);
  px(skin, 6, 3, 5, 4);
  px(ink, 10, 5);
  px(cyanDark, 5, 8, 7, 5);
  px(cyan, 6, 7, 5, 6);
  px(cyanLight, 7, 8, 3, 1);
  px(skin, 4, 9 + Math.max(0, -legShift), 1, 3);
  px(skin, 12, 9 + Math.max(0, legShift), 1, 3);
  px(boot, 6 + legShift, 13, 2, 2);
  px(boot, 10 - legShift, 13, 2, 2);
  px(gold, 12, 7, 1, 5);
}
