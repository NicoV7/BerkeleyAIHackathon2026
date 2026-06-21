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
  windowsWithinRenderHalo,
  shouldSwapChunk,
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
  overlayFor,
  shorelineBaseFrame,
  forestFeatherOverlay,
  grassTint,
  type Neighbors,
} from "./TileAtlas";
import { WorldSim, type MoveIntent } from "./WorldSim";
import { TILE_SIZE } from "./constants";
export { TILE_SIZE } from "./constants";

/** How often (ms) to persist the player's absolute position to the server. */
const SYNC_DEBOUNCE_MS = 1500;
// Chunk window = radius*2. 96 was "too big"; 48 made the window ~viewport-sized,
// so re-centre swaps fired constantly (visible "tiles swapping"). 64 lets one
// chunk cover a typical viewport — infrequent, seamless re-centres — while
// staying well under the old 96. Cached neighbours still fill big screens at the
// next re-centre.
const CHUNK_FETCH_RADIUS_TILES = 32;
// How far (tiles) a cached chunk may sit from the anchor and still be painted
// into the live buffer, so the area around the player is gap-free.
const CHUNK_RENDER_HALO_TILES = 2;
// Chunks tile the world on a fixed grid of this side length (= radius*2). Snapping
// requests to this grid keeps chunk origins stable, so neighbours are fetched once
// and cached (not re-requested every tile) and re-centres happen only on cell cross.
const CHUNK_SIZE = CHUNK_FETCH_RADIUS_TILES * 2;
// Lead (in tiles) added around the ACTUAL viewport when deciding to re-centre.
// Each buffer renders a single chunk window (no neighbour halo), so we must swap
// before the visible area spills past the chunk edge. Triggering off the real
// camera viewport (+ this lead) is gap-free on any display size; prefetch keeps
// the swap instant.
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
/** Phaser texture key for the loaded Kenney roguelike terrain atlas (spritesheet). */
const ATLAS_KEY = "rogue";
/** Same sheet loaded as a plain image, used as the Tilemap tileset (Wave 0). */
const TILESET_KEY = "rogue_tiles";
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
  onEncounter: (wildId?: string | null) => void;
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

/** One double-buffer slot: a Tilemap with a base ground layer + feature overlay. */
interface TerrainBuffer {
  map: Phaser.Tilemaps.Tilemap;
  base: Phaser.Tilemaps.TilemapLayer;
  overlay: Phaser.Tilemaps.TilemapLayer;
  widthTiles: number;
  heightTiles: number;
  /** Global tile origin the buffer is currently positioned at (for reuse checks). */
  originX: number;
  originY: number;
}

export class OverworldScene extends Phaser.Scene {
  private cfg!: OverworldConfig;

  // Map data (loaded once per scene start)
  private mapData: MapState | null = null;
  private worldData: WorldState | null = null;

  // Terrain renders via Phaser TILEMAP layers (Wave 0) — DOUBLE BUFFERED.
  // Two terrain buffers (front = visible, back = hidden), each a Tilemap with a
  // base ground layer + a feature overlay layer. A new chunk is fully populated
  // into the BACK buffer, then depths swap so it becomes the front; the old
  // terrain stays on screen the whole time the new chunk fetches + populates, so
  // there is never a black gap. This replaces the old RenderTexture.stamp path,
  // which left terrain BLANK in Canvas (stamp is a no-op) and Phaser-4 WebGL
  // (RenderTexture.stamp didn't composite). Tilemaps render the Kenney atlas
  // correctly under BOTH renderers, so the foundation needs no renderer switch.
  // The thin per-buffer Graphics layer draws POI markers (+ a procedural fallback
  // for any unmapped tile-int) on top of the tilemap so nothing renders blank.
  private terrain: [TerrainBuffer | null, TerrainBuffer | null] = [null, null];
  private tileGraphics!: [Phaser.GameObjects.Graphics, Phaser.GameObjects.Graphics];
  /** Index (0|1) of the buffer currently shown to the player. */
  private frontBuffer = 0;
  /** The chunk window currently displayed in the front buffer. */
  private liveWindow: ChunkWindow | null = null;
  // Detail overlay layer (#24): procedural sub-tile decorations drawn each step.
  // (The #24 player-shadow layer is dropped — V3's shadowGfx covers player + all
  // actors. atlasReady is gone — the Wave-0 tilemap path doesn't need it.)
  private detailGraphics!: Phaser.GameObjects.Graphics;
  private playerSprite!: Phaser.GameObjects.Sprite;
  private playerAnimator!: PlayerSpriteAnimator;
  private playerLabel: Phaser.GameObjects.Text | null = null;
  private lastDetailTile = { x: -1, y: -1 };

  // Sign system
  private signs: Array<{ key: string; tileX: number; tileY: number; text: string; spr: Phaser.GameObjects.Sprite }> = [];
  private signPopupBg: Phaser.GameObjects.Graphics | null = null;
  private signPopupText: Phaser.GameObjects.Text | null = null;
  private nearestSignKey = "";

  // V3: blob shadows under actors (one Graphics layer, redrawn each frame) + a
  // water shimmer (throttled brightness pulse on the visible water tiles).
  private shadowGfx!: Phaser.GameObjects.Graphics;
  /** Water cells in FRONT-buffer-local tile coords (collected while painting). */
  private liveWaterCells: { x: number; y: number }[] = [];
  private lastShimmerMs = 0;

  // Depth bands per buffer slot, indexed [front, back]. The whole front buffer
  // (base + overlay + POI graphics) renders above the whole back buffer, and all
  // terrain renders below the actors (enemy=5, npc=7, player=10).
  private static readonly BASE_DEPTH = [-8, -16] as const;
  private static readonly OVERLAY_DEPTH = [-7, -15] as const;
  private static readonly G_DEPTH = [-6, -14] as const;

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
  /** Grid cell we last warmed neighbours for — prefetch re-fires only when the
   *  player crosses into a new chunk cell, not every tile. */
  private lastPrefetchCell = { x: Number.NaN, y: Number.NaN };
  /** Grid cell the live chunk is centred on — re-centre only on a cell change. */
  private activeCell = { x: Number.NaN, y: Number.NaN };
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
    // Also load the terrain sheet as a plain IMAGE for use as a Tilemap tileset
    // (addTilesetImage needs an image texture). Same geometry as the spritesheet:
    // 16px tiles, 1px spacing, 0 margin → 57×31, so frame index == tilemap index.
    this.load.image(TILESET_KEY, SHEET);
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
    // Terrain renders via Phaser Tilemap layers, created lazily per buffer in
    // ensureTerrainBuffer(). Two thin Graphics layers sit just above each
    // buffer's tilemap for POI markers (+ a procedural fallback for any unmapped
    // tile-int), and below the actors.
    const g0 = this.add.graphics();
    g0.setDepth(OverworldScene.G_DEPTH[0]);
    const g1 = this.add.graphics();
    g1.setDepth(OverworldScene.G_DEPTH[1]);
    this.tileGraphics = [g0, g1];
    this.frontBuffer = 0;
    this.liveWindow = null;
    this.detailGraphics = this.add.graphics();
    this.detailGraphics.setDepth(2);
    this.lastDetailTile = { x: -1, y: -1 };
    this.cameras.main.setBackgroundColor("#1a1a2e");

    // Blob-shadow layer: above terrain (depth < 0), below all actors (enemy=5,
    // npc=7/9, player=10). Redrawn every frame in drawShadows().
    this.shadowGfx = this.add.graphics();
    this.shadowGfx.setDepth(4);

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
    // refreshMap() does scene.restart, which fires SHUTDOWN (NOT a Scene method
    // named destroy()). Without cleaning up here, every encounter→battle→return
    // loop orphans the prior life's tilemaps. Clean render objects + flush on both.
    this.events.once(Phaser.Scenes.Events.SHUTDOWN, () => {
      this.flushSync();
      this.cleanupRender();
    });
    this.events.once(Phaser.Scenes.Events.DESTROY, () => {
      this.flushSync();
      this.cleanupRender();
    });

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
    params.set("chunk_size", String(CHUNK_SIZE));
    if (centerTile) {
      // Snap to the chunk-GRID cell centre so chunks tile the world on a fixed
      // grid (stable origins) instead of shifting per-tile with the player.
      const cc = this.cellCenter(centerTile.x, centerTile.y);
      params.set("center_x", String(cc.x));
      params.set("center_y", String(cc.y));
    }
    return `/api/runs/${this.cfg.runId}/map?${params.toString()}`;
  }

  /** Grid cell index for a tile coord. */
  private cellOf(tile: number): number {
    return Math.floor(tile / CHUNK_SIZE);
  }

  /** Centre tile of the grid cell containing (tx, ty). */
  private cellCenter(tx: number, ty: number): { x: number; y: number } {
    const half = Math.floor(CHUNK_SIZE / 2);
    return {
      x: this.cellOf(tx) * CHUNK_SIZE + half,
      y: this.cellOf(ty) * CHUNK_SIZE + half,
    };
  }

  /** True if a cached chunk already covers this centre tile (skip re-fetch). */
  private chunkCovering(x: number, y: number): boolean {
    for (const d of this.prefetchedChunks.values()) {
      const ox = d.origin_x ?? 0;
      const oy = d.origin_y ?? 0;
      if (x >= ox && y >= oy && x < ox + d.width && y < oy + d.height) return true;
    }
    return false;
  }

  private windowOf(data: MapState): ChunkWindow {
    return {
      originX: data.origin_x ?? 0,
      originY: data.origin_y ?? 0,
      width: data.width,
      height: data.height,
    };
  }

  /**
   * Merged COLLISION grid for the anchor + cached neighbours (same set we render),
   * so the sim's blocked-tile test works across cell boundaries. Without this the
   * single-chunk sim treats every chunk edge as an out-of-bounds wall, deadlocking
   * traversal at tile ~63 of a 1024-wide world. Uncovered cells default to
   * walkable so a not-yet-loaded neighbour never blocks movement.
   */
  private buildMergedTiles(anchor: MapState): { tiles: number[][]; originX: number; originY: number } {
    const chunks = this.renderChunkSet(anchor);
    let bx0 = Infinity;
    let by0 = Infinity;
    let bx1 = -Infinity;
    let by1 = -Infinity;
    for (const c of chunks) {
      const ox = c.origin_x ?? 0;
      const oy = c.origin_y ?? 0;
      if (ox < bx0) bx0 = ox;
      if (oy < by0) by0 = oy;
      if (ox + c.width > bx1) bx1 = ox + c.width;
      if (oy + c.height > by1) by1 = oy + c.height;
    }
    const w = bx1 - bx0;
    const h = by1 - by0;
    const tiles: number[][] = Array.from({ length: h }, () => new Array(w).fill(0));
    for (const c of chunks) {
      const ox = (c.origin_x ?? 0) - bx0;
      const oy = (c.origin_y ?? 0) - by0;
      for (let y = 0; y < c.height; y++) {
        const row = c.tiles[y];
        const dst = tiles[oy + y];
        for (let x = 0; x < c.width; x++) dst[ox + x] = row[x];
      }
    }
    return { tiles, originX: bx0, originY: by0 };
  }

  /** Refresh the sim's collision grid to the merged neighbourhood (no render). */
  private syncSimTiles() {
    if (!this.sim || !this.mapData) return;
    const m = this.buildMergedTiles(this.mapData);
    this.sim.setTiles(m.tiles, m.originX, m.originY);
  }

  /** Re-paint the neighbourhood (back buffer + seamless swap) and refresh
   *  collision — used when a newly-cached neighbour extends coverage. */
  private refreshNeighborhood() {
    if (!this.sim || !this.mapData) return;
    this.liveWindow = this.drawMapInto(this.backIndex(), this.mapData);
    this.swapBuffers();
    this.syncSimTiles();
  }

  /** True if `win` is fully inside the current liveWindow (already rendered). */
  private windowCovered(win: ChunkWindow): boolean {
    const lw = this.liveWindow;
    if (!lw) return false;
    return (
      win.originX >= lw.originX &&
      win.originY >= lw.originY &&
      win.originX + win.width <= lw.originX + lw.width &&
      win.originY + win.height <= lw.originY + lw.height
    );
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
        onEncounter: this.cfg.onEncounter,
        onEnterInterior: () => this.flushSync(),
      });

      const win = this.windowOf(data);
      const { originX, originY } = win;
      this.rememberChunk(data);
      await this.warmChunkNeighborhood(data);
      if (!this.sys?.isActive()) return;

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
        const bbox = this.drawMapInto(this.backIndex(), data);
        this.swapBuffers();
        this.mapData = data;
        // Collision spans the merged neighbourhood, not one chunk, so the player
        // can cross cell boundaries instead of hitting an OOB wall.
        this.liveWindow = bbox;
        this.syncSimTiles();
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
      this.activeCell = { x: this.cellOf(start.x), y: this.cellOf(start.y) };

      this.liveWindow = this.drawMapInto(this.frontBuffer, data);
      this.syncSimTiles();
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
    this.applyBufferDepths(back, 0); // back → front depth band
    this.applyBufferDepths(this.frontBuffer, 1); // front → back depth band
    // Hide the old front (now back) so its protruding/stale edges can't ghost
    // through as "double rendering"; show the freshly-painted new front.
    this.setBufferVisible(this.frontBuffer, false);
    this.setBufferVisible(back, true);
    this.frontBuffer = back;
  }

  /** Push one buffer slot's layers to the [front=0|back=1] depth band. */
  private applyBufferDepths(buffer: number, band: 0 | 1) {
    const t = this.terrain[buffer];
    t?.base.setDepth(OverworldScene.BASE_DEPTH[band]);
    t?.overlay.setDepth(OverworldScene.OVERLAY_DEPTH[band]);
    this.tileGraphics[buffer].setDepth(OverworldScene.G_DEPTH[band]);
  }

  /** Show/hide a buffer slot's terrain layers + POI graphics together. */
  private setBufferVisible(buffer: number, vis: boolean) {
    const t = this.terrain[buffer];
    t?.base.setVisible(vis);
    t?.overlay.setVisible(vis);
    this.tileGraphics[buffer]?.setVisible(vis);
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
  /**
   * Paint a buffer with the anchor chunk PLUS every cached neighbour within the
   * render halo, sized to their bounding box, so the area around the player is
   * always covered — no black chunks at the seams. Returns the bbox window (the
   * caller stores it as the live coverage for re-centre decisions). Also collects
   * the front-buffer-local water cells for the shimmer.
   */
  private drawMapInto(buffer: number, anchor: MapState): ChunkWindow {
    const g = this.tileGraphics[buffer];
    g.clear();

    const chunks = this.renderChunkSet(anchor);
    let bx0 = Infinity;
    let by0 = Infinity;
    let bx1 = -Infinity;
    let by1 = -Infinity;
    for (const c of chunks) {
      const ox = c.origin_x ?? 0;
      const oy = c.origin_y ?? 0;
      if (ox < bx0) bx0 = ox;
      if (oy < by0) by0 = oy;
      if (ox + c.width > bx1) bx1 = ox + c.width;
      if (oy + c.height > by1) by1 = oy + c.height;
    }
    const widthTiles = bx1 - bx0;
    const heightTiles = by1 - by0;

    const buf = this.ensureTerrainBuffer(buffer, widthTiles, heightTiles, bx0, by0);
    // Position layers at the bbox global pixel origin; setScale upscales 16→32.
    buf.base.setPosition(bx0 * TILE_SIZE, by0 * TILE_SIZE);
    buf.overlay.setPosition(bx0 * TILE_SIZE, by0 * TILE_SIZE);

    this.liveWaterCells = [];
    for (const c of chunks) this.drawTerrainWindow(g, buf, c, bx0, by0);
    this.drawPois(g);

    return { originX: bx0, originY: by0, width: widthTiles, height: heightTiles };
  }

  /**
   * The anchor chunk + cached neighbour chunks within the render halo. Painting
   * them together is what fills the area around the player so chunk seams never
   * show as black gaps. The prefetcher keeps the neighbours warm in the cache.
   */
  private renderChunkSet(anchor: MapState): MapState[] {
    const anchorWin = this.windowOf(anchor);
    const anchorKey = chunkKey(anchorWin.originX, anchorWin.originY);
    const out: MapState[] = [anchor];
    for (const cached of this.prefetchedChunks.values()) {
      const win = this.windowOf(cached);
      if (chunkKey(win.originX, win.originY) === anchorKey) continue;
      if (windowsWithinRenderHalo(anchorWin, win, CHUNK_RENDER_HALO_TILES)) {
        out.push(cached);
      }
    }
    return out;
  }

  /**
   * Populate one terrain buffer's tilemap layers from `data` (chunk-local tile
   * coords). Every tile-int with an atlas frame writes its base ground frame
   * (ROAD→bridge where it touches water) and, if applicable, a feature frame on
   * the overlay layer. Tile-ints with no atlas frame clear both cells and fall
   * back to the procedural Graphics renderer so nothing renders blank.
   */
  private drawTerrainWindow(
    g: Phaser.GameObjects.Graphics,
    buf: TerrainBuffer,
    data: MapState,
    bboxOriginX: number,
    bboxOriginY: number
  ) {
    const originX = data.origin_x ?? 0;
    const originY = data.origin_y ?? 0;
    const { tiles } = data;
    // Chunk's offset within the bbox buffer (buffer-local = bbox-relative).
    const offX = originX - bboxOriginX;
    const offY = originY - bboxOriginY;
    for (let y = 0; y < data.height; y++) {
      for (let x = 0; x < data.width; x++) {
        const tile = tiles[y][x];
        const cx = offX + x;
        const cy = offY + y;
        const jitter = tileJitter(originX + x, originY + y);
        const base = baseFrameFor(tile);

        if (base !== null) {
          const nb = this.neighborsOf(tiles, x, y, tile);
          // V1 cohesion: shoreline beach on water-edge land, else bridge, else base.
          // recalculateFaces=false: collision is WorldSim's job, not the tilemap's.
          // Bridge wins over shoreline so a ROAD crossing water stays a bridge.
          const baseFrame =
            bridgeFrameFor(tile, nb) ?? shorelineBaseFrame(tile, nb) ?? base;
          const bt = buf.base.putTileAt(baseFrame, cx, cy, false);
          if (bt) bt.tint = grassTint(tile, jitter) ?? 0xffffff;
          if (tile === TILE.WATER) this.liveWaterCells.push({ x: cx, y: cy });

          // Feature overlay (tree/campfire/structure), else feather the forest
          // edge with sparse scrub trees on bordering grass.
          const overlay =
            overlayFor(tile, jitter) ?? forestFeatherOverlay(tile, nb, jitter);
          if (overlay) {
            const t = buf.overlay.putTileAt(overlay.frame, cx, cy, false);
            if (t) {
              t.alpha = overlay.alpha;
              t.tint = overlay.tint ?? 0xffffff;
            }
          } else {
            buf.overlay.removeTileAt(cx, cy, true, false);
          }
        } else {
          // Unmapped tile-int: clear tilemap cells + draw procedural fallback.
          buf.base.removeTileAt(cx, cy, true, false);
          buf.overlay.removeTileAt(cx, cy, true, false);
          const px = (originX + x) * TILE_SIZE;
          const py = (originY + y) * TILE_SIZE;
          this.drawFallbackTile(g, tile, px, py, jitter);
        }
      }
    }
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

  /**
   * Lazily create the Tilemap terrain buffer for one slot, re-creating it whenever
   * the bbox dimensions OR origin change. Recreating on any change guarantees a
   * fresh (empty) grid, so stale tiles from a previous window can never linger —
   * the only reuse is an identical bbox (same dims + origin), which is then simply
   * repainted in place.
   */
  private ensureTerrainBuffer(
    buffer: number,
    widthTiles: number,
    heightTiles: number,
    originX: number,
    originY: number
  ): TerrainBuffer {
    const existing = this.terrain[buffer];
    if (
      existing &&
      (existing.widthTiles !== widthTiles ||
        existing.heightTiles !== heightTiles ||
        existing.originX !== originX ||
        existing.originY !== originY)
    ) {
      existing.map.destroy();
      this.terrain[buffer] = null;
    }
    if (!this.terrain[buffer]) {
      const map = this.make.tilemap({
        tileWidth: SHEET_TILE,
        tileHeight: SHEET_TILE,
        width: widthTiles,
        height: heightTiles,
      });
      // 16px tiles, 1px spacing, 0 margin → tilemap tile index == atlas frame.
      const tileset = map.addTilesetImage(
        "rogue",
        TILESET_KEY,
        SHEET_TILE,
        SHEET_TILE,
        0,
        1
      )!;
      const band = buffer === this.frontBuffer ? 0 : 1;
      const base = map.createBlankLayer("base", tileset, 0, 0)!;
      const overlay = map.createBlankLayer("overlay", tileset, 0, 0)!;
      base.setOrigin(0, 0);
      overlay.setOrigin(0, 0);
      base.setScale(ATLAS_SCALE);
      overlay.setScale(ATLAS_SCALE);
      base.setDepth(OverworldScene.BASE_DEPTH[band]);
      overlay.setDepth(OverworldScene.OVERLAY_DEPTH[band]);
      // New buffers are visible by default; swapBuffers manages front/back hiding.
      this.terrain[buffer] = {
        map,
        base,
        overlay,
        widthTiles,
        heightTiles,
        originX,
        originY,
      };
    }
    return this.terrain[buffer]!;
  }

  private worldPois(): RoutablePOI[] {
    return this.worldData?.pois?.length ? this.worldData.pois : (this.mapData?.pois ?? []);
  }

  private drawPois(g: Phaser.GameObjects.Graphics) {
    const pois = this.worldPois();
    for (const poi of pois) {
      if (poi.kind === "start") continue;
      const px = poi.x * TILE_SIZE;
      const py = poi.y * TILE_SIZE;
      if (poi.kind === "waypost") {
        this.drawWaypost(g, poi, px, py, pois);
        continue;
      }
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
   * Draw a signpost: a wooden post + board, plus an arrow pointing toward the
   * nearest town POI so the player can read which way the road leads. Direction
   * is computed client-side from the POI list — the server only marks position.
   */
  private drawWaypost(
    g: Phaser.GameObjects.Graphics,
    poi: RoutablePOI,
    px: number,
    py: number,
    pois: RoutablePOI[]
  ) {
    const cx = px + TILE_SIZE / 2;
    const cy = py + TILE_SIZE / 2;
    const board = 0xc9a25a;
    const wood = 0x6b4423;
    // post
    g.fillStyle(wood, 1);
    g.fillRect(cx - 1.5, cy - 4, 3, TILE_SIZE / 2);
    // sign board
    g.fillStyle(board, 1);
    g.fillRect(cx - 9, cy - 9, 18, 8);
    g.lineStyle(1, wood, 1);
    g.strokeRect(cx - 9, cy - 9, 18, 8);

    // Arrow on the board pointing toward the nearest town.
    const town = this.nearestPoiOfKind(poi, pois, "town");
    if (!town) return;
    const ang = Math.atan2(town.y - poi.y, town.x - poi.x);
    const bx = cx;
    const by = cy - 5;
    const len = 6;
    const tipX = bx + Math.cos(ang) * len;
    const tipY = by + Math.sin(ang) * len;
    g.lineStyle(1.5, 0x3a2410, 1);
    g.beginPath();
    g.moveTo(bx - Math.cos(ang) * len, by - Math.sin(ang) * len);
    g.lineTo(tipX, tipY);
    g.strokePath();
    // arrowhead
    const head = 3;
    g.fillStyle(0x3a2410, 1);
    g.fillTriangle(
      tipX,
      tipY,
      tipX - Math.cos(ang - 0.5) * head,
      tipY - Math.sin(ang - 0.5) * head,
      tipX - Math.cos(ang + 0.5) * head,
      tipY - Math.sin(ang + 0.5) * head
    );
  }

  /**
   * Redraw the blob-shadow layer: a soft dark ellipse under the player and every
   * live enemy/NPC, so actors read as standing ON the ground rather than floating.
   * Cleared + redrawn each frame (cheap — only a handful of actors on screen).
   */
  private drawShadows() {
    const g = this.shadowGfx;
    if (!g) return;
    g.clear();
    g.fillStyle(0x000000, 0.26);
    const foot = TILE_SIZE * 0.3;
    const ellipse = (x: number, y: number, w: number, h: number) =>
      g.fillEllipse(x, y + foot, w, h);
    if (this.playerSprite) ellipse(this.playerSprite.x, this.playerSprite.y, TILE_SIZE * 0.5, TILE_SIZE * 0.22);
    this.enemies.forEach((x, y) => ellipse(x, y, TILE_SIZE * 0.5, TILE_SIZE * 0.22));
    this.npcs.forEach((x, y) => ellipse(x, y, TILE_SIZE * 0.42, TILE_SIZE * 0.18));
  }

  /**
   * Shimmer the visible water: a gentle per-cell BLUE→white tint pulse over the
   * water frame so it reads as reflective/moving — never darkening (which looked
   * broken/glitchy) and never grayscale. `liveWaterCells` is in FRONT-buffer-local
   * tile coords, collected while the buffer was painted (see drawTerrainWindow).
   */
  private updateWaterShimmer(timeMs: number) {
    if (timeMs - this.lastShimmerMs < 90 || !this.liveWaterCells.length) return;
    this.lastShimmerMs = timeMs;
    const buf = this.terrain[this.frontBuffer];
    if (!buf) return;
    const t = timeMs / 700;
    for (const c of this.liveWaterCells) {
      const tile = buf.base.getTileAt(c.x, c.y);
      if (!tile) continue;
      // Phase-offset sine per cell → a travelling glint. Tint stays light:
      // R/G rise toward white, B pinned high → a soft blue→white shimmer.
      const s = 0.5 + 0.5 * Math.sin(t + (c.x + c.y) * 0.55);
      const r = 190 + Math.round(55 * s);
      const g = 215 + Math.round(35 * s);
      tile.tint = (r << 16) | (g << 8) | 0xff;
    }
  }

  /** Nearest POI of a given kind to `from` (squared tile distance); null if none. */
  private nearestPoiOfKind(
    from: RoutablePOI,
    pois: RoutablePOI[],
    kind: RoutablePOI["kind"]
  ): RoutablePOI | null {
    let best: RoutablePOI | null = null;
    let bestD = Infinity;
    for (const p of pois) {
      if (p.kind !== kind) continue;
      const d = (p.x - from.x) ** 2 + (p.y - from.y) ** 2;
      if (d < bestD) {
        bestD = d;
        best = p;
      }
    }
    return best;
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
    // Animate the day/night cycle every frame, even before the sim/map loads.
    this.postFX.update(time);

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

    // (Player + actor shadows are drawn by V3's drawShadows() below.)

    // Local terrain decorations — only redrawn when the player crosses a tile.
    const dtx = this.sim.tileX;
    const dty = this.sim.tileY;
    if (dtx !== this.lastDetailTile.x || dty !== this.lastDetailTile.y) {
      this.lastDetailTile = { x: dtx, y: dty };
      this.redrawLocalDetail(dtx, dty);
    }

    this.emitPlayerTile();
    this.npcs.update(time, delta, this.sim.x, this.sim.y, (tx, ty) =>
      this.sim!.isBlockedTile(tx, ty)
    );
    this.updateNearestSign();
    this.maybeTriggerNpcTalk(time);
    this.maybeEnterInterior();
    this.maybeRefreshChunk(time);

    // V3 atmosphere: ground actors with blob shadows + shimmer the water.
    this.drawShadows();
    this.updateWaterShimmer(time);

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

    // Re-centre ONLY when the player crosses into a new grid cell. The new 3×3
    // neighbourhood overlaps the old by 2/3 so the swap is seamless, and cached
    // neighbours make it instant. (Coverage-based re-centring fired far too often
    // with small chunks, causing visible swap/re-render churn.)
    const cellX = this.cellOf(tx);
    const cellY = this.cellOf(ty);
    if (cellX === this.activeCell.x && cellY === this.activeCell.y) return;
    this.activeCell = { x: cellX, y: cellY };

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
    // Fire only when the player crosses into a NEW grid cell — not every tile.
    // (Per-tile, player-relative prefetch re-requested the same neighbours
    // hundreds of times per second; grid cells are stable and cache cleanly.)
    const cellX = this.cellOf(tx);
    const cellY = this.cellOf(ty);
    if (cellX === this.lastPrefetchCell.x && cellY === this.lastPrefetchCell.y) return;
    this.lastPrefetchCell = { x: cellX, y: cellY };

    const half = Math.floor(CHUNK_SIZE / 2);
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        const cx = (cellX + dx) * CHUNK_SIZE + half;
        const cy = (cellY + dy) * CHUNK_SIZE + half;
        if (cx < 0 || cy < 0) continue;
        void this.prefetchChunk(cx, cy);
      }
    }
  }

  /** Ensure the anchor chunk's immediate 3x3 neighbourhood is cached. */
  private async warmChunkNeighborhood(anchor: MapState) {
    const originX = anchor.origin_x ?? 0;
    const originY = anchor.origin_y ?? 0;
    const cellX = this.cellOf(originX + Math.floor(anchor.width / 2));
    const cellY = this.cellOf(originY + Math.floor(anchor.height / 2));
    const half = Math.floor(CHUNK_SIZE / 2);
    const jobs: Array<Promise<void>> = [];
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (dx === 0 && dy === 0) continue;
        const cx = (cellX + dx) * CHUNK_SIZE + half;
        const cy = (cellY + dy) * CHUNK_SIZE + half;
        if (cx < 0 || cy < 0) continue;
        jobs.push(this.prefetchChunk(cx, cy, false));
      }
    }
    await Promise.all(jobs);
    this.lastPrefetchCell = { x: cellX, y: cellY };
  }

  /** Fetch one chunk into the prefetch cache, keyed by its returned origin. */
  private async prefetchChunk(centerX: number, centerY: number, refreshNeighborhood = true) {
    // Dedupe by center so two near-tiles don't double-fetch the same region.
    const reqKey = chunkKey(centerX, centerY);
    if (this.prefetchInFlight.has(reqKey)) return;
    // Skip if a cached chunk already covers this centre — this is the guard that
    // stops the runaway re-fetching of already-warm neighbours.
    if (this.chunkCovering(centerX, centerY)) return;
    this.prefetchInFlight.add(reqKey);
    try {
      const res = await fetch(this.mapUrl({ x: centerX, y: centerY }));
      if (!res.ok || !this.sys?.isActive()) return;
      const data = (await res.json()) as MapState;
      this.rememberChunk(data);
      // A newly-cached adjacent neighbour: extend collision (so the player can
      // walk into it) and, if it adds coverage, paint it via a seamless swap.
      // Grid-aligned origins mean tiles never shift — no churn, just fill-in.
      if (refreshNeighborhood && this.sim && this.mapData) {
        const win = this.windowOf(data);
        const adjacent = windowsWithinRenderHalo(
          this.windowOf(this.mapData),
          win,
          CHUNK_RENDER_HALO_TILES
        );
        if (adjacent && !this.windowCovered(win)) {
          this.refreshNeighborhood();
        } else if (adjacent) {
          this.syncSimTiles();
        }
      }
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
      // FOREST trees are rendered by the Wave-0 Kenney tilemap overlay (real
      // pixel-art trees), so the #24 drawn-tree detail is intentionally skipped.
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

  /**
   * Release render objects + caches. Called on BOTH Scene SHUTDOWN (scene.restart,
   * the encounter-return loop) and DESTROY, since Phaser does not auto-invoke a
   * Scene method named destroy(). Idempotent via the null-outs.
   */
  private cleanupRender() {
    this.enemies.destroy();
    this.npcs.destroy();
    this.destroySigns();
    this.detailGraphics?.destroy();
    this.playerLabel?.destroy();
    this.playerLabel = null;
    this.terrain[0]?.map.destroy();
    this.terrain[1]?.map.destroy();
    this.terrain = [null, null];
    this.prefetchedChunks.clear();
    this.prefetchInFlight.clear();
  }
}
