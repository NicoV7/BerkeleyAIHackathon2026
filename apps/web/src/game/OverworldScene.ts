/**
 * OverworldScene — Pokémon-style tile overworld in Phaser 3.
 *
 * - Grid-based movement (arrow keys / WASD), one tile per keypress.
 * - Procedural coloured-rectangle tiles (no art dependency).
 * - Calls POST /api/runs/{id}/move on each step.
 * - Emits "encounter" event with wild-monster id when a collision is detected.
 */

import Phaser from "phaser";

export const TILE_SIZE = 32;

export interface OverworldConfig {
  runId: string;
  onEncounter: (wildId: string) => void;
}

interface TileEnemy {
  id: string;
  x: number;
  y: number;
  sprite: string;
}

interface MapState {
  width: number;
  height: number;
  tiles: number[][];
  player_x: number;
  player_y: number;
  enemies: TileEnemy[];
}

interface MoveResult {
  player_x: number;
  player_y: number;
  encounter_id: string | null;
}

export class OverworldScene extends Phaser.Scene {
  private cfg!: OverworldConfig;

  // Map data (loaded once)
  private mapData: MapState | null = null;

  // Graphics objects
  private tileGraphics!: Phaser.GameObjects.Graphics;
  private playerSprite!: Phaser.GameObjects.Sprite;
  private enemySprites: Map<string, Phaser.GameObjects.Sprite> = new Map();

  // Input
  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys;
  private wasd!: {
    up: Phaser.Input.Keyboard.Key;
    down: Phaser.Input.Keyboard.Key;
    left: Phaser.Input.Keyboard.Key;
    right: Phaser.Input.Keyboard.Key;
  };

  // Movement throttle
  private moving = false;
  private moveDelay = 150; // ms between moves when key held

  // Player logical position
  private px = 1;
  private py = 1;

  constructor() {
    super({ key: "OverworldScene" });
  }

  init(data: OverworldConfig) {
    this.cfg = data;
  }

  preload() {
    // No external assets — everything is drawn procedurally.
  }

  /**
   * Bake two tiny 16×16 pixel-art textures with Graphics.generateTexture.
   * pixelArt upscaling keeps the chunky pixels crisp at TILE_SIZE.
   */
  private bakeSprites() {
    const SIZE = 16;

    // px helper: paint a filled pixel rect on a graphics object.
    const px = (
      g: Phaser.GameObjects.Graphics,
      color: number,
      x: number,
      y: number,
      w = 1,
      h = 1
    ) => {
      g.fillStyle(color, 1);
      g.fillRect(x, y, w, h);
    };

    // --- Player: a little cyan knight blob (body, head, eyes, gold sword) ---
    if (!this.textures.exists("player")) {
      const g = this.make.graphics({ x: 0, y: 0 }, false);
      // body
      px(g, 0x5cc8ff, 5, 8, 6, 6);
      // head
      px(g, 0x5cc8ff, 5, 3, 6, 5);
      // ink eyes
      px(g, 0x0e1018, 6, 5);
      px(g, 0x0e1018, 9, 5);
      // ink feet
      px(g, 0x0e1018, 5, 14, 2, 2);
      px(g, 0x0e1018, 9, 14, 2, 2);
      // gold sword down the right side
      px(g, 0xffcf3f, 12, 6, 1, 7);
      px(g, 0xffcf3f, 11, 12, 3, 1);
      g.generateTexture("player", SIZE, SIZE);
      g.destroy();
    }

    // --- Enemy: a menacing rose blob (body + gold accent + ink eyes) ---
    if (!this.textures.exists("enemy")) {
      const g = this.make.graphics({ x: 0, y: 0 }, false);
      // round-ish body
      px(g, 0xff5d6c, 4, 5, 8, 8);
      px(g, 0xff5d6c, 5, 3, 6, 2);
      px(g, 0xff5d6c, 3, 7, 1, 4);
      px(g, 0xff5d6c, 12, 7, 1, 4);
      // little horns (gold accent)
      px(g, 0xffcf3f, 4, 2);
      px(g, 0xffcf3f, 11, 2);
      // ink eyes
      px(g, 0x0e1018, 6, 7, 1, 2);
      px(g, 0x0e1018, 9, 7, 1, 2);
      g.generateTexture("enemy", SIZE, SIZE);
      g.destroy();
    }
  }

  async create() {
    this.tileGraphics = this.add.graphics();
    this.cameras.main.setBackgroundColor("#1a1a2e");

    // Bake the procedural pixel-art sprites once (no external assets).
    this.bakeSprites();

    // Player sprite (cyan hero — matches --party). pixelArt upscaling keeps it crisp.
    this.playerSprite = this.add.sprite(0, 0, "player");
    this.playerSprite.setDisplaySize(TILE_SIZE - 4, TILE_SIZE - 4);
    this.playerSprite.setDepth(10);

    // Input
    this.cursors = this.input.keyboard!.createCursorKeys();
    this.wasd = {
      up: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.W),
      down: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.S),
      left: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.A),
      right: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.D),
    };

    // Load initial map state
    await this.fetchMapAndDraw();
  }

  private async fetchMapAndDraw() {
    try {
      const res = await fetch(`/api/runs/${this.cfg.runId}/map`);
      if (!res.ok) return;
      this.mapData = (await res.json()) as MapState;
      this.px = this.mapData.player_x;
      this.py = this.mapData.player_y;
      this.drawMap();
      this.drawEnemies(this.mapData.enemies);
      this.positionPlayer();
    } catch (e) {
      console.error("Failed to fetch map:", e);
    }
  }

  private drawMap() {
    if (!this.mapData) return;
    const g = this.tileGraphics;
    g.clear();

    for (let y = 0; y < this.mapData.height; y++) {
      for (let x = 0; x < this.mapData.width; x++) {
        const blocked = this.mapData.tiles[y][x] === 1;
        const px = x * TILE_SIZE;
        const py = y * TILE_SIZE;

        // Tile fill (desaturated grass / darker walls)
        g.fillStyle(blocked ? 0x2a3326 : 0x33402a, 1);
        g.fillRect(px, py, TILE_SIZE, TILE_SIZE);

        // Subtle grid lines
        g.lineStyle(1, blocked ? 0x1b2118 : 0x222c1c, 0.6);
        g.strokeRect(px, py, TILE_SIZE, TILE_SIZE);

        // Darker wall "rock" texture
        if (blocked) {
          g.fillStyle(0x1b2118, 0.85);
          g.fillRect(px + 6, py + 6, TILE_SIZE - 12, TILE_SIZE - 12);
        }
      }
    }
  }

  private drawEnemies(enemies: TileEnemy[]) {
    // Remove stale sprites
    for (const [id, spr] of this.enemySprites) {
      if (!enemies.find((e) => e.id === id)) {
        spr.destroy();
        this.enemySprites.delete(id);
      }
    }

    for (const enemy of enemies) {
      if (!this.enemySprites.has(enemy.id)) {
        const ex = enemy.x * TILE_SIZE + TILE_SIZE / 2;
        const ey = enemy.y * TILE_SIZE + TILE_SIZE / 2;
        const spr = this.add.sprite(ex, ey, "enemy");
        spr.setDisplaySize(TILE_SIZE - 6, TILE_SIZE - 6);
        spr.setDepth(5);

        // Pulsing animation (relative to the baked display scale)
        const baseScale = spr.scaleX;
        this.tweens.add({
          targets: spr,
          scaleX: baseScale * 1.15,
          scaleY: baseScale * 1.15,
          duration: 700,
          yoyo: true,
          repeat: -1,
          ease: "Sine.easeInOut",
        });

        this.enemySprites.set(enemy.id, spr);
      }
    }
  }

  private positionPlayer() {
    const wx = this.px * TILE_SIZE + TILE_SIZE / 2;
    const wy = this.py * TILE_SIZE + TILE_SIZE / 2;
    this.playerSprite.setPosition(wx, wy);

    // Camera follows player with padding
    if (this.mapData) {
      const mapW = this.mapData.width * TILE_SIZE;
      const mapH = this.mapData.height * TILE_SIZE;
      this.cameras.main.setBounds(0, 0, mapW, mapH);
      this.cameras.main.startFollow(this.playerSprite, true, 0.1, 0.1);
    }
  }

  update(_time: number, _delta: number) {
    if (this.moving || !this.mapData) return;

    let dx = 0;
    let dy = 0;

    if (
      Phaser.Input.Keyboard.JustDown(this.cursors.left) ||
      Phaser.Input.Keyboard.JustDown(this.wasd.left)
    ) {
      dx = -1;
    } else if (
      Phaser.Input.Keyboard.JustDown(this.cursors.right) ||
      Phaser.Input.Keyboard.JustDown(this.wasd.right)
    ) {
      dx = 1;
    } else if (
      Phaser.Input.Keyboard.JustDown(this.cursors.up) ||
      Phaser.Input.Keyboard.JustDown(this.wasd.up)
    ) {
      dy = -1;
    } else if (
      Phaser.Input.Keyboard.JustDown(this.cursors.down) ||
      Phaser.Input.Keyboard.JustDown(this.wasd.down)
    ) {
      dy = 1;
    }

    if (dx !== 0 || dy !== 0) {
      void this.doMove(dx, dy);
    }
  }

  private async doMove(dx: number, dy: number) {
    this.moving = true;
    try {
      const res = await fetch(`/api/runs/${this.cfg.runId}/move`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ dx, dy }),
      });
      if (!res.ok) return;
      const data = (await res.json()) as MoveResult;

      this.px = data.player_x;
      this.py = data.player_y;
      this.positionPlayer();

      // Flash movement tween
      this.tweens.add({
        targets: this.playerSprite,
        alpha: 0.6,
        duration: 60,
        yoyo: true,
      });

      if (data.encounter_id) {
        // Remove the collided enemy sprite
        const collided = this.mapData?.enemies.find(
          (e) => e.id === data.encounter_id
        );
        if (collided) {
          const spr = this.enemySprites.get(collided.id);
          if (spr) {
            spr.destroy();
            this.enemySprites.delete(collided.id);
          }
          // Update local map data
          if (this.mapData) {
            this.mapData.enemies = this.mapData.enemies.filter(
              (e) => e.id !== collided.id
            );
          }
        }

        // Notify React wrapper
        this.cfg.onEncounter(data.encounter_id);
      }
    } catch (e) {
      console.error("Move error:", e);
    } finally {
      // Throttle before next move
      await new Promise<void>((resolve) =>
        this.time.delayedCall(this.moveDelay, resolve)
      );
      this.moving = false;
    }
  }

  /** Called externally to reload the map (e.g., after returning from encounter). */
  async refreshMap() {
    await this.fetchMapAndDraw();
  }

  destroy() {
    this.enemySprites.forEach((spr) => spr.destroy());
    this.enemySprites.clear();
  }
}
