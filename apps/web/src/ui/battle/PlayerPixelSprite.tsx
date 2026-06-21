import { useEffect, useRef } from "react";
import { drawFrontFrame, type PixelPainter } from "../../game/PlayerAnimator";

const SPRITE_SIZE = 16;
const SPRITE_SCALE = 3.5;
const DISPLAY_SIZE = Math.round(SPRITE_SIZE * SPRITE_SCALE);

/**
 * Renders the overworld player avatar (cyan knight) on an HTML canvas using
 * the same pixel-painting logic as the Phaser scene, so battle and overworld
 * always show the identical sprite.
 */
export function PlayerPixelSprite() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.clearRect(0, 0, SPRITE_SIZE, SPRITE_SIZE);
    const px: PixelPainter = (color, x, y, w = 1, h = 1) => {
      ctx.fillStyle = `#${color.toString(16).padStart(6, "0")}`;
      ctx.fillRect(x, y, w, h);
    };
    drawFrontFrame(px, 0);
  }, []);

  return (
    <div style={{ width: DISPLAY_SIZE, height: DISPLAY_SIZE, position: "relative" }}>
      <canvas
        ref={canvasRef}
        width={SPRITE_SIZE}
        height={SPRITE_SIZE}
        style={{
          width: DISPLAY_SIZE,
          height: DISPLAY_SIZE,
          imageRendering: "pixelated",
          filter: "drop-shadow(2px 3px 0 rgba(0,0,0,0.6))",
          position: "absolute",
          top: 0,
          left: 0,
        }}
      />
    </div>
  );
}
