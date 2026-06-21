/**
 * InteriorScene — town / den / dungeon interiors (WS-3, Track B Wave 2).
 *
 * Reached via SceneRouter.enter(): step on an enterable POI (town/den) on the
 * overworld → the router fetches the interior WorldSpecLite → starts this scene
 * with { runId, interior, router }. We render the interior with the SAME atlas
 * RenderTexture pipeline as the overworld, spawn the interior's NPC anchors, drop
 * the player at the entrance DOOR, and return to the overworld when the player
 * steps back onto a DOOR (or presses Esc/Backspace) via router.exit().
 *
 * Interior TILES caveat: the /interior endpoint returns a WorldSpecLite WITHOUT a
 * tile grid (frozen schema), so we reconstruct a deterministic, navigable grid
 * client-side from the spec (see InteriorLayout). The POIs the server DOES return
 * (exit DOOR + feature/NPC anchors) are guaranteed to sit on carved floor.
 *
 * One scene class is registered under two keys (TownInteriorScene /
 * DungeonInteriorScene) so SceneRouter's INTERIOR_SCENE_FOR_KIND can pick by kind;
 * the visual palette is chosen from the interior kind at runtime.
 */

import Phaser from "phaser";
import { NPCBehaviorManager, type NPCAnchorView } from "./NPCBehavior";
import {
  createPlayerTextures,
  PlayerSpriteAnimator,
  playerTextureKey,
} from "./PlayerAnimator";
import { PostFX } from "./PostFX";
import type { InteriorSpec, NPCAnchor, SceneRouter } from "./SceneRouter";
import { INTERIOR_TILE } from "./SceneRouter";
import { FRAME, frameAt, SHEET_COLS, SHEET_ROWS } from "./TileAtlas";
import { WorldSim, type MoveIntent } from "./WorldSim";
import { TILE_SIZE } from "./constants";
import {
  buildInteriorGrid,
  normalizeKind,
  type InteriorGrid,
  type InteriorKind,
} from "./InteriorLayout";

const ATLAS_KEY = "rogue";
const SHEET_TILE = 16;
const ATLAS_SCALE = TILE_SIZE / SHEET_TILE;

/** Per-kind palette of atlas frames for interior tiles (floor / wall / accents). */
interface InteriorPalette {
  floor: number;
  wall: number;
  door: number;
  feature: number;
  /** Background colour shown under any gaps. */
  bg: string;
}

const PALETTES: Record<InteriorKind, InteriorPalette> = {
  // Town: warm sandstone plaza floor, light stone building walls.
  town: {
    floor: FRAME.TOWN_FLOOR,
    wall: FRAME.STRUCTURE,
    door: FRAME.ROAD,
    feature: FRAME.COBBLE,
    bg: "#241c12",
  },
  // Cave/den: dark charcoal floor, grey rock walls.
  cave: {
    floor: FRAME.CAVE,
    wall: FRAME.STONE,
    door: FRAME.ROAD,
    feature: FRAME.COBBLE,
    bg: "#0e0c10",
  },
  // Dungeon: same charcoal floor, structured stone walls.
  dungeon: {
    floor: FRAME.CAVE,
    wall: FRAME.STRUCTURE,
    door: FRAME.ROAD,
    feature: FRAME.COBBLE,
    bg: "#0e0c10",
  },
};

/** Procedural floor colour used by the no-atlas fallback (keeps it never-blank). */
const FALLBACK_FILL: Record<string, { floor: number; wall: number }> = {
  town: { floor: 0x7a6846, wall: 0x9a8c63 },
  cave: { floor: 0x2a2530, wall: 0x55585a },
  dungeon: { floor: 0x26222c, wall: 0x6a6470 },
};

export interface InteriorSceneData {
  runId: string;
  interior: InteriorSpec;
  router: SceneRouter;
  /** Resolved interior kind from the originating POI (town | cave | dungeon). */
  interiorKind?: string;
  /** Optional NPC-talk callback bubbled up like the overworld's onNpcTalk. */
  onNpcTalk?: (npc: NPCAnchorView) => void;
}

export class InteriorScene extends Phaser.Scene {
  private sceneData!: InteriorSceneData;
  private kind: InteriorKind = "cave";
  private grid!: InteriorGrid;

  private terrainRT: Phaser.GameObjects.RenderTexture | null = null;
  private fallbackG!: Phaser.GameObjects.Graphics;
  private atlasReady = false;

  private playerSprite!: Phaser.GameObjects.Sprite;
  private playerAnimator!: PlayerSpriteAnimator;
  private sim!: WorldSim;
  private npcs = new NPCBehaviorManager();
  private postFX = new PostFX();

  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys;
  private wasd!: Record<"up" | "down" | "left" | "right", Phaser.Input.Keyboard.Key>;
  private runKey!: Phaser.Input.Keyboard.Key;

  /** Latched once we begin exiting so movement/exit checks stop firing. */
  private exiting = false;
  /** Grace so the player isn't instantly bounced back out on the entrance DOOR. */
  private entryGraceMs = 600;
  private lastTalkNpcId: string | null = null;
  private lastTalkAtMs = 0;

  constructor(key: string) {
    super({ key });
  }

  init(data: InteriorSceneData) {
    this.sceneData = data;
    this.exiting = false;
    this.entryGraceMs = 600;
    this.lastTalkNpcId = null;
  }

  create() {
    this.kind = normalizeKind(this.sceneData.interiorKind ?? this.sceneKindHint());
    this.grid = buildInteriorGrid(this.sceneData.interior, this.kind);
    const palette = PALETTES[this.kind];
    this.cameras.main.setBackgroundColor(palette.bg);

    this.atlasReady = this.textures.exists(ATLAS_KEY);
    this.fallbackG = this.add.graphics();
    this.fallbackG.setDepth(-1);

    createPlayerTextures(this);
    this.ensureNpcTexture();

    // Render terrain.
    this.drawInterior();

    // Player at the entrance DOOR.
    const { entrance } = this.grid;
    this.sim = new WorldSim({
      tiles: this.grid.tiles,
      width: this.grid.width,
      height: this.grid.height,
      startTileX: entrance.x,
      startTileY: entrance.y,
    });
    this.playerSprite = this.textures.exists(playerTextureKey("down", 1))
      ? this.add.sprite(0, 0, playerTextureKey("down", 1))
      : this.add.sprite(0, 0, "player");
    this.playerSprite.setDisplaySize(TILE_SIZE, TILE_SIZE);
    this.playerSprite.setDepth(10);
    this.playerAnimator = new PlayerSpriteAnimator(this.playerSprite);
    this.sim.applyToSprite(this.playerSprite);
    this.sim.attachCamera(this.cameras.main, this.playerSprite);

    // NPC anchors (server-authored), placed on their carved floor pockets.
    this.spawnNpcs();

    // Input.
    this.cursors = this.input.keyboard!.createCursorKeys();
    this.wasd = {
      up: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.W),
      down: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.S),
      left: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.A),
      right: this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.D),
    };
    this.runKey = this.input.keyboard!.addKey(Phaser.Input.Keyboard.KeyCodes.SHIFT);
    // Back action: Esc / Backspace returns to the overworld immediately.
    this.input.keyboard?.on("keydown-ESC", () => this.returnToOverworld());
    this.input.keyboard?.on("keydown-BACKSPACE", () => this.returnToOverworld());

    this.postFX.attach(this);
    this.input.keyboard?.on("keydown-BACKTICK", () => this.postFX.toggle());

    // On-screen hint for the back action (a non-dead-end guarantee).
    this.add
      .text(12, 12, "Esc / step on the door to leave", {
        fontFamily: "monospace",
        fontSize: "11px",
        color: "#e8e6d8",
      })
      .setScrollFactor(0)
      .setDepth(1000);
  }

  /**
   * Bake the amber NPC marker if it isn't already in the (game-global) texture
   * manager. Normally OverworldScene bakes it first, but the interior must never
   * depend on that ordering — a missing texture would render a green Phaser box.
   */
  private ensureNpcTexture() {
    if (this.textures.exists("npc")) return;
    const g = this.make.graphics({ x: 0, y: 0 }, false);
    const px = (color: number, x: number, y: number, w = 1, h = 1) => {
      g.fillStyle(color, 1);
      g.fillRect(x, y, w, h);
    };
    px(0xffcf3f, 5, 3, 6, 4);
    px(0xc17f2a, 4, 7, 8, 7);
    px(0x0e1018, 6, 5);
    px(0x0e1018, 9, 5);
    px(0xe8e6d8, 5, 12, 6, 1);
    g.generateTexture("npc", 16, 16);
    g.destroy();
  }

  /** Best-effort kind from the registered scene key (TownInteriorScene -> town). */
  private sceneKindHint(): string {
    return this.scene.key.toLowerCase().includes("town") ? "town" : "cave";
  }

  private spawnNpcs() {
    this.npcs.destroy();
    const anchors = this.sceneData.router.npcAnchorsFromInterior(this.sceneData.interior);
    for (const anchor of anchors) {
      const p = this.clampAnchor(anchor);
      const spr = this.add.sprite(
        p.x * TILE_SIZE + TILE_SIZE / 2,
        p.y * TILE_SIZE + TILE_SIZE / 2,
        "npc"
      );
      spr.setDisplaySize(TILE_SIZE - 8, TILE_SIZE - 8);
      spr.setDepth(7);
      this.npcs.add({ ...anchor }, spr);
    }
  }

  /** Keep an anchor inside the room so an off-grid coord never spawns in a wall. */
  private clampAnchor(a: NPCAnchor): { x: number; y: number } {
    return {
      x: Math.max(1, Math.min(this.grid.width - 2, a.x)),
      y: Math.max(1, Math.min(this.grid.height - 2, a.y)),
    };
  }

  // ---- Rendering (same atlas RenderTexture pipeline as the overworld) ----

  private drawInterior() {
    const { width, height, tiles } = this.grid;
    const widthPx = width * TILE_SIZE;
    const heightPx = height * TILE_SIZE;
    const palette = PALETTES[this.kind];

    const rt = this.atlasReady ? this.ensureRT(widthPx, heightPx) : null;
    if (rt) rt.clear();
    this.fallbackG.clear();

    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const tile = tiles[y][x];
        const px = x * TILE_SIZE;
        const py = y * TILE_SIZE;
        if (rt) {
          this.stampInteriorTile(rt, palette, tile, px, py);
        } else {
          this.drawFallbackTile(tile, px, py);
        }
      }
    }
  }

  private stampInteriorTile(
    rt: Phaser.GameObjects.RenderTexture,
    palette: InteriorPalette,
    tile: number,
    px: number,
    py: number
  ) {
    // Floor base under everything so walls/doors read as sitting on a floor.
    this.stamp(rt, palette.floor, px, py);
    if (tile === INTERIOR_TILE.WALL) {
      this.stamp(rt, palette.wall, px, py);
    } else if (tile === INTERIOR_TILE.DOOR) {
      this.stamp(rt, palette.door, px, py);
      // A bright marker so the exit is obvious.
      this.stamp(rt, frameAt(4, 8), px, py, 0.9, 0xffcf3f);
    } else if (tile === INTERIOR_TILE.FEATURE) {
      this.stamp(rt, palette.feature, px, py);
    }
  }

  private stamp(
    rt: Phaser.GameObjects.RenderTexture,
    frame: number,
    px: number,
    py: number,
    alpha = 1,
    tint?: number
  ) {
    // Guard against an out-of-range frame (defensive — palette uses known frames).
    if (frame < 0 || frame >= SHEET_COLS * SHEET_ROWS) return;
    rt.stamp(ATLAS_KEY, frame, px, py, {
      originX: 0,
      originY: 0,
      scale: ATLAS_SCALE,
      alpha,
      ...(tint !== undefined ? { tint } : {}),
    });
  }

  private drawFallbackTile(tile: number, px: number, py: number) {
    const g = this.fallbackG;
    const fill = FALLBACK_FILL[this.kind] ?? FALLBACK_FILL.cave;
    g.fillStyle(tile === INTERIOR_TILE.WALL ? fill.wall : fill.floor, 1);
    g.fillRect(px, py, TILE_SIZE, TILE_SIZE);
    if (tile === INTERIOR_TILE.DOOR) {
      g.fillStyle(0xffcf3f, 0.8);
      g.fillRect(px + 8, py + 8, TILE_SIZE - 16, TILE_SIZE - 16);
    } else if (tile === INTERIOR_TILE.FEATURE) {
      g.fillStyle(0x5cc8ff, 0.5);
      g.fillRect(px + 10, py + 10, TILE_SIZE - 20, TILE_SIZE - 20);
    }
  }

  private ensureRT(widthPx: number, heightPx: number): Phaser.GameObjects.RenderTexture {
    if (!this.terrainRT) {
      this.terrainRT = this.add.renderTexture(0, 0, widthPx, heightPx);
      this.terrainRT.setOrigin(0, 0);
      this.terrainRT.setDepth(-2);
    }
    return this.terrainRT;
  }

  // ---- Update loop ----

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
    if (this.exiting || !this.sim) return;
    if (this.entryGraceMs > 0) this.entryGraceMs -= delta;

    const intent = this.readIntent();
    this.sim.update(intent, delta);
    this.sim.applyToSprite(this.playerSprite);
    this.playerAnimator.update({
      intent,
      velocity: { vx: this.sim.vx, vy: this.sim.vy },
      deltaMs: delta,
    });
    this.npcs.update(time, delta, this.sim.x, this.sim.y, (tx, ty) =>
      this.sim!.isBlockedTile(tx, ty)
    );
    this.maybeTriggerNpcTalk(time);

    // Step on a DOOR (after the entry grace) → return to the overworld.
    if (this.entryGraceMs <= 0 && this.onExitTile()) {
      this.returnToOverworld();
    }
  }

  private onExitTile(): boolean {
    const tx = this.sim.tileX;
    const ty = this.sim.tileY;
    return this.grid.exits.some((e) => e.x === tx && e.y === ty);
  }

  private maybeTriggerNpcTalk(time: number) {
    const onTalk = this.sceneData.onNpcTalk;
    if (!onTalk) return;
    const anchor = this.npcs.nearest(this.sim.x, this.sim.y);
    if (!anchor) {
      this.lastTalkNpcId = null;
      return;
    }
    if (this.lastTalkNpcId === anchor.npc_id && time - this.lastTalkAtMs < 1500) return;
    this.lastTalkNpcId = anchor.npc_id;
    this.lastTalkAtMs = time;
    onTalk(anchor);
  }

  private returnToOverworld() {
    if (this.exiting) return;
    this.exiting = true;
    this.npcs.destroy();
    this.sceneData.router.exit();
  }

  destroy() {
    this.npcs.destroy();
    this.terrainRT?.destroy();
    this.terrainRT = null;
  }
}

/** Scene keys registered for the two interior flavours (see SceneRouter map). */
export const TOWN_INTERIOR_KEY = "TownInteriorScene";
export const DUNGEON_INTERIOR_KEY = "DungeonInteriorScene";
