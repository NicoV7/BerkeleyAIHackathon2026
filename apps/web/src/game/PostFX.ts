/**
 * PostFX — atmosphere overlays applied to the overworld scene (Wave 0).
 *
 * Two demo-quality visuals without shaders:
 *   1. Vignette — a soft circular dim attached to the camera so edges fade.
 *      Scrolls with the camera, hides the hard map border, adds "depth."
 *   2. Day/night tint — a single full-screen alpha rectangle the scene can
 *      pulse over time. Default tint is a faint cool blue so the overworld
 *      reads as twilight rather than flat green.
 *
 * Both are toggleable for verification (success criterion #0: confirm the
 * visual upgrade is real by toggling and comparing).
 */
import Phaser from "phaser";

const VIGNETTE_EDGE_FRAC = 0.22; // how far the dark frame reaches inward (fraction of viewport)
const VIGNETTE_MAX_ALPHA = 0.78; // darkness at the very edge
const NIGHT_TINT = 0x0a1530;
const NIGHT_ALPHA = 0.18;

export class PostFX {
  private vignette?: Phaser.GameObjects.Graphics;
  private nightTint?: Phaser.GameObjects.Rectangle;
  private enabled = true;

  attach(scene: Phaser.Scene): void {
    const cam = scene.cameras.main;
    const w = cam.width;
    const h = cam.height;

    this.vignette = scene.add.graphics();
    this.vignette.setScrollFactor(0);
    this.vignette.setDepth(1000);
    this.drawVignette(this.vignette, w, h);

    this.nightTint = scene.add.rectangle(w / 2, h / 2, w, h, NIGHT_TINT, NIGHT_ALPHA);
    this.nightTint.setScrollFactor(0);
    this.nightTint.setDepth(999);
  }

  setEnabled(on: boolean): void {
    this.enabled = on;
    this.vignette?.setVisible(on);
    this.nightTint?.setVisible(on);
  }

  toggle(): boolean {
    this.setEnabled(!this.enabled);
    return this.enabled;
  }

  destroy(): void {
    this.vignette?.destroy();
    this.nightTint?.destroy();
    this.vignette = undefined;
    this.nightTint = undefined;
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
