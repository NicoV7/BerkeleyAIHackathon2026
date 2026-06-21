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
import {
  chunkKey,
  neighborPrefetchCentres,
  nearChunkEdge,
  shouldSwapChunk,
  windowsWithinRenderHalo,
  type ChunkWindow,
} from "./ChunkStream";
import {
  DUNGEON_INTERIOR_KEY,
  InteriorScene,
  TOWN_INTERIOR_KEY,
} from "./InteriorScene";
import { SceneRouter, type RegionSpec, type RoutablePOI } from "./SceneRouter";
import {
  SHEET_COLS,
  SHEET_ROWS,
  baseFrameFor,
  bridgeFrameFor,
  forestBorderEdges,
  overlayFor,
  shorelineEdges,
  type EdgeStamp,
  type Neighbors,
} from "./TileAtlas";
import { WorldSim, type MoveIntent } from "./WorldSim";
import { TILE_SIZE } from "./constants";
export { TILE_SIZE } from "./constants";

/** How often (ms) to persist the player's absolute position to the server. */
const SYNC_DEBOUNCE_MS = 1500;
const CHUNK_FETCH_RADIUS_TILES = 48;
const CHUNK_EDGE_MARGIN_TILES = 18;
const CHUNK_FETCH_THROTTLE_MS = 450;
const CHUNK_RENDER_HALO_TILES = 4;

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
/** Phaser texture key for the loaded Kenney roguelike terrain atlas. */
const ATLAS_KEY = "rogue";
/** Atlas frames are 16px; the world renders at TILE_SIZE → integer upscale. */
const ATLAS_SCALE = TILE_SIZE / SHEET_TILE;

const SIGN_INTERACT_PX = TILE_SIZE * 1.8;
const SIGN_POOL = [
  "Beware: Wild Debaters ahead!",
  "Village of Logos — 3 tiles east",
  "Campsite nearby. Rest your arguments.",
  "Danger: Sophists in the mountains",
  "Ancient Rhetoric Grove",
  "Trade Post — bring strong claims",
  "Warning: Ad Hominem territory",
  "The Great Debate Plains",
  "Here be Strawmen",
  "Socratic Springs — refreshing questions",
  "Pathos Peaks — emotional terrain ahead",
  "Ethos Ridge — credibility required",
  "Farmlands of Reason",
  "Mind the logical gap",
  "Fallacy Fields — tread carefully",
] as const;

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
  playerName?: string;
  onEncounter: (wildId: string) => void;
  onNpcTalk?: (npc: NPCAnchorView) => void;
  onMapLoaded?: (m: {
    width: number;
    height: number;
    tiles: number[][];
    enemies: { id: string; x: number; y: number }[];
    // Global tile origin of this chunk window, so the HUD can map GLOBAL quest
    // target coords into the minimap's chunk-local space (WS-7 render side).
    originX: number;
    originY: number;
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

  // Graphics objects — DOUBLE BUFFERED (WS-8).
  // Two terrain RenderTextures (front = visible, back = hidden) plus two matching
  // fallback Graphics layers. A new chunk is fully stamped into the BACK pair,
  // then we swap depths so it becomes the front; the old terrain stays on screen
  // the whole time the new chunk fetches + stamps, so there is never a black gap.
  // The fallback Graphics draws unmapped tile-ints + POI markers on top of the
  // atlas RT (so nothing renders blank).
  private terrainRT: [
    Phaser.GameObjects.RenderTexture | null,
    Phaser.GameObjects.RenderTexture | null,
  ] = [null, null];
  private tileGraphics!: [Phaser.GameObjects.Graphics, Phaser.GameObjects.Graphics];
  /** Index (0|1) of the buffer currently shown to the player. */
  private frontBuffer = 0;
  /** The chunk window currently displayed in the front buffer. */
  private liveWindow: ChunkWindow | null = null;
  private atlasReady = false;
  // Detail + shadow overlay layers (procedural decorations drawn each tile step).
  private detailGraphics!: Phaser.GameObjects.Graphics;
  private shadowGraphics!: Phaser.GameObjects.Graphics;
  private playerSprite!: Phaser.GameObjects.Sprite;
  private playerAnimator!: PlayerSpriteAnimator;
  private playerLabel: Phaser.GameObjects.Text | null = null;
  private lastDetailTile = { x: -1, y: -1 };

  // Sign system
  private signs: Array<{ key: string; tileX: number; tileY: number; text: string; spr: Phaser.GameObjects.Sprite }> = [];
  private signPopupBg: Phaser.GameObjects.Graphics | null = null;
  private signPopupText: Phaser.GameObjects.Text | null = null;
  private nearestSignKey = "";

  // Depth bands for the two terrain layers. Front sits just under the actors;
  // back sits below the front so the in-progress chunk is hidden until swapped.
  private static readonly RT_DEPTH = [-2, -4] as const;
  private static readonly G_DEPTH = [-1, -3] as const;

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
  // Prefetch cache: chunk windows already fetched (or in flight) keyed by origin,
  // so a swap is usually instant. Bounded to keep memory flat as the player roams.
  private prefetchedChunks = new Map<string, MapState>();
  private prefetchInFlight = new Set<string>();
  private lastPrefetchTile = { x: Number.NaN, y: Number.NaN };
  private static readonly PREFETCH_CACHE_LIMIT = 24;

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
    // Kenney roguelike terrain atlas: 16px tiles, 1px spacing, no margin →
    // a clean 57×31 grid (verified against the 968×526 PNG). The frame indices
    // in TileAtlas.ts assume exactly this geometry.
    this.load.spritesheet(ATLAS_KEY, SHEET, {
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
    // Warn (don't crash) if the atlas geometry drifts from what TileAtlas maps —
    // an upgraded sheet with different dimensions would mis-key every frame.
    this.load.once(Phaser.Loader.Events.COMPLETE, () => {
      const tex = this.textures.get(ATLAS_KEY);
      const src = tex?.getSourceImage() as { width?: number; height?: number } | undefined;
      if (!src?.width || !src?.height) return;
      const stride = SHEET_TILE + 1; // tile + 1px spacing
      const cols = Math.floor((src.width + 1) / stride);
      const rows = Math.floor((src.height + 1) / stride);
      const even = (src.width + 1) % stride === 0 && (src.height + 1) % stride === 0;
      if (!even || cols !== SHEET_COLS || rows !== SHEET_ROWS) {
        console.warn(
          `[OverworldScene] atlas geometry ${src.width}x${src.height} → ${cols}x${rows} ` +
            `(expected ${SHEET_COLS}x${SHEET_ROWS}); tile frame mapping may be off.`
        );
      }
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

    // --- Sign: wooden post with message board ---
    if (!this.textures.exists("sign")) {
      const g = this.make.graphics({ x: 0, y: 0 }, false);
      px(g, 0x5c3a1e, 7, 7, 2, 9);   // post
      px(g, 0x7a4f28, 1, 1, 14, 6);   // board back
      px(g, 0xd4a96a, 2, 2, 12, 4);   // board face
      px(g, 0x5c3a1e, 2, 4, 12, 1);   // board midline
      px(g, 0x8b5e30, 1, 7, 1, 1);    // left post bracket
      px(g, 0x8b5e30, 14, 7, 1, 1);   // right post bracket
      g.generateTexture("sign", SIZE, SIZE);
      g.destroy();
    }
  }

  async create() {
    // Terrain atlas blits below the fallback graphics; both below actors.
    // Double-buffered: a front (visible) + back (hidden) fallback Graphics layer,
    // mirroring the two terrain RenderTextures created lazily in ensureTerrainRT.
    // Terrain renders via the procedural Graphics path (colored tiles) for ALL
    // renderers. The atlas RenderTexture path (drawAtlasTile/stamp) left the
    // terrain BLANK in both Canvas (stamp is a no-op) and WebGL (Phaser 4
    // RenderTexture.stamp did not composite as expected) — verified live in the
    // browser. Graphics.fillRect is the original, known-good renderer that works
    // everywhere, so it is the default. Re-enabling the pixel-art atlas needs a
    // WebGL-correct path (Blitter/Tilemap) and is tracked as a follow-up.
    this.atlasReady = false;
    void this.textures.exists(ATLAS_KEY);
    const g0 = this.add.graphics();
    g0.setDepth(OverworldScene.G_DEPTH[0]);
    const g1 = this.add.graphics();
    g1.setDepth(OverworldScene.G_DEPTH[1]);
    this.tileGraphics = [g0, g1];
    this.frontBuffer = 0;
    this.liveWindow = null;
    this.detailGraphics = this.add.graphics();
    this.detailGraphics.setDepth(2);
    this.shadowGraphics = this.add.graphics();
    this.shadowGraphics.setDepth(9);
    this.lastDetailTile = { x: -1, y: -1 };
    this.cameras.main.setBackgroundColor("#1a1a2e");

    // Bake the procedural pixel-art sprites once (no external assets).
    this.bakeSprites();
    createPlayerTextures(this);

    // Register the interior scenes (WS-3) into THIS game's SceneManager so the
    // SceneRouter can start them by key. One InteriorScene class is added under
    // two keys (town vs dungeon palette). Idempotent across scene restarts.
    this.registerInteriorScenes();

    this.playerSprite = this.textures.exists(playerTextureKey("down", 1))
      ? this.add.sprite(0, 0, playerTextureKey("down", 1))
      : this.add.sprite(0, 0, "player");
    this.playerSprite.setDisplaySize(TILE_SIZE, TILE_SIZE);
    this.playerSprite.setDepth(10);
    this.playerAnimator = new PlayerSpriteAnimator(this.playerSprite);

    // Floating name tag above the player's head.
    this.playerLabel?.destroy();
    if (this.cfg.playerName) {
      this.playerLabel = this.add.text(0, 0, this.cfg.playerName.toUpperCase(), {
        fontFamily: "Silkscreen, monospace",
        fontSize: "10px",
        color: "#5cc8ff",
        stroke: "#0e1018",
        strokeThickness: 4,
      });
      this.playerLabel.setOrigin(0.5, 1);
      this.playerLabel.setDepth(11);
    }

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

  /**
   * Register the interior scenes into the running game's SceneManager so the
   * SceneRouter can `scene.start(key)` them. Adds the single InteriorScene class
   * under the town + dungeon keys. Idempotent: skips keys already registered (the
   * overworld scene restarts on every encounter/return, which re-runs create()).
   */
  private registerInteriorScenes() {
    const mgr = this.scene;
    for (const key of [TOWN_INTERIOR_KEY, DUNGEON_INTERIOR_KEY]) {
      if (!mgr.get(key)) {
        // Phaser accepts a Scene instance; we pass the key into the constructor.
        mgr.add(key, new InteriorScene(key), false);
      }
    }
  }

  /** Build the /map request URL for a chunk centred on an optional tile. */
  private mapUrl(centerTile?: { x: number; y: number }): string {
    const params = new URLSearchParams();
    params.set("chunk_size", String(CHUNK_FETCH_RADIUS_TILES * 2));
    if (centerTile) {
      params.set("center_x", String(centerTile.x));
      params.set("center_y", String(centerTile.y));
    }
    return `/api/runs/${this.cfg.runId}/map?${params.toString()}`;
  }

  private windowOf(data: MapState): ChunkWindow {
    return {
      originX: data.origin_x ?? 0,
      originY: data.origin_y ?? 0,
      width: data.width,
      height: data.height,
    };
  }

  private async fetchMapAndDraw(centerTile?: { x: number; y: number }) {
    // Phaser auto-starts this scene from `scene: [OverworldScene]` (in
    // Overworld.tsx) before the React wrapper restarts it with run config, so the
    // first create() runs with empty init data and no runId. Bail until the
    // wrapper's game.scene.start re-runs create() with cfg populated — otherwise
    // we fire GET /api/runs/undefined/map → 404.
    if (!this.cfg?.runId) return;
    try {
      // Use a warm prefetched chunk if the desired window is already cached so the
      // swap is instant (no in-flight gap at all). Otherwise fetch on demand.
      let data: MapState | null = centerTile
        ? this.takePrefetched(centerTile)
        : null;
      const worldNeeded = !this.worldData;
      if (!data) {
        const [mapRes, worldRes] = await Promise.all([
          fetch(this.mapUrl(centerTile)),
          worldNeeded
            ? fetch(`/api/runs/${this.cfg.runId}/world`)
            : Promise.resolve(null),
        ]);
        if (!mapRes.ok) return;
        data = (await mapRes.json()) as MapState;
        if (worldNeeded) {
          this.worldData = worldRes?.ok
            ? ((await worldRes.json()) as WorldState)
            : null;
        }
      } else if (worldNeeded) {
        const worldRes = await fetch(`/api/runs/${this.cfg.runId}/world`);
        this.worldData = worldRes.ok
          ? ((await worldRes.json()) as WorldState)
          : null;
      }
      // The scene may have been destroyed (React StrictMode double-mount, HMR,
      // navigation away) while the fetch was in flight; bail before touching
      // `this.add`, which would null-deref through the dead game object factory.
      if (!this.sys?.isActive()) return;

      this.sceneRouter ??= new SceneRouter({
        runId: this.cfg.runId,
        scenePlugin: this.scene,
        // Forward NPC dialogue from interiors to the same React handler the
        // overworld uses, and persist position before leaving for an interior.
        onNpcTalk: this.cfg.onNpcTalk,
        onEnterInterior: () => this.flushSync(),
      });

      const win = this.windowOf(data);
      const { originX, originY } = win;
      this.rememberChunk(data);

      if (this.sim) {
        // DOUBLE BUFFER: the live (front) terrain stays visible. Stamp the new
        // chunk fully into the BACK buffer, then atomically swap. `this.mapData`
        // + the sim's collision tiles are only swapped AFTER the new terrain is
        // on screen, so there is never a frame with cleared-but-not-redrawn land.
        if (!shouldSwapChunk(this.liveWindow, win)) {
          // Same window re-fetched (e.g. enemy refresh): just refresh actors.
          this.mapData = data;
          this.spawnEnemies(data.enemies);
          this.spawnNpcs();
          this.emitHudMap();
          return;
        }
        this.drawMapInto(this.backIndex(), data);
        this.swapBuffers();
        this.mapData = data;
        this.sim.setTiles(data.tiles, originX, originY);
        this.liveWindow = win;
        this.lastDetailTile = { x: -1, y: -1 };
        this.buildSigns();
        this.spawnEnemies(data.enemies);
        this.spawnNpcs();
        this.emitHudMap();
        this.emitPlayerTile();
        return;
      }

      // First load: spin up the sim + draw straight into the front buffer.
      this.mapData = data;
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

      this.drawMapInto(this.frontBuffer, data);
      this.liveWindow = win;
      this.buildSigns();
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

  /** The hidden buffer index (the one NOT currently shown). */
  private backIndex(): number {
    return this.frontBuffer ^ 1;
  }

  /**
   * Atomically swap the front/back buffers: the freshly-stamped back terrain
   * becomes visible (front depth) and the old front drops behind it. Done in a
   * single frame so the transition is seamless — the new chunk is already fully
   * drawn before this runs (see drawMapInto), so there is no black gap.
   */
  private swapBuffers() {
    const back = this.backIndex();
    this.terrainRT[back]?.setDepth(OverworldScene.RT_DEPTH[0]);
    this.terrainRT[this.frontBuffer]?.setDepth(OverworldScene.RT_DEPTH[1]);
    this.tileGraphics[back].setDepth(OverworldScene.G_DEPTH[0]);
    this.tileGraphics[this.frontBuffer].setDepth(OverworldScene.G_DEPTH[1]);
    this.frontBuffer = back;
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
      originX,
      originY,
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

  /** Tile-int at (x,y) in CHUNK-LOCAL coords of `tiles`; out-of-bounds returns the
   * same tile so autotiling treats the chunk border as "no edge" (avoids fake
   * shorelines/borders at the seam where the next chunk will load). */
  private tileAt(tiles: number[][], x: number, y: number, fallback: number): number {
    if (y < 0 || y >= tiles.length || x < 0 || x >= tiles[0].length) return fallback;
    return tiles[y][x];
  }

  /** Orthogonal neighbours of a chunk-local tile (border → self, see tileAt). */
  private neighborsOf(tiles: number[][], x: number, y: number, self: number): Neighbors {
    return {
      n: this.tileAt(tiles, x, y - 1, self),
      s: this.tileAt(tiles, x, y + 1, self),
      e: this.tileAt(tiles, x + 1, y, self),
      w: this.tileAt(tiles, x - 1, y, self),
    };
  }

  /**
   * Draw a whole chunk into the given BUFFER (front or back) from `data`. This is
   * the double-buffer primitive: it never touches `this.mapData`, so the caller
   * can stamp a new chunk into the hidden back buffer while the front stays
   * visible, then swap. Renders:
   *   - atlas frames stamped into terrainRT[buffer] (real pixel-art tiles),
   *   - edge autotiling (water shorelines, forest/grass borders) + bridges,
   *   - procedural color jitter ONLY for tile-ints with no atlas frame,
   *   - POI markers on the matching fallback Graphics layer.
   */
  private drawMapInto(buffer: number, data: MapState) {
    const g = this.tileGraphics[buffer];
    g.clear();

    const originX = data.origin_x ?? 0;
    const originY = data.origin_y ?? 0;
    const widthPx = data.width * TILE_SIZE;
    const heightPx = data.height * TILE_SIZE;

    const rt = this.atlasReady
      ? this.ensureTerrainRT(buffer, widthPx, heightPx)
      : null;
    if (rt) {
      rt.setPosition(originX * TILE_SIZE, originY * TILE_SIZE);
      rt.clear();
    }

    this.drawTerrainWindow(g, data, rt);
    for (const cached of this.renderableCachedChunks(data)) {
      this.drawTerrainWindow(g, cached, null);
    }

    this.drawPois(g);
  }

  private renderableCachedChunks(anchor: MapState): MapState[] {
    const anchorWin = this.windowOf(anchor);
    const anchorKey = chunkKey(anchorWin.originX, anchorWin.originY);
    return [...this.prefetchedChunks.values()].filter((cached) => {
      const win = this.windowOf(cached);
      return (
        chunkKey(win.originX, win.originY) !== anchorKey &&
        windowsWithinRenderHalo(anchorWin, win, CHUNK_RENDER_HALO_TILES)
      );
    });
  }

  private drawTerrainWindow(
    g: Phaser.GameObjects.Graphics,
    data: MapState,
    rt: Phaser.GameObjects.RenderTexture | null
  ) {
    const originX = data.origin_x ?? 0;
    const originY = data.origin_y ?? 0;
    const { tiles } = data;
    for (let y = 0; y < data.height; y++) {
      for (let x = 0; x < data.width; x++) {
        const tile = tiles[y][x];
        const px = (originX + x) * TILE_SIZE;
        const py = (originY + y) * TILE_SIZE;
        const jitter = tileJitter(originX + x, originY + y);

        const base = baseFrameFor(tile);
        if (rt && base !== null) {
          this.drawAtlasTile(rt, tiles, originX, originY, tile, x, y, px, py, jitter);
        } else {
          // Procedural fallback so unmapped tile-ints never render blank.
          this.drawFallbackTile(g, tile, px, py, jitter);
        }
      }
    }
  }

  /** Stamp one atlas-backed tile (+ overlay + autotile edges + bridge) into rt.
   * Local-relative origin: rt is positioned at the chunk origin, so stamp at
   * the tile's chunk-local pixel offset. */
  private drawAtlasTile(
    rt: Phaser.GameObjects.RenderTexture,
    tiles: number[][],
    originX: number,
    originY: number,
    tile: number,
    x: number,
    y: number,
    px: number,
    py: number,
    jitter: number
  ) {
    const lx = px - originX * TILE_SIZE;
    const ly = py - originY * TILE_SIZE;
    const nb = this.neighborsOf(tiles, x, y, tile);

    // Base ground frame (grass/water/road/stone/...). A ROAD touching WATER is
    // swapped for the wooden bridge plank.
    const bridge = bridgeFrameFor(tile, nb);
    const base = bridge ?? baseFrameFor(tile)!;
    this.stamp(rt, base, lx, ly);

    // Edge autotiling: translucent sand shoreline where water meets land, and a
    // faint darker-green seam where grass meets forest.
    for (const edge of shorelineEdges(tile, nb)) this.stampEdge(rt, edge, lx, ly);
    for (const edge of forestBorderEdges(tile, nb)) this.stampEdge(rt, edge, lx, ly);

    // Feature overlay (tree / campfire / structure) drawn on top of the base.
    const overlay = overlayFor(tile, jitter);
    if (overlay) {
      this.stamp(rt, overlay.frame, lx, ly, overlay.alpha, overlay.tint);
    }
  }

  /** Stamp a 16px atlas frame upscaled to TILE_SIZE at local (lx,ly). */
  private stamp(
    rt: Phaser.GameObjects.RenderTexture,
    frame: number,
    lx: number,
    ly: number,
    alpha = 1,
    tint?: number
  ) {
    rt.stamp(ATLAS_KEY, frame, lx, ly, {
      originX: 0,
      originY: 0,
      scale: ATLAS_SCALE,
      alpha,
      ...(tint !== undefined ? { tint } : {}),
    });
  }

  /** Stamp an edge accent frame clipped (by half-tile offset) to one side. */
  private stampEdge(
    rt: Phaser.GameObjects.RenderTexture,
    edge: EdgeStamp,
    lx: number,
    ly: number
  ) {
    // Nudge the full-tile accent toward the relevant edge so it reads as a strip
    // hugging that side rather than recolouring the whole tile.
    const off = TILE_SIZE * 0.5;
    let dx = 0;
    let dy = 0;
    if (edge.side === "n") dy = -off;
    else if (edge.side === "s") dy = off;
    else if (edge.side === "e") dx = off;
    else dx = -off;
    rt.stamp(ATLAS_KEY, edge.frame, lx + dx, ly + dy, {
      originX: 0,
      originY: 0,
      scale: ATLAS_SCALE,
      alpha: edge.alpha,
      ...(edge.tint !== undefined ? { tint: edge.tint } : {}),
    });
  }

  /**
   * Original procedural color-jitter renderer, retained ONLY as the fallback for
   * tile-ints with no atlas frame (so the overworld never shows blank tiles).
   */
  private drawFallbackTile(
    g: Phaser.GameObjects.Graphics,
    tile: number,
    px: number,
    py: number,
    jitter: number
  ) {
    const blocked = BLOCKED_TILES.has(tile);
    const fill = terrainFill(tile, jitter);
    g.fillStyle(fill, 1);
    g.fillRect(px, py, TILE_SIZE, TILE_SIZE);

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

    if (blocked && tile !== TILE.WATER && tile !== TILE.MOUNTAIN) {
      g.fillStyle(0x1b2118, 0.85);
      g.fillRect(px + 6, py + 6, TILE_SIZE - 12, TILE_SIZE - 12);
    }

    if (tile === TILE.CAMP) {
      g.fillStyle(0xffcf3f, 0.55);
      g.fillRect(px + 10, py + 10, TILE_SIZE - 20, TILE_SIZE - 20);
    }
  }

  /** Lazily create / resize the terrain RenderTexture for one buffer slot. */
  private ensureTerrainRT(
    buffer: number,
    widthPx: number,
    heightPx: number
  ): Phaser.GameObjects.RenderTexture {
    const existing = this.terrainRT[buffer];
    if (existing && (existing.width !== widthPx || existing.height !== heightPx)) {
      existing.destroy();
      this.terrainRT[buffer] = null;
    }
    if (!this.terrainRT[buffer]) {
      const rt = this.add.renderTexture(0, 0, widthPx, heightPx);
      rt.setOrigin(0, 0);
      // The back buffer is created hidden (behind the front); swapBuffers raises
      // it to the front depth once the chunk is fully stamped.
      rt.setDepth(
        buffer === this.frontBuffer
          ? OverworldScene.RT_DEPTH[0]
          : OverworldScene.RT_DEPTH[1]
      );
      this.terrainRT[buffer] = rt;
    }
    return this.terrainRT[buffer]!;
  }

  private worldPois(): RoutablePOI[] {
    return this.worldData?.pois?.length ? this.worldData.pois : (this.mapData?.pois ?? []);
  }

  private drawPois(g: Phaser.GameObjects.Graphics) {
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

    const labelStyle: Phaser.Types.GameObjects.Text.TextStyle = {
      fontFamily: "Silkscreen, monospace",
      fontSize: "10px",
      color: "#e8e6d8",
      stroke: "#0e1018",
      strokeThickness: 4,
    };

    this.enemies.spawn(
      spawns,
      (enemy: Enemy) => {
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
      },
      (enemy: Enemy) => {
        const lbl = this.add.text(
          enemy.x,
          enemy.y - TILE_SIZE * 0.65,
          "???",
          labelStyle
        );
        lbl.setOrigin(0.5, 1);
        lbl.setDepth(6);
        return lbl;
      }
    );
  }

  private spawnNpcs() {
    this.npcs.destroy();
    const anchors = this.worldPois().flatMap((poi) => poi.npc_anchors ?? []);
    const npcLabelStyle: Phaser.Types.GameObjects.Text.TextStyle = {
      fontFamily: "Silkscreen, monospace",
      fontSize: "10px",
      color: "#ffcf3f",
      stroke: "#0e1018",
      strokeThickness: 4,
    };
    for (const anchor of anchors) {
      const spr = this.add.sprite(
        anchor.x * TILE_SIZE + TILE_SIZE / 2,
        anchor.y * TILE_SIZE + TILE_SIZE / 2,
        "npc"
      );
      spr.setDisplaySize(TILE_SIZE - 8, TILE_SIZE - 8);
      spr.setDepth(7);
      const displayName = (anchor.name ?? anchor.archetype).toUpperCase();
      const lbl = this.add.text(
        spr.x,
        spr.y - TILE_SIZE * 0.65,
        displayName,
        npcLabelStyle
      );
      lbl.setOrigin(0.5, 1);
      lbl.setDepth(8);
      this.npcs.add(anchor, spr, lbl);
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
    if (this.playerLabel) {
      const bob = Math.sin(time / 420) * 1.5;
      this.playerLabel.setPosition(this.sim.x, this.sim.y - TILE_SIZE * 0.65 + bob);
    }

    // Player ground shadow — soft ellipse, updates every frame.
    this.shadowGraphics.clear();
    this.shadowGraphics.fillStyle(0x0e1018, 0.32);
    this.shadowGraphics.fillEllipse(
      this.sim.x, this.sim.y + TILE_SIZE * 0.36,
      TILE_SIZE * 0.72, TILE_SIZE * 0.24
    );

    // Local terrain decorations — only redrawn when the player crosses a tile.
    const dtx = this.sim.tileX;
    const dty = this.sim.tileY;
    if (dtx !== this.lastDetailTile.x || dty !== this.lastDetailTile.y) {
      this.lastDetailTile = { x: dtx, y: dty };
      this.redrawLocalDetail(dtx, dty);
    }

    this.emitPlayerTile();
    this.npcs.update(time, this.sim.x, this.sim.y);
    this.updateNearestSign();
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
    if (!this.sim || !this.mapData || !this.liveWindow) return;

    const tx = this.sim.tileX;
    const ty = this.sim.tileY;

    // Always keep warming neighbour chunks while roaming so the next swap is
    // instant (data already cached). Cheap + throttled to once per tile change.
    this.maybePrefetchNeighbors(tx, ty);

    if (this.chunkFetchPending) return;
    if (time - this.lastChunkFetchAtMs < CHUNK_FETCH_THROTTLE_MS) return;

    // Re-centre only when the player nears the live chunk's edge margin. Because
    // the swap stamps into the BACK buffer first (the front stays visible), there
    // is no black gap even if the fetch is slow.
    if (!nearChunkEdge(this.liveWindow, tx, ty, CHUNK_EDGE_MARGIN_TILES)) return;

    this.chunkFetchPending = true;
    this.lastChunkFetchAtMs = time;
    void this.fetchMapAndDraw({ x: tx, y: ty }).finally(() => {
      this.chunkFetchPending = false;
    });
  }

  /**
   * Warm the 8 neighbour chunks (cardinals + diagonals) around the player so a
   * future re-centre swap is instant. Fires at most once per tile and dedupes by
   * chunk origin; results are cached in `prefetchedChunks` for fetchMapAndDraw to
   * consume. Best-effort: any failure is silently dropped (the on-demand fetch in
   * fetchMapAndDraw is the safety net).
   */
  private maybePrefetchNeighbors(tx: number, ty: number) {
    if (tx === this.lastPrefetchTile.x && ty === this.lastPrefetchTile.y) return;
    this.lastPrefetchTile = { x: tx, y: ty };

    const stride = CHUNK_FETCH_RADIUS_TILES; // re-centre when one radius away
    const centres = neighborPrefetchCentres(
      tx,
      ty,
      stride,
      this.mapData?.world_width ?? undefined,
      this.mapData?.world_height ?? undefined
    );
    for (const c of centres) {
      void this.prefetchChunk(c.x, c.y);
    }
  }

  /** Fetch one chunk into the prefetch cache, keyed by its returned origin. */
  private async prefetchChunk(centerX: number, centerY: number) {
    // Dedupe by center so two near-tiles don't double-fetch the same region.
    const reqKey = chunkKey(centerX, centerY);
    if (this.prefetchInFlight.has(reqKey)) return;
    this.prefetchInFlight.add(reqKey);
    try {
      const res = await fetch(this.mapUrl({ x: centerX, y: centerY }));
      if (!res.ok || !this.sys?.isActive()) return;
      const data = (await res.json()) as MapState;
      this.rememberChunk(data);
      this.paintCachedChunkIntoFront(data);
    } catch {
      // Best-effort warm-up; on-demand fetch covers any miss.
    } finally {
      this.prefetchInFlight.delete(reqKey);
    }
  }

  private rememberChunk(data: MapState) {
    const key = chunkKey(data.origin_x ?? 0, data.origin_y ?? 0);
    this.prefetchedChunks.set(key, data);
    this.evictPrefetchOverflow();
  }

  private paintCachedChunkIntoFront(data: MapState) {
    if (!this.liveWindow || !this.sys?.isActive()) return;
    const win = this.windowOf(data);
    if (!windowsWithinRenderHalo(this.liveWindow, win, CHUNK_RENDER_HALO_TILES)) return;
    const g = this.tileGraphics[this.frontBuffer];
    this.drawTerrainWindow(g, data, null);
    this.drawPois(g);
  }

  /**
   * Reuse a prefetched chunk whose window contains the requested centre. Keeping
   * it cached lets the same window remain renderable as an adjacent/behind chunk
   * after the player recentres into a different window.
   */
  private takePrefetched(centerTile: { x: number; y: number }): MapState | null {
    for (const data of this.prefetchedChunks.values()) {
      const ox = data.origin_x ?? 0;
      const oy = data.origin_y ?? 0;
      if (
        centerTile.x >= ox &&
        centerTile.y >= oy &&
        centerTile.x < ox + data.width &&
        centerTile.y < oy + data.height &&
        // Don't reuse the chunk we already display.
        shouldSwapChunk(this.liveWindow, this.windowOf(data))
      ) {
        return data;
      }
    }
    return null;
  }

  /** Keep the prefetch cache bounded (drop oldest insertions first). */
  private evictPrefetchOverflow() {
    while (this.prefetchedChunks.size > OverworldScene.PREFETCH_CACHE_LIMIT) {
      const oldest = this.prefetchedChunks.keys().next().value;
      if (oldest === undefined) break;
      this.prefetchedChunks.delete(oldest);
    }
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

  /** Redraw rich sub-tile decorations across the full visible screen. */
  private redrawLocalDetail(_centerTx: number, _centerTy: number) {
    if (!this.mapData) return;
    const g = this.detailGraphics;
    g.clear();
    const originX = this.mapData.origin_x ?? 0;
    const originY = this.mapData.origin_y ?? 0;
    const cam = this.cameras.main;

    // Cover every tile visible in the camera viewport, plus one tile of padding
    // to avoid pop-in at edges as the camera scrolls.
    const leftTile   = Math.floor(cam.scrollX / TILE_SIZE) - 1;
    const topTile    = Math.floor(cam.scrollY / TILE_SIZE) - 1;
    const rightTile  = Math.ceil((cam.scrollX + cam.width)  / TILE_SIZE) + 1;
    const bottomTile = Math.ceil((cam.scrollY + cam.height) / TILE_SIZE) + 1;

    for (let ty = topTile; ty <= bottomTile; ty++) {
      for (let tx = leftTile; tx <= rightTile; tx++) {
        const lx = tx - originX;
        const ly = ty - originY;
        if (ly < 0 || ly >= this.mapData.tiles.length) continue;
        const row = this.mapData.tiles[ly];
        if (!row || lx < 0 || lx >= row.length) continue;
        const tile = row[lx];
        const px = tx * TILE_SIZE;
        const py = ty * TILE_SIZE;
        const j = tileJitter(tx, ty);
        this.drawTileDetail(g, tile, px, py, j);
      }
    }
  }

  private drawTileDetail(
    g: Phaser.GameObjects.Graphics,
    tile: number,
    px: number,
    py: number,
    j: number
  ) {
    switch (tile) {
      case TILE.GRASS:    this.detailGrass(g, px, py, j);    break;
      case TILE.FOREST:   this.detailForest(g, px, py, j);   break;
      case TILE.MOUNTAIN: this.detailMountain(g, px, py, j); break;
      case TILE.ROAD:     this.detailRoad(g, px, py, j);     break;
      case TILE.FEATURE:  this.detailFeature(g, px, py, j);  break;
      case TILE.TOWN:     this.detailTown(g, px, py, j);     break;
      case TILE.WATER:    this.detailWater(g, px, py, j);    break;
      case TILE.CAVE:     this.detailCave(g, px, py, j);     break;
    }
  }

  private detailGrass(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    // ~12% of grass tiles become farmland (crop rows).
    if ((j & 0x1f) < 4) {
      this.detailFarm(g, px, py, j);
      return;
    }
    // Two or three blade pairs scattered across the tile.
    g.fillStyle(0x567a38, 0.75);
    const b1x = px + 4 + (j & 0x7);
    const b1y = py + TILE_SIZE - 7;
    g.fillRect(b1x,     b1y,     1, 4);
    g.fillRect(b1x + 2, b1y + 1, 1, 3);
    const b2x = px + 14 + ((j >> 4) & 0x7);
    g.fillRect(b2x,     b1y,     1, 5);
    g.fillRect(b2x - 2, b1y + 2, 1, 3);
    if ((j & 0x3) === 0) {
      g.fillRect(px + 22 + ((j >> 8) & 0x5), b1y + 1, 1, 4);
    }
    // Occasional small flower dot.
    if ((j & 0xf) < 3) {
      g.fillStyle(0xffe080, 0.85);
      g.fillRect(px + 10 + ((j >> 8) & 0xf), py + TILE_SIZE - 9, 2, 2);
    }
  }

  private detailFarm(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    // Plowed soil rows.
    const rowH = 4;
    const soilColor  = 0x6b4226;
    const cropColor  = 0x4a9130;
    const crop2Color = 0x7ec850;
    for (let row = 0; row < 4; row++) {
      const ry = py + 4 + row * (rowH + 2);
      g.fillStyle(soilColor, 0.55);
      g.fillRect(px + 2, ry + rowH - 1, TILE_SIZE - 4, 2); // soil furrow
      g.fillStyle(cropColor, 0.7);
      g.fillRect(px + 2, ry, TILE_SIZE - 4, rowH - 1);    // crop strip
      // Crop plant symbols (small + shapes)
      const plantX = px + 4 + ((j >> (row * 3)) & 0x9);
      g.fillStyle(crop2Color, 0.9);
      g.fillRect(plantX,     ry + 1, 3, 1); // horizontal
      g.fillRect(plantX + 1, ry,     1, 3); // vertical
      const plantX2 = plantX + 10 + ((j >> (row * 2 + 5)) & 0x5);
      g.fillRect(plantX2,     ry + 1, 3, 1);
      g.fillRect(plantX2 + 1, ry,     1, 3);
    }
  }

  private detailForest(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    const cx = px + TILE_SIZE / 2 + ((j & 0x3) - 1);
    // Trunk
    g.fillStyle(0x3d2c22, 0.9);
    g.fillRect(cx - 2, py + TILE_SIZE - 9, 4, 9);
    // Shadow beneath crown
    g.fillStyle(0x1a3018, 0.3);
    g.fillEllipse(cx, py + TILE_SIZE / 2 + 4, 20, 8);
    // Crown — two layers for depth
    g.fillStyle(0x1e4d20, 0.82);
    g.fillCircle(cx, py + TILE_SIZE / 2 - 2, 9);
    g.fillStyle(0x2d6b2a, 0.5);
    g.fillCircle(cx - 2, py + TILE_SIZE / 2 - 4, 5);
  }

  private detailMountain(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    // Scattered pebbles.
    g.fillStyle(0x9a9e9e, 0.65);
    g.fillRect(px + 4  + (j & 0x7),        py + TILE_SIZE - 9 + ((j >> 3) & 0x3), 3, 2);
    g.fillRect(px + 16 + ((j >> 5) & 0x7), py + TILE_SIZE - 7 + ((j >> 8) & 0x3), 2, 2);
    g.fillRect(px + 10 + ((j >> 10) & 0x5),py + TILE_SIZE - 5,                     2, 3);
    // Snow speck near the peak if high-jitter tile.
    if ((j & 0x1f) < 8) {
      g.fillStyle(0xdedbd0, 0.5);
      g.fillRect(px + TILE_SIZE / 2 - 1 + ((j >> 2) & 0x3), py + 8, 3, 2);
    }
  }

  private detailRoad(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    // Worn wheel-track lines running left-right.
    g.fillStyle(0x7a5c32, 0.42);
    g.fillRect(px + 3, py + 6  + ((j >> 2) & 0x3), TILE_SIZE - 6, 1);
    g.fillRect(px + 3, py + TILE_SIZE - 9 + ((j >> 5) & 0x3), TILE_SIZE - 6, 1);
    // Center worn strip.
    g.fillStyle(0x8c6a42, 0.18);
    g.fillRect(px + 8, py + 4, TILE_SIZE - 16, TILE_SIZE - 8);
  }

  private detailTown(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    const style = j & 0x3; // 0=house 1=shop 2=inn 3=barn
    if (style === 3) {
      // Barn: wide reddish planks + loft window
      g.fillStyle(0x8b3a1e, 0.6);
      g.fillRect(px + 2, py + 4, TILE_SIZE - 4, TILE_SIZE - 10);
      g.fillStyle(0xd4a96a, 0.4);
      g.fillRect(px + 2, py + 4, TILE_SIZE - 4, 1); // top plank line
      g.fillRect(px + 2, py + 10, TILE_SIZE - 4, 1);
      g.fillStyle(0xfff4b0, 0.5);
      g.fillRect(px + TILE_SIZE / 2 - 3, py + 5, 6, 4); // loft window
    } else {
      // House/shop/inn: standard window + optional chimney
      const windowX = px + 4 + ((j >> 4) & 0x5);
      g.fillStyle(0xfff4b0, style === 1 ? 0.7 : 0.5);
      g.fillRect(windowX, py + 4, 7, 6);
      g.fillStyle(0x5c4a20, 0.65);
      g.fillRect(windowX + 3, py + 4, 1, 6);
      g.fillRect(windowX,     py + 6, 7, 1);
      // Chimney on houses
      if (style === 0) {
        g.fillStyle(0x6b4226, 0.8);
        g.fillRect(px + TILE_SIZE - 8, py, 4, 6);
        g.fillStyle(0x3d2c22, 0.5);
        g.fillRect(px + TILE_SIZE - 9, py, 6, 2); // chimney top
      }
      // Sign above shop door
      if (style === 1) {
        g.fillStyle(0xffcf3f, 0.55);
        g.fillRect(px + 4, py + 2, 10, 2);
      }
      // Door
      if ((j & 0x7) < 5) {
        g.fillStyle(0x3d2c22, 0.75);
        g.fillRect(px + TILE_SIZE - 10, py + TILE_SIZE - 9, 5, 9);
      }
    }
    // Fence rail along bottom edge of most town tiles
    if ((j & 0x3) !== 3) {
      g.lineStyle(1, 0x8b6914, 0.5);
      g.strokeRect(px + 1, py + TILE_SIZE - 5, TILE_SIZE - 2, 4);
      g.fillStyle(0x8b6914, 0.6);
      g.fillRect(px + 4,              py + TILE_SIZE - 5, 2, 4); // post
      g.fillRect(px + TILE_SIZE - 7,  py + TILE_SIZE - 5, 2, 4); // post
    }
  }

  private detailFeature(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    // Feature tiles render as fence sections with posts.
    g.fillStyle(0x8b6914, 0.75);
    // Two posts
    g.fillRect(px + 3,              py + 4, 3, TILE_SIZE - 8);
    g.fillRect(px + TILE_SIZE - 7,  py + 4, 3, TILE_SIZE - 8);
    // Top rail
    g.fillRect(px + 1, py + 6, TILE_SIZE - 2, 2);
    // Bottom rail
    g.fillRect(px + 1, py + TILE_SIZE - 9, TILE_SIZE - 2, 2);
    // Post caps
    g.fillStyle(0xd4a96a, 0.6);
    g.fillRect(px + 3, py + 4, 3, 2);
    g.fillRect(px + TILE_SIZE - 7, py + 4, 3, 2);
  }

  private detailWater(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    // Two ripple arc hints at phase-shifted positions.
    g.lineStyle(1, 0x91d4ef, 0.35);
    const r1x = px + 6 + (j & 0x7);
    const r1y = py + 8 + ((j >> 3) & 0x5);
    g.strokeRect(r1x, r1y, 10, 4);
    const r2x = px + 14 + ((j >> 6) & 0x7);
    const r2y = py + TILE_SIZE - 12 + ((j >> 9) & 0x5);
    g.strokeRect(r2x, r2y, 8, 3);
  }

  private detailCave(g: Phaser.GameObjects.Graphics, px: number, py: number, j: number) {
    // Stalactite drip marks.
    g.fillStyle(0x5a4a52, 0.55);
    const s1x = px + 7 + (j & 0x7);
    g.fillRect(s1x, py + 4, 2, 5 + ((j >> 3) & 0x3));
    g.fillRect(s1x + 1, py + 9 + ((j >> 3) & 0x3), 1, 1);
    const s2x = px + 18 + ((j >> 5) & 0x7);
    g.fillRect(s2x, py + 6, 2, 4 + ((j >> 8) & 0x3));
    // Drip pool dot.
    if ((j & 0xf) < 5) {
      g.fillStyle(0x2a3f5a, 0.6);
      g.fillRect(s1x, py + TILE_SIZE - 6, 3, 2);
    }
  }

  private buildSigns() {
    this.destroySigns();
    if (!this.mapData || !this.textures.exists("sign")) return;
    const originX = this.mapData.origin_x ?? 0;
    const originY = this.mapData.origin_y ?? 0;

    for (let ly = 0; ly < this.mapData.height; ly++) {
      for (let lx = 0; lx < this.mapData.width; lx++) {
        const tile = this.mapData.tiles[ly][lx];
        const tx = lx + originX;
        const ty = ly + originY;
        const j = tileJitter(tx, ty);
        // Eligible: ROAD ~1%, FEATURE ~2%, GRASS ~0.4%
        const isSign =
          (tile === TILE.ROAD    && (j & 0x7f) === 3) ||
          (tile === TILE.FEATURE && (j & 0x3f) === 7) ||
          (tile === TILE.GRASS   && (j & 0x1ff) === 11);
        if (!isSign) continue;

        const key = `${tx},${ty}`;
        const text = SIGN_POOL[Math.abs(j >> 4) % SIGN_POOL.length];
        const wx = tx * TILE_SIZE + TILE_SIZE / 2;
        const wy = ty * TILE_SIZE + TILE_SIZE / 2;
        const spr = this.add.sprite(wx, wy - 6, "sign");
        spr.setDisplaySize(TILE_SIZE * 0.8, TILE_SIZE * 0.8);
        spr.setDepth(4);
        this.signs.push({ key, tileX: tx, tileY: ty, text, spr });
      }
    }
  }

  private destroySigns() {
    for (const s of this.signs) s.spr.destroy();
    this.signs = [];
    this.hideSignPopup();
    this.nearestSignKey = "";
  }

  private updateNearestSign() {
    if (!this.sim) return;
    let nearest: typeof this.signs[0] | null = null;
    let bestDist = Infinity;
    for (const s of this.signs) {
      const wx = s.tileX * TILE_SIZE + TILE_SIZE / 2;
      const wy = s.tileY * TILE_SIZE + TILE_SIZE / 2;
      const d = Math.hypot(this.sim.x - wx, this.sim.y - wy);
      if (d < SIGN_INTERACT_PX && d < bestDist) {
        nearest = s;
        bestDist = d;
      }
    }
    const key = nearest?.key ?? "";
    if (key !== this.nearestSignKey) {
      this.nearestSignKey = key;
      if (nearest) this.showSignPopup(nearest.text);
      else this.hideSignPopup();
    }
  }

  private showSignPopup(text: string) {
    this.hideSignPopup();
    const cam = this.cameras.main;
    const W = Math.min(320, cam.width - 32);
    const H = 56;
    const sx = (cam.width - W) / 2;
    const sy = cam.height - H - 24;

    const bg = this.add.graphics();
    bg.fillStyle(0x0e1018, 0.92);
    bg.fillRect(sx, sy, W, H);
    bg.lineStyle(2, 0xffcf3f, 0.9);
    bg.strokeRect(sx, sy, W, H);
    // Pin icon strip on left
    bg.fillStyle(0xffcf3f, 0.25);
    bg.fillRect(sx, sy, 18, H);
    bg.lineStyle(0, 0, 0);
    bg.fillStyle(0xffcf3f, 0.9);
    bg.fillRect(sx + 7, sy + H / 2 - 5, 4, 4);  // pin head
    bg.fillRect(sx + 8, sy + H / 2 - 1, 2, 8);  // pin post
    bg.setScrollFactor(0);
    bg.setDepth(500);

    const txt = this.add.text(sx + 26, sy + H / 2, text, {
      fontFamily: "Silkscreen, monospace",
      fontSize: "8px",
      color: "#e8e6d8",
      wordWrap: { width: W - 32 },
    });
    txt.setOrigin(0, 0.5);
    txt.setScrollFactor(0);
    txt.setDepth(501);

    this.signPopupBg = bg;
    this.signPopupText = txt;
  }

  private hideSignPopup() {
    this.signPopupBg?.destroy();
    this.signPopupText?.destroy();
    this.signPopupBg = null;
    this.signPopupText = null;
  }

  destroy() {
    this.enemies.destroy();
    this.npcs.destroy();
    this.destroySigns();
    this.detailGraphics?.destroy();
    this.shadowGraphics?.destroy();
    this.playerLabel?.destroy();
    this.playerLabel = null;
    this.terrainRT[0]?.destroy();
    this.terrainRT[1]?.destroy();
    this.terrainRT = [null, null];
    this.prefetchedChunks.clear();
    this.prefetchInFlight.clear();
  }
}
