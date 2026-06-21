/**
 * OverworldScene — Pokémon-style tile overworld in Phaser 3.
 *
 * Track B, Wave 1: CLIENT-AUTHORITATIVE MOVEMENT.
 * --------------------------------------------------------------------------
 * Movement is now simulated client-side every frame (WorldSim) instead of one
 * `POST /api/runs/{id}/move` per step. Collision is checked locally against the
 * tile array received from /map. Wild
 * enemies roam via a per-frame FSM (EnemyAI). The server is the seed +
 * persistence authority only: position is pushed back DEBOUNCED (~1.5s) and on
 * scene shutdown via `POST /api/runs/{id}/sync`. Encounters trigger client-side
 * when a roaming enemy touches the player → cfg.onEncounter(wildId).
 *
 * Public contract (unchanged, consumed by ui/Overworld.tsx):
 *   - init({ runId, onEncounter })
 *   - refreshMap()  — reload after returning from an encounter.
 */

import Phaser from "phaser";
import { EnemyManager, type Enemy, type EnemySpawn } from "./EnemyAI";
import { NPCBehaviorManager, type NPCAnchorView } from "./NPCBehavior";
import {
  createPlayerTextures,
  PlayerSpriteAnimator,
  playerTextureKey,
} from "./PlayerAnimator";
import { PostFX } from "./PostFX";
import { SceneRouter, type RegionSpec, type RoutablePOI } from "./SceneRouter";
import { WorldSim, type MoveIntent } from "./WorldSim";
import { TILE_SIZE } from "./constants";
export { TILE_SIZE } from "./constants";

/** How often (ms) to persist the player's absolute position to the server. */
const SYNC_DEBOUNCE_MS = 1500;
const CHUNK_FETCH_RADIUS_TILES = 48;
const CHUNK_EDGE_MARGIN_TILES = 18;
const CHUNK_FETCH_THROTTLE_MS = 450;

const TILE = {
  GRASS: 0,
  BLOCKED: 1,
  CAMP: 2,
  ROAD: 3,
  FEATURE: 4,
  FOREST: 5,
  WATER: 6,
  MOUNTAIN: 7,
  TOWN: 8,
  CAVE: 9,
} as const;

const BLOCKED_TILES = new Set<number>([
  TILE.BLOCKED,
  TILE.WATER,
  TILE.MOUNTAIN,
]);

const SHEET = "/tiles/roguelikeSheet_transparent.png";
const CHAR_SHEET = "/sprites/roguelikeChar_transparent.png";
const SHEET_TILE = 16;
const ENEMY_FRAME = 7;

/**
 * Deterministic per-tile 32-bit hash. Used to drive sub-tile color jitter so
 * the overworld stops looking like a flat grid without any noise libraries.
 */
function tileJitter(x: number, y: number): number {
  let h = (x * 374761393 + y * 668265263) | 0;
  h = (h ^ (h >>> 13)) * 1274126177;
  return h | 0;
}

/**
 * Nudge an 0xRRGGBB color per-channel by a deterministic amount derived from
 * ``jitter``, clamped to [lo, hi]. Pure; same args -> same color.
 */
function clampRgb(base: number, jitter: number, lo: number, hi: number): number {
  const range = hi - lo;
  const dr = lo + (((jitter >>> 0) & 0xff) * range) / 0xff;
  const dg = lo + (((jitter >>> 8) & 0xff) * range) / 0xff;
  const db = lo + (((jitter >>> 16) & 0xff) * range) / 0xff;
  const r = Math.max(0, Math.min(255, ((base >> 16) & 0xff) + Math.round(dr)));
  const g = Math.max(0, Math.min(255, ((base >> 8) & 0xff) + Math.round(dg)));
  const b = Math.max(0, Math.min(255, (base & 0xff) + Math.round(db)));
  return (r << 16) | (g << 8) | b;
}

function terrainFill(tile: number, jitter: number): number {
  switch (tile) {
    case TILE.BLOCKED:
      return clampRgb(0x2a3326, jitter, -6, 6);
    case TILE.CAMP:
      return clampRgb(0x4b432c, jitter, -8, 8);
    case TILE.ROAD:
      return clampRgb(0x6a5132, jitter, -8, 8);
    case TILE.FEATURE:
      return clampRgb(0x465a50, jitter, -8, 8);
    case TILE.FOREST:
      return clampRgb(0x213b26, jitter, -8, 8);
    case TILE.WATER:
      return clampRgb(0x1d4f70, jitter, -6, 10);
    case TILE.MOUNTAIN:
      return clampRgb(0x5b6060, jitter, -10, 10);
    case TILE.TOWN:
      return clampRgb(0x7a6846, jitter, -8, 8);
    case TILE.CAVE:
      return clampRgb(0x372f32, jitter, -7, 7);
    default:
      return clampRgb(0x33402a, jitter, -10, 10);
  }
}

export interface OverworldConfig {
  runId: string;
  onEncounter: (wildId: string) => void;
  onNpcTalk?: (npc: NPCAnchorView) => void;
  onMapLoaded?: (m: {
    width: number;
    height: number;
    tiles: number[][];
    enemies: { id: string; x: number; y: number }[];
  }) => void;
  onPlayerMove?: (x: number, y: number) => void;
  returnTile?: { x: number; y: number };
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
  origin_x?: number;
  origin_y?: number;
  world_width?: number | null;
  world_height?: number | null;
  chunk_size?: number | null;
  pois?: RoutablePOI[];
}

interface WorldState {
  seed: number;
  width: number;
  height: number;
  regions: RegionSpec[];
  pois: RoutablePOI[];
  start?: RoutablePOI | null;
  goal?: RoutablePOI | null;
}

export class OverworldScene extends Phaser.Scene {
  private cfg!: OverworldConfig;

  // Map data (loaded once per scene start)
  private mapData: MapState | null = null;
  private worldData: WorldState | null = null;

  // Graphics objects
  private tileGraphics!: Phaser.GameObjects.Graphics;
  private playerSprite!: Phaser.GameObjects.Sprite;
  private playerAnimator!: PlayerSpriteAnimator;

  // Client-side world simulation + roaming enemy runtime
  private sim: WorldSim | null = null;
  private enemies = new EnemyManager();
  private npcs = new NPCBehaviorManager();
  private sceneRouter: SceneRouter | null = null;

  // Atmosphere overlays (vignette + night tint). Camera-scrolling, toggleable.
  private postFX = new PostFX();

  // Input
  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys;
  private wasd!: {
    up: Phaser.Input.Keyboard.Key;
    down: Phaser.Input.Keyboard.Key;
    left: Phaser.Input.Keyboard.Key;
    right: Phaser.Input.Keyboard.Key;
  };
  private runKey!: Phaser.Input.Keyboard.Key;

  // Position-sync debounce bookkeeping
  private syncAccumMs = 0;
  private lastSyncedTile = { x: -1, y: -1 };
  // Monotonic sequence number attached to every /sync. The server keeps the
  // high-water mark and drops any sync with seq <= last seen, so an out-of-order
  // / stale request (refresh + reconnect race, retried debounce) can't roll a
  // newer position back to an older one.
  private syncSeq = 0;
  // Latched so we stop driving the sim/encounters after a battle starts.
  private encounterFired = false;
  // Idempotency lock: once a collision fires POST /api/encounters we must NOT
  // fire again until it resolves — roaming enemies re-collide every frame, and
  // two enemies can overlap the player in the same frame. Held from the first
  // trigger until the scene transitions / restarts.
  private encounterPending = false;
  private lastTalkNpcId: string | null = null;
  private lastTalkAtMs = 0;
  private chunkFetchPending = false;
  private lastChunkFetchAtMs = 0;
  private lastHudTile = { x: -1, y: -1 };

  constructor() {
    super({ key: "OverworldScene" });
  }

  init(data: OverworldConfig) {
    this.cfg = data;
    // Fresh transition state on (re)start — refreshMap restarts via create().
    this.encounterFired = false;
    this.encounterPending = false;
    this.lastHudTile = { x: -1, y: -1 };
  }

  preload() {
    this.load.spritesheet("rogue", SHEET, {
      frameWidth: SHEET_TILE,
      frameHeight: SHEET_TILE,
      margin: 0,
      spacing: 1,
    });
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

    // --- NPC: amber-robed villager/mentor marker ---
    if (!this.textures.exists("npc")) {
      const g = this.make.graphics({ x: 0, y: 0 }, false);
      px(g, 0xffcf3f, 5, 3, 6, 4);
      px(g, 0xc17f2a, 4, 7, 8, 7);
      px(g, 0x0e1018, 6, 5);
      px(g, 0x0e1018, 9, 5);
      px(g, 0xe8e6d8, 5, 12, 6, 1);
      g.generateTexture("npc", SIZE, SIZE);
      g.destroy();
    }
  }

  async create() {
    this.tileGraphics = this.add.graphics();
    this.cameras.main.setBackgroundColor("#1a1a2e");

    // Bake the procedural pixel-art sprites once (no external assets).
    this.bakeSprites();
    createPlayerTextures(this);

    this.playerSprite = this.textures.exists(playerTextureKey("down", 1))
      ? this.add.sprite(0, 0, playerTextureKey("down", 1))
      : this.add.sprite(0, 0, "player");
    this.playerSprite.setDisplaySize(TILE_SIZE, TILE_SIZE);
    this.playerSprite.setDepth(10);
    this.playerAnimator = new PlayerSpriteAnimator(this.playerSprite);

    // Input
    this.cursors = this.input.keyboard!.createCursorKeys();
    this.wasd = {
      up: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.W),
      down: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.S),
      left: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.A),
      right: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.D),
    };
    this.runKey = this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.SHIFT);

    // Flush the latest position to the server when the scene tears down
    // (navigation away / encounter transition) — the on-transition sync.
    this.events.once(Phaser.Scenes.Events.SHUTDOWN, () => this.flushSync());
    this.events.once(Phaser.Scenes.Events.DESTROY, () => this.flushSync());

    // Atmosphere overlays (drawn ON TOP of everything via depth=999/1000).
    this.postFX.attach(this);
    // Backtick toggles PostFX off for verification (compare with/without).
    this.input.keyboard?.on("keydown-BACKTICK", () => this.postFX.toggle());

    // Load initial map state
    await this.fetchMapAndDraw();
  }

  private async fetchMapAndDraw(centerTile?: { x: number; y: number }) {
    // Phaser auto-starts this scene from `scene: [OverworldScene]` (in
    // Overworld.tsx) before the React wrapper restarts it with run config, so the
    // first create() runs with empty init data and no runId. Bail until the
    // wrapper's game.scene.start re-runs create() with cfg populated — otherwise
    // we fire GET /api/runs/undefined/map → 404.
    if (!this.cfg?.runId) return;
    try {
      const params = new URLSearchParams();
      params.set("chunk_size", String(CHUNK_FETCH_RADIUS_TILES * 2));
      if (centerTile) {
        params.set("center_x", String(centerTile.x));
        params.set("center_y", String(centerTile.y));
      }
      const [mapRes, worldRes] = await Promise.all([
        fetch(`/api/runs/${this.cfg.runId}/map?${params.toString()}`),
        this.worldData
          ? Promise.resolve(null)
          : fetch(`/api/runs/${this.cfg.runId}/world`),
      ]);
      if (!mapRes.ok) return;
      const data = (await mapRes.json()) as MapState;
      const world =
        this.worldData ??
        (worldRes?.ok ? ((await worldRes.json()) as WorldState) : null);
      // The scene may have been destroyed (React StrictMode double-mount, HMR,
      // navigation away) while the fetch was in flight; bail before touching
      // `this.add`, which would null-deref through the dead game object factory.
      if (!this.sys?.isActive()) return;
      this.mapData = data;
      this.worldData = world;
      this.sceneRouter = new SceneRouter({
        runId: this.cfg.runId,
        scenePlugin: this.scene,
      });

      const originX = data.origin_x ?? 0;
      const originY = data.origin_y ?? 0;
      if (this.sim) {
        this.sim.setTiles(data.tiles, originX, originY);
        this.drawMap();
        this.spawnEnemies(data.enemies);
        this.spawnNpcs();
        this.emitHudMap();
        this.emitPlayerTile();
        return;
      }

      // Spin up the client-side simulation seeded at the persisted player tile.
      const start = this.cfg.returnTile ?? { x: data.player_x, y: data.player_y };
      this.sim = new WorldSim({
        tiles: data.tiles,
        width: data.width,
        height: data.height,
        worldWidth: data.world_width ?? data.width,
        worldHeight: data.world_height ?? data.height,
        tileOriginX: originX,
        tileOriginY: originY,
        startTileX: start.x,
        startTileY: start.y,
      });
      this.lastSyncedTile = { x: start.x, y: start.y };

      this.drawMap();
      this.spawnEnemies(data.enemies);
      this.spawnNpcs();

      // Position player + attach the follow camera now that the sim exists.
      this.sim.applyToSprite(this.playerSprite);
      this.sim.attachCamera(this.cameras.main, this.playerSprite);
      this.emitHudMap();
      this.emitPlayerTile();
    } catch (e) {
      console.error("Failed to fetch map:", e);
    }
  }

  private emitHudMap() {
    if (!this.mapData) return;
    const originX = this.mapData.origin_x ?? 0;
    const originY = this.mapData.origin_y ?? 0;
    this.cfg.onMapLoaded?.({
      width: this.mapData.width,
      height: this.mapData.height,
      tiles: this.mapData.tiles,
      enemies: this.mapData.enemies.map((e) => ({
        id: e.id,
        x: e.x - originX,
        y: e.y - originY,
      })),
    });
  }

  private emitPlayerTile() {
    if (!this.sim) return;
    const x = this.sim.tileX - (this.mapData?.origin_x ?? 0);
    const y = this.sim.tileY - (this.mapData?.origin_y ?? 0);
    if (x === this.lastHudTile.x && y === this.lastHudTile.y) return;
    this.lastHudTile = { x, y };
    this.cfg.onPlayerMove?.(x, y);
  }

  private drawMap() {
    if (!this.mapData) return;
    const g = this.tileGraphics;
    g.clear();

    for (let y = 0; y < this.mapData.height; y++) {
      for (let x = 0; x < this.mapData.width; x++) {
        const tile = this.mapData.tiles[y][x];
        const blocked = BLOCKED_TILES.has(tile);
        const camp = tile === TILE.CAMP;
        const px = ((this.mapData.origin_x ?? 0) + x) * TILE_SIZE;
        const py = ((this.mapData.origin_y ?? 0) + y) * TILE_SIZE;

        // Per-tile color variation breaks the "flat tilemap" read. Hash of
        // (x,y) maps to a -8..+8 RGB jitter; same map = same look every boot.
        const jitter = tileJitter(x, y);
        const fill = terrainFill(tile, jitter);
        g.fillStyle(fill, 1);
        g.fillRect(px, py, TILE_SIZE, TILE_SIZE);

        // Sub-tile foliage speckle so grass/forest read as continuous terrain.
        if (tile === TILE.GRASS || tile === TILE.FOREST) {
          const speckleAlpha = 0.18 + (jitter & 0x3f) / 600;
          g.fillStyle(tile === TILE.FOREST ? 0x3f6c36 : 0x4a5a36, speckleAlpha);
          const sx = px + ((jitter >> 2) & 0xf);
          const sy = py + ((jitter >> 6) & 0xf);
          g.fillRect(sx, sy, 2, 2);
          g.fillRect(sx + 6, sy + 5, 2, 2);
        }

        if (tile === TILE.ROAD) {
          g.fillStyle(0x8c6a42, 0.35);
          g.fillRect(px + 4, py + 13, TILE_SIZE - 8, 3);
        }

        if (tile === TILE.WATER) {
          g.fillStyle(0x6fb7d7, 0.22);
          g.fillRect(px + 4, py + 9 + (jitter & 0x3), TILE_SIZE - 8, 2);
          g.fillRect(px + 10, py + 20 - (jitter & 0x5), TILE_SIZE - 16, 2);
        }

        if (tile === TILE.MOUNTAIN) {
          g.fillStyle(0xd3d0bd, 0.22);
          g.fillTriangle(
            px + TILE_SIZE / 2,
            py + 6,
            px + 8,
            py + TILE_SIZE - 7,
            px + TILE_SIZE - 8,
            py + TILE_SIZE - 7
          );
        }

        if (tile === TILE.TOWN) {
          g.fillStyle(0xd4b46e, 0.25);
          g.fillRect(px + 7, py + 7, TILE_SIZE - 14, TILE_SIZE - 14);
        }

        if (tile === TILE.CAVE) {
          g.fillStyle(0x0e1018, 0.65);
          g.fillRect(px + 8, py + 9, TILE_SIZE - 16, TILE_SIZE - 13);
        }

        // Darker wall texture for hard blocked tiles.
        if (blocked && tile !== TILE.WATER && tile !== TILE.MOUNTAIN) {
          g.fillStyle(0x1b2118, 0.85);
          g.fillRect(px + 6, py + 6, TILE_SIZE - 12, TILE_SIZE - 12);
        }

        // Campsite overlay (walkable; gold hearth marker)
        if (camp) {
          g.fillStyle(0xffcf3f, 0.55);
          g.fillRect(px + 10, py + 10, TILE_SIZE - 20, TILE_SIZE - 20);
        }
      }
    }

    this.drawPois();
  }

  private worldPois(): RoutablePOI[] {
    return this.worldData?.pois?.length ? this.worldData.pois : (this.mapData?.pois ?? []);
  }

  private drawPois() {
    const g = this.tileGraphics;
    for (const poi of this.worldPois()) {
      if (poi.kind === "start") continue;
      const px = poi.x * TILE_SIZE;
      const py = poi.y * TILE_SIZE;
      const color =
        poi.kind === "town"
          ? 0xffcf3f
          : poi.kind === "den"
            ? 0xff5d6c
            : poi.kind === "goal"
              ? 0x6ee787
              : 0x5cc8ff;
      g.lineStyle(2, color, 0.9);
      g.strokeRect(px + 5, py + 5, TILE_SIZE - 10, TILE_SIZE - 10);
      g.fillStyle(color, 0.18);
      g.fillRect(px + 8, py + 8, TILE_SIZE - 16, TILE_SIZE - 16);
    }
  }

  /**
   * Build live roaming enemies from the seeded /map spawns. Each gets a pulsing
   * sprite; positions are now driven every frame by the EnemyAI FSM, not static.
   */
  private spawnEnemies(enemies: TileEnemy[]) {
    this.enemies.destroy();
    const spawns: EnemySpawn[] = enemies.map((e) => ({
      id: e.id,
      tileX: e.x,
      tileY: e.y,
    }));

    this.enemies.spawn(spawns, (enemy: Enemy) => {
      const spr = this.textures.exists("chars")
        ? this.add.sprite(enemy.x, enemy.y, "chars", ENEMY_FRAME)
        : this.add.sprite(enemy.x, enemy.y, "enemy");
      spr.setDisplaySize(TILE_SIZE, TILE_SIZE);
      spr.setDepth(5);

      // Pulsing animation (relative to the baked display scale).
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
      return spr;
    });
  }

  private spawnNpcs() {
    this.npcs.destroy();
    const anchors = this.worldPois().flatMap((poi) => poi.npc_anchors ?? []);
    for (const anchor of anchors) {
      const spr = this.add.sprite(
        anchor.x * TILE_SIZE + TILE_SIZE / 2,
        anchor.y * TILE_SIZE + TILE_SIZE / 2,
        "npc"
      );
      spr.setDisplaySize(TILE_SIZE - 8, TILE_SIZE - 8);
      spr.setDepth(7);
      this.npcs.add(anchor, spr);
    }
  }

  /** Read keyboard intent into a normalised {dx,dy} for WorldSim. */
  private readIntent(): MoveIntent {
    let dx = 0;
    let dy = 0;
    if (this.cursors.left.isDown || this.wasd.left.isDown) dx -= 1;
    if (this.cursors.right.isDown || this.wasd.right.isDown) dx += 1;
    if (this.cursors.up.isDown || this.wasd.up.isDown) dy -= 1;
    if (this.cursors.down.isDown || this.wasd.down.isDown) dy += 1;
    return { dx, dy, running: this.runKey.isDown };
  }

  update(time: number, delta: number) {
    if (!this.sim || !this.mapData || this.encounterFired || this.encounterPending)
      return;

    // 1) Integrate smooth player movement + local collision.
    const intent = this.readIntent();
    this.sim.update(intent, delta);
    this.sim.applyToSprite(this.playerSprite);
    this.playerAnimator.update({
      intent,
      velocity: { vx: this.sim.vx, vy: this.sim.vy },
      deltaMs: delta,
    });
    this.emitPlayerTile();
    this.npcs.update(time, this.sim.x, this.sim.y);
    this.maybeTriggerNpcTalk(time);
    this.maybeEnterInterior();
    this.maybeRefreshChunk(time);

    // 2) Tick roaming enemy AI; collide → trigger encounter (client-side).
    const hitId = this.enemies.update(
      delta,
      this.sim.x,
      this.sim.y,
      (tx, ty) => this.sim!.isBlockedTile(tx, ty)
    );
    if (hitId) {
      this.triggerEncounter(hitId);
      return;
    }

    // 3) Debounced position persistence (server is persistence authority only).
    this.maybeSyncPosition(delta);
  }

  private maybeTriggerNpcTalk(time: number) {
    if (!this.sim || !this.cfg.onNpcTalk) return;
    const anchor = this.npcs.nearest(this.sim.x, this.sim.y);
    if (!anchor) {
      this.lastTalkNpcId = null;
      return;
    }
    if (this.lastTalkNpcId === anchor.npc_id && time - this.lastTalkAtMs < 1500) return;
    this.lastTalkNpcId = anchor.npc_id;
    this.lastTalkAtMs = time;
    this.cfg.onNpcTalk(anchor);
  }

  private maybeEnterInterior() {
    if (!this.sim || !this.sceneRouter) return;
    const tx = this.sim.tileX;
    const ty = this.sim.tileY;
    const poi = this.worldPois().find((p) => p.x === tx && p.y === ty);
    if (!poi) return;
    void this.sceneRouter.enter(poi);
  }

  private maybeRefreshChunk(time: number) {
    if (!this.sim || !this.mapData || this.chunkFetchPending) return;
    if (time - this.lastChunkFetchAtMs < CHUNK_FETCH_THROTTLE_MS) return;

    const originX = this.mapData.origin_x ?? 0;
    const originY = this.mapData.origin_y ?? 0;
    const localX = this.sim.tileX - originX;
    const localY = this.sim.tileY - originY;
    const nearEdge =
      localX < CHUNK_EDGE_MARGIN_TILES ||
      localY < CHUNK_EDGE_MARGIN_TILES ||
      localX >= this.mapData.width - CHUNK_EDGE_MARGIN_TILES ||
      localY >= this.mapData.height - CHUNK_EDGE_MARGIN_TILES;
    if (!nearEdge) return;

    this.chunkFetchPending = true;
    this.lastChunkFetchAtMs = time;
    void this.fetchMapAndDraw({ x: this.sim.tileX, y: this.sim.tileY }).finally(
      () => {
        this.chunkFetchPending = false;
      }
    );
  }

  /** Remove the collided enemy and hand off to the React encounter flow. */
  private triggerEncounter(wildId: string) {
    // Idempotency: drop any re-collision while an encounter is already pending.
    // (update() also early-returns on encounterFired, but guard here too so a
    // same-frame double-collision / any direct caller can't double-POST.)
    if (this.encounterPending) return;
    this.encounterPending = true;
    this.encounterFired = true;
    this.enemies.remove(wildId);
    if (this.mapData) {
      this.mapData.enemies = this.mapData.enemies.filter((e) => e.id !== wildId);
      this.emitHudMap();
    }
    // Persist position before the scene flips to the battle screen.
    this.flushSync();
    this.cfg.onEncounter(wildId);
  }

  /**
   * Accumulate frame time; once past the debounce window AND the player has
   * actually changed tiles, push the absolute position to the server.
   */
  private maybeSyncPosition(deltaMs: number) {
    if (!this.sim) return;
    this.syncAccumMs += deltaMs;
    if (this.syncAccumMs < SYNC_DEBOUNCE_MS) return;
    this.syncAccumMs = 0;

    const tx = this.sim.tileX;
    const ty = this.sim.tileY;
    if (tx === this.lastSyncedTile.x && ty === this.lastSyncedTile.y) return;
    this.lastSyncedTile = { x: tx, y: ty };
    void this.postSync(tx, ty);
  }

  /** Immediate position flush (scene transition / encounter / shutdown). */
  private flushSync() {
    if (!this.sim || !this.cfg?.runId) return;
    const tx = this.sim.tileX;
    const ty = this.sim.tileY;
    if (tx === this.lastSyncedTile.x && ty === this.lastSyncedTile.y) return;
    this.lastSyncedTile = { x: tx, y: ty };
    void this.postSync(tx, ty);
  }

  private async postSync(x: number, y: number) {
    // Strictly-increasing seq so the server can drop stale/out-of-order syncs.
    const seq = ++this.syncSeq;
    try {
      await fetch(`/api/runs/${this.cfg.runId}/sync`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ x, y, seq }),
      });
    } catch (e) {
      // Persistence is best-effort; movement is client-authoritative.
      console.error("Position sync error:", e);
    }
  }

  /** Called externally to reload the map (e.g., after returning from encounter). */
  async refreshMap() {
    // Restart the scene so create() rebuilds the sim/enemies cleanly with the
    // server's latest persisted position.
    this.scene.restart(this.cfg);
  }

  /**
   * Reset encounter latch flags so the player can move again after returning
   * from battle. Called by Overworld.tsx when activeEncounterId goes null.
   * Without this, encounterFired stays true and update() exits early forever.
   */
  resetAfterBattle() {
    this.encounterFired = false;
    this.encounterPending = false;
  }

  destroy() {
    this.enemies.destroy();
    this.npcs.destroy();
  }
}
