/**
 * PostFX — atmosphere overlays applied to the overworld scene.
 *
 *   1. Vignette — a soft edge dim attached to the camera so edges fade, hides
 *      the hard map border, adds "depth."
 *   2. Day/night cycle (V2) — a full-screen grade rectangle animated through
 *      dawn → noon → dusk → night via update(time). Night is FLOORED to a cool
 *      blue at moderate alpha (never black) so the world stays readable —
 *      "don't make dark too dark". Noon is fully clear.
 *
 * Both are toggleable for verification (toggle and compare).
 */
import Phaser from "phaser";

const VIGNETTE_EDGE_FRAC = 0.22; // how far the dark frame reaches inward (fraction of viewport)
const VIGNETTE_MAX_ALPHA = 0.78; // darkness at the very edge

/** Full real-time duration (ms) of one dawn→night→dawn cycle. */
const DAY_CYCLE_MS = 90_000;
/** Start mid-morning so the first frame the player sees is bright. */
const DAY_START_PHASE = 0.4;

interface GradeKey {
  /** Cycle phase in [0,1). */
  t: number;
  /** Grade tint (0xRRGGBB) multiplied over the scene. */
  color: number;
  /** Grade alpha — night floored so it never goes fully dark. */
  alpha: number;
}

// Keyframes around the clock. Night (~0.0/1.0) is a cool blue capped at 0.42
// alpha; noon (~0.5) is clear; dawn/dusk are warm. Linearly interpolated.
const DAY_KEYS: GradeKey[] = [
  { t: 0.0, color: 0x0a1530, alpha: 0.42 }, // deep night (floored, not black)
  { t: 0.22, color: 0x0c1838, alpha: 0.4 }, // late night
  { t: 0.3, color: 0xff8a4c, alpha: 0.2 }, // dawn, warm
  { t: 0.42, color: 0xffffff, alpha: 0.0 }, // morning, clear
  { t: 0.58, color: 0xffffff, alpha: 0.0 }, // afternoon, clear
  { t: 0.7, color: 0xff6a30, alpha: 0.24 }, // dusk, warm/red
  { t: 0.82, color: 0x16224e, alpha: 0.4 }, // nightfall, cool
  { t: 1.0, color: 0x0a1530, alpha: 0.42 }, // wrap → deep night
];

/** Channel-wise lerp between two 0xRRGGBB colors. */
function lerpColor(a: number, b: number, f: number): number {
  const ar = (a >> 16) & 0xff;
  const ag = (a >> 8) & 0xff;
  const ab = a & 0xff;
  const r = Math.round(ar + (((b >> 16) & 0xff) - ar) * f);
  const g = Math.round(ag + (((b >> 8) & 0xff) - ag) * f);
  const bl = Math.round(ab + ((b & 0xff) - ab) * f);
  return (r << 16) | (g << 8) | bl;
}

/** Sample the day/night grade (color + alpha) at cycle phase t in [0,1). */
function sampleGrade(t: number): { color: number; alpha: number } {
  for (let i = 0; i < DAY_KEYS.length - 1; i++) {
    const a = DAY_KEYS[i];
    const b = DAY_KEYS[i + 1];
    if (t >= a.t && t <= b.t) {
      const f = b.t === a.t ? 0 : (t - a.t) / (b.t - a.t);
      return { color: lerpColor(a.color, b.color, f), alpha: a.alpha + (b.alpha - a.alpha) * f };
    }
  }
  return { color: DAY_KEYS[0].color, alpha: DAY_KEYS[0].alpha };
}

export class PostFX {
  private vignette?: Phaser.GameObjects.Graphics;
  private grade?: Phaser.GameObjects.Rectangle;
  private enabled = true;

  attach(scene: Phaser.Scene): void {
    const cam = scene.cameras.main;
    const w = cam.width;
    const h = cam.height;

    this.vignette = scene.add.graphics();
    this.vignette.setScrollFactor(0);
    this.vignette.setDepth(1000);
    this.drawVignette(this.vignette, w, h);

    const g0 = sampleGrade(DAY_START_PHASE);
    this.grade = scene.add.rectangle(w / 2, h / 2, w, h, g0.color, g0.alpha);
    this.grade.setScrollFactor(0);
    this.grade.setDepth(999);
  }

  /**
   * Advance the day/night cycle. `timeMs` is the scene clock; the grade colour +
   * alpha are sampled from the keyframes so the sky warms at dawn, clears at noon,
   * reddens at dusk, and settles to a readable (floored) cool night.
   */
  update(timeMs: number): void {
    if (!this.grade || !this.enabled) return;
    const phase = ((DAY_START_PHASE + timeMs / DAY_CYCLE_MS) % 1 + 1) % 1;
    const { color, alpha } = sampleGrade(phase);
    this.grade.setFillStyle(color, alpha);
  }

  setEnabled(on: boolean): void {
    this.enabled = on;
    this.vignette?.setVisible(on);
    this.grade?.setVisible(on);
  }

  toggle(): boolean {
    this.setEnabled(!this.enabled);
    return this.enabled;
  }

  destroy(): void {
    this.vignette?.destroy();
    this.grade?.destroy();
    this.vignette = undefined;
    this.grade = undefined;
  }

  private drawVignette(g: Phaser.GameObjects.Graphics, w: number, h: number): void {
    g.clear();
    // Four edge-fade gradient bars. Each bar is the full edge length and goes
    // from dark-on-the-edge to transparent-inward, layered so corners stack to
    // the darkest. fillGradientStyle takes 4 corner colors+alphas (TL/TR/BL/BR).
    const edgeX = Math.round(w * VIGNETTE_EDGE_FRAC);
    const edgeY = Math.round(h * VIGNETTE_EDGE_FRAC);
    const dark = 0x000000;
    const A = VIGNETTE_MAX_ALPHA;

    // top
    g.fillGradientStyle(dark, dark, dark, dark, A, A, 0, 0);
    g.fillRect(0, 0, w, edgeY);
    // bottom
    g.fillGradientStyle(dark, dark, dark, dark, 0, 0, A, A);
    g.fillRect(0, h - edgeY, w, edgeY);
    // left
    g.fillGradientStyle(dark, dark, dark, dark, A, 0, A, 0);
    g.fillRect(0, 0, edgeX, h);
    // right
    g.fillGradientStyle(dark, dark, dark, dark, 0, A, 0, A);
    g.fillRect(w - edgeX, 0, edgeX, h);
  }
}
