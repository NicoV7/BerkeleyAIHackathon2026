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

// --- Kenney roguelike tileset (apps/web/public/tiles/) ---
// Sheet is 57 cols × 31 rows of 16px tiles, margin 0 / spacing 1.
// index = row * 57 + col (0-based). Indices below were read off the sheet and
// cross-checked against Kenney's own sample_map.tmx. Tune if tiles look wrong.
const SHEET = "/tiles/roguelikeSheet_transparent.png";
const SHEET_TILE = 16; // native tile size in the sheet

// Ground (full opaque tiles): plain green grass + occasional flower-grass.
const GRASS_INDICES = [855, 856, 912, 913];
const FLOWER_INDICES = [541, 542, 543, 544]; // grass tiles with a single flower
const FLOWER_CHANCE = 0.12; // fraction of walkable tiles that get a flower
const DIRT_INDEX = 462; // seamless center of the brown dirt 3×3 autotile (paths)

// Blocked-tile props (transparent overlays that sit on the grass below):
// brown trees so obstacles read clearly against the green grass.
//   527 brown round tree · 530 brown pine · 540 bare/dead tree · 533 brown bush
const PROP_INDICES = [527, 530, 540, 533];

// --- Kenney roguelike CHARACTER sheet (apps/web/public/sprites/) ---
// 54 cols × 12 rows of 16px tiles, margin 0 / spacing 1. index = row*54 + col.
// These torso frames read as little head+body characters. Tune if needed.
const CHAR_SHEET = "/sprites/roguelikeChar_transparent.png";
const PLAYER_FRAME = 10; // teal/cyan villager (matches --party)
const ENEMY_FRAME = 7; // orange villager (warm, contrasts the player)

/**
 * Deterministic per-tile pseudo-random in [0,1) — seeded by (x,y) so terrain
 * variety is STABLE across rebuilds (no flicker if the map is re-drawn). `salt`
 * decorrelates independent choices (grass vs flower vs prop) on the same tile.
 */
function tileRand(x: number, y: number, salt = 0): number {
  let h = (x * 374761393 + y * 668265263 + salt * 2246822519) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0;
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

function pick<T>(arr: T[], r: number): T {
  return arr[Math.min(arr.length - 1, Math.floor(r * arr.length))];
}

/** Small seeded PRNG (mulberry32) for stable, map-wide procedural choices. */
function mulberry32(seed: number): () => number {
  return function () {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Carve a cosmetic dirt-path network as a set of "x,y" keys. A horizontal
 * drunkard's-walk across the map plus a vertical one for a crossroads feel.
 * Seeded by map size so it's deterministic (no flicker) and purely visual —
 * the API's blocked grid is untouched, so path tiles stay walkable.
 */
function buildPathSet(width: number, height: number): Set<string> {
  const path = new Set<string>();
  const rng = mulberry32((width * 73856093) ^ (height * 19349663) ^ 0xc0ffee);

  // Horizontal route, left edge to right edge.
  let y = 1 + Math.floor(rng() * (height - 2));
  for (let x = 0; x < width; x++) {
    path.add(`${x},${y}`);
    if (rng() < 0.3) path.add(`${x},${Math.min(height - 1, y + 1)}`); // 2-wide
    const r = rng();
    if (r < 0.33) y = Math.max(1, y - 1);
    else if (r > 0.66) y = Math.min(height - 2, y + 1);
  }

  // Vertical route, top edge to bottom edge, for a crossroads.
  let x = 1 + Math.floor(rng() * (width - 2));
  for (let yy = 0; yy < height; yy++) {
    path.add(`${x},${yy}`);
    if (rng() < 0.3) path.add(`${Math.min(width - 1, x + 1)},${yy}`);
    const r = rng();
    if (r < 0.33) x = Math.max(1, x - 1);
    else if (r > 0.66) x = Math.min(width - 2, x + 1);
  }

  return path;
}

export interface OverworldConfig {
  runId: string;
  onEncounter: (wildId: string) => void;
  /** Fired once the map loads — lets the React HUD draw the minimap. */
  onMapLoaded?: (m: {
    width: number;
    height: number;
    tiles: number[][];
    enemies: { id: string; x: number; y: number }[];
  }) => void;
  /** Fired on initial placement and every move — drives the minimap dot. */
  onPlayerMove?: (x: number, y: number) => void;
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
  private tilemap: Phaser.Tilemaps.Tilemap | null = null;
  private shadowGfx: Phaser.GameObjects.Graphics | null = null;
  private terrainBuilt = false;
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
    // Kenney roguelike tileset for ground + decoration. Sprites are still
    // baked procedurally in bakeSprites(); only the terrain uses real art.
    this.load.spritesheet("rogue", SHEET, {
      frameWidth: SHEET_TILE,
      frameHeight: SHEET_TILE,
      margin: 0,
      spacing: 1,
    });
    // Kenney character sheet for player/enemy sprites (falls back to baked
    // blobs if this 404s — see create()/drawEnemies guards on "chars").
    this.load.spritesheet("chars", CHAR_SHEET, {
      frameWidth: SHEET_TILE,
      frameHeight: SHEET_TILE,
      margin: 0,
      spacing: 1,
    });
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

    // Player sprite — Kenney character if loaded, else the baked cyan blob.
    this.playerSprite = this.textures.exists("chars")
      ? this.add.sprite(0, 0, "chars", PLAYER_FRAME)
      : this.add.sprite(0, 0, "player");
    this.playerSprite.setDisplaySize(TILE_SIZE, TILE_SIZE);
    this.playerSprite.setDepth(10);

    // Input
    this.cursors = this.input.keyboard!.createCursorKeys();
    this.wasd = {
      up: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.W),
      down: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.S),
      left: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.A),
      right: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.D),
    };

    // Camera zoom keeps ~14 tiles visible vertically; recompute on canvas resize
    // so the world stays a comfortable size at any window dimension.
    this.applyZoom();
    this.scale.on("resize", this.applyZoom, this);

    // Load initial map state
    await this.fetchMapAndDraw();
  }

  /**
   * Zoom so the map COVERS the viewport (no dark margins) — fills the shorter
   * axis and scrolls the longer one. Falls back to default 20×15 until the map
   * loads. Clamped so tiles never get absurdly large/small.
   */
  private applyZoom() {
    const view = this.scale.gameSize;
    const vw = view.width || this.cameras.main.width;
    const vh = view.height || this.cameras.main.height;
    const worldW = (this.mapData?.width ?? 20) * TILE_SIZE;
    const worldH = (this.mapData?.height ?? 15) * TILE_SIZE;
    const z = Phaser.Math.Clamp(Math.max(vw / worldW, vh / worldH), 1, 4);
    this.cameras.main.setZoom(z);
  }

  private async fetchMapAndDraw() {
    try {
      const res = await fetch(`/api/runs/${this.cfg.runId}/map`);
      if (!res.ok) return;
      const data = (await res.json()) as MapState;
      // The scene may have been destroyed (React StrictMode double-mount, HMR,
      // navigation away) while the fetch was in flight; bail before touching
      // `this.add`, which would null-deref through the dead game object factory.
      if (!this.sys?.isActive()) return;
      this.mapData = data;
      this.px = this.mapData.player_x;
      this.py = this.mapData.player_y;
      // Build the world ONCE; subsequent refreshes only move sprites. This is
      // what lets per-tile variety be stable (no flicker) and keeps /move cheap.
      this.buildTerrain();
      this.drawEnemies(this.mapData.enemies);
      this.positionPlayer();
      // Now that map dimensions are known, fit the zoom to cover the viewport.
      this.applyZoom();
      // Hand the grid + enemy positions to the React HUD for the minimap.
      this.cfg.onMapLoaded?.({
        width: this.mapData.width,
        height: this.mapData.height,
        tiles: this.mapData.tiles,
        enemies: this.mapData.enemies.map((e) => ({ id: e.id, x: e.x, y: e.y })),
      });
    } catch (e) {
      console.error("Failed to fetch map:", e);
    }
  }

  /**
   * Paint the whole world a single time with the Kenney roguelike tileset:
   * - "ground" layer (depth 0): seeded grass + occasional flower-grass.
   * - shadow graphics (depth 1): a soft ellipse under each tall prop ("grounds"
   *   it so trees don't look like they float).
   * - "decor" layer (depth 2): a seeded tree/bush on every blocked tile.
   * 16px tiles are scaled to TILE_SIZE (32) so they align with the scene's
   * existing player/enemy/camera coordinate system. Falls back to procedural
   * rectangles if the PNG failed to load.
   */
  private buildTerrain() {
    if (!this.mapData || this.terrainBuilt) return;

    if (!this.textures.exists("rogue")) {
      this.drawProceduralMap();
      this.terrainBuilt = true;
      return;
    }

    const { width, height, tiles } = this.mapData;

    const map = this.make.tilemap({
      tileWidth: SHEET_TILE,
      tileHeight: SHEET_TILE,
      width,
      height,
    });
    this.tilemap = map;

    const tileset = map.addTilesetImage(
      "rogue",
      "rogue",
      SHEET_TILE,
      SHEET_TILE,
      0,
      1
    )!;
    const scale = TILE_SIZE / SHEET_TILE; // 16 -> 32

    const ground = map.createBlankLayer("ground", tileset)!;
    ground.setScale(scale).setDepth(0);

    const decor = map.createBlankLayer("decor", tileset)!;
    decor.setScale(scale).setDepth(2);

    const shadows = this.add.graphics();
    shadows.setDepth(1);
    this.shadowGfx = shadows;

    // Cosmetic dirt-path network (walkable; purely a ground-layer paint job).
    const pathSet = buildPathSet(width, height);

    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const blocked = tiles[y][x] === 1;
        const onPath = pathSet.has(`${x},${y}`);

        // Ground is ALWAYS opaque — grass normally, dirt where a path runs.
        // Both flowers and trees are transparent overlays that sit on top.
        const groundIdx =
          onPath && !blocked ? DIRT_INDEX : pick(GRASS_INDICES, tileRand(x, y, 3));
        ground.putTileAt(groundIdx, x, y);

        if (blocked) {
          // Grounding shadow at the prop's base.
          const cx = x * TILE_SIZE + TILE_SIZE / 2;
          const cy = y * TILE_SIZE + TILE_SIZE * 0.82;
          shadows.fillStyle(0x000000, 0.22);
          shadows.fillEllipse(cx, cy, TILE_SIZE * 0.72, TILE_SIZE * 0.3);
          // Seeded brown tree — obstacles stand out against the grass.
          decor.putTileAt(pick(PROP_INDICES, tileRand(x, y, 5)), x, y);
        } else if (!onPath && tileRand(x, y, 7) < FLOWER_CHANCE) {
          // Seeded flower scatter on the overlay, above the grass (not on paths).
          decor.putTileAt(pick(FLOWER_INDICES, tileRand(x, y, 11)), x, y);
        }
      }
    }

    this.terrainBuilt = true;
  }

  /** Fallback terrain (no art): desaturated grass / darker walls. */
  private drawProceduralMap() {
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
        // Kenney character if loaded, else the baked rose blob.
        const spr = this.textures.exists("chars")
          ? this.add.sprite(ex, ey, "chars", ENEMY_FRAME)
          : this.add.sprite(ex, ey, "enemy");
        spr.setDisplaySize(TILE_SIZE, TILE_SIZE);
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
    this.cfg.onPlayerMove?.(this.px, this.py);

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
    this.scale.off("resize", this.applyZoom, this);
    this.enemySprites.forEach((spr) => spr.destroy());
    this.enemySprites.clear();
    if (this.shadowGfx) {
      this.shadowGfx.destroy();
      this.shadowGfx = null;
    }
    if (this.tilemap) {
      this.tilemap.destroy();
      this.tilemap = null;
    }
    this.terrainBuilt = false;
  }
}
