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
  private playerSprite!: Phaser.GameObjects.Rectangle;
  private enemySprites: Map<string, Phaser.GameObjects.Rectangle> = new Map();

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

  async create() {
    this.tileGraphics = this.add.graphics();
    this.cameras.main.setBackgroundColor("#1a1a2e");

    // Player rectangle (blue)
    this.playerSprite = this.add.rectangle(0, 0, TILE_SIZE - 4, TILE_SIZE - 4, 0x4488ff);
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

        // Tile fill
        g.fillStyle(blocked ? 0x2d4a22 : 0x2d5a1e, 1);
        g.fillRect(px, py, TILE_SIZE, TILE_SIZE);

        // Subtle grid lines
        g.lineStyle(1, blocked ? 0x1a2e12 : 0x1e3d14, 0.6);
        g.strokeRect(px, py, TILE_SIZE, TILE_SIZE);

        // Darker wall "rock" texture
        if (blocked) {
          g.fillStyle(0x1a2e12, 0.8);
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
        const spr = this.add.rectangle(ex, ey, TILE_SIZE - 6, TILE_SIZE - 6, 0xff4444);
        spr.setDepth(5);

        // Pulsing animation
        this.tweens.add({
          targets: spr,
          scaleX: 1.15,
          scaleY: 1.15,
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
