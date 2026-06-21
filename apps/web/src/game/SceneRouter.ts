/**
 * SceneRouter — overworld <-> interior scene transitions (Track B, Wave 2).
 *
 * Step on an enterable POI (town/den) → fetch/generate its interior
 * WorldSpecLite from the server → start an interior scene, remembering the
 * overworld return tile → walk to the interior exit → return to the overworld at
 * the POI tile you entered from.
 *
 * Interior fetch:
 *   GET /api/runs/{runId}/interior/{poiId}  (poiId = `${kind}:${x}:${y}`)
 * The server generates the interior on demand (procedural algorithms package)
 * and caches it by (interior_seed, kind), so re-entering the same POI returns the
 * identical layout. The endpoint never raises — it always returns a renderable
 * WorldSpecLite — so enter() can rely on a usable response or a network error.
 *
 * Scene registration:
 *   The actual interior Phaser scenes (TownInteriorScene / DungeonInteriorScene)
 *   are the teammate's Wave-3 deliverable. This router is decoupled from them via
 *   INTERIOR_SCENE_FOR_KIND + an injectable onEnterInterior callback, so it works
 *   the moment a scene is registered and no-ops cleanly until then.
 */

import type Phaser from "phaser";

/** Tile legend used by interiors (mirrors apps/api .../algorithms/base.py). */
export const INTERIOR_TILE = {
  FLOOR: 0,
  WALL: 1,
  DOOR: 3, // exit/entrance marker (walkable)
  FEATURE: 4, // NPC/chest/altar anchor (walkable)
} as const;

/** Minimal POI shape the router needs (subset of the server POI schema). */
export interface RoutablePOI {
  kind: "camp" | "town" | "den" | "landmark" | "start" | "goal" | "waypost";
  x: number; // overworld tile coords — also the return point
  y: number;
  name?: string;
  /** Wave 2 additive fields: present only on enterable POIs (town/den). */
  interior_seed?: number | null;
  interior_kind?: "town" | "cave" | "dungeon" | null;
  npc_anchors?: NPCAnchor[];
  scripted?: boolean;
}

export interface NPCAnchor {
  npc_id: string;
  archetype: "villager" | "merchant" | "quest_giver" | "figure" | "innkeeper";
  x: number;
  y: number;
  name?: string;
  figure_id?: string | null;
}

export interface RegionSpec {
  name: string;
  biome: string;
  bounds?: number[] | null;
  lore?: string | null;
}

/** Shape returned by GET /api/runs/{id}/interior/{poi_id} (WorldSpecLite). */
export interface InteriorSpec {
  seed: number;
  width: number;
  height: number;
  regions: RegionSpec[];
  pois: RoutablePOI[];
  start?: RoutablePOI | null;
  goal?: RoutablePOI | null;
}

export interface SceneRouterConfig {
  runId: string;
  /** The Phaser scene manager (game.scene), used to start/stop scenes. */
  scenePlugin: Phaser.Scenes.ScenePlugin;
  /**
   * Optional hook invoked with the fetched interior just before the interior
   * scene starts. Lets the overworld/teammate observe or customise the handoff
   * (e.g. analytics, a loading beat) without subclassing the router.
   */
  onEnterInterior?: (poi: RoutablePOI, interior: InteriorSpec) => void;
  /** Optional hook invoked when returning to the overworld at `returnTile`. */
  onExitInterior?: (returnTile: { x: number; y: number }) => void;
  /**
   * Optional NPC-talk callback forwarded to the interior scene so interior NPCs
   * surface the same React dialogue as the overworld. Shape matches
   * OverworldConfig.onNpcTalk (an NPCAnchor-like view).
   */
  onNpcTalk?: (npc: NPCAnchor) => void;
  /** Optional battle handoff forwarded to dungeon interiors. */
  onEncounter?: (wildId?: string | null) => void;
}

/**
 * Which Phaser scene a POI kind opens into. Keyed by the POI's interior_kind
 * hint (preferred) with a fallback by POI.kind.
 *
 * WS-3: the interior scenes (TownInteriorScene / DungeonInteriorScene — both the
 * single InteriorScene class registered under two keys) are added to the Phaser
 * game by OverworldScene at create() time, so these keys resolve at runtime.
 * `town` POIs open the town interior; `den`/`cave`/`dungeon` open the dungeon
 * interior. If a key is somehow unregistered, enter() still no-ops gracefully.
 */
export const INTERIOR_SCENE_FOR_KIND: Record<string, string> = {
  // by interior_kind hint:
  town: "TownInteriorScene",
  cave: "DungeonInteriorScene",
  dungeon: "DungeonInteriorScene",
  // by POI.kind fallback (when interior_kind is absent):
  den: "DungeonInteriorScene",
};

/** Overworld scene key we return to on exit. */
const OVERWORLD_SCENE_KEY = "OverworldScene";

export class SceneRouter {
  private cfg: SceneRouterConfig;
  /** Where to drop the player when they exit back to the overworld. */
  private returnTile: { x: number; y: number } | null = null;
  /** Guards against double-enter while an interior fetch is in flight. */
  private entering = false;

  constructor(cfg: SceneRouterConfig) {
    this.cfg = cfg;
  }

  /** A POI is enterable iff the server marked it with interior fields. */
  isEnterable(poi: RoutablePOI): boolean {
    return poi.interior_seed != null;
  }

  /** Stable positional id matching the server's `poi_id()` (`kind:x:y`). */
  private poiId(poi: RoutablePOI): string {
    return `${poi.kind}:${poi.x}:${poi.y}`;
  }

  /** Resolve the interior scene key for a POI (interior_kind hint, then kind). */
  private sceneKeyFor(poi: RoutablePOI): string | undefined {
    if (poi.interior_kind && INTERIOR_SCENE_FOR_KIND[poi.interior_kind]) {
      return INTERIOR_SCENE_FOR_KIND[poi.interior_kind];
    }
    return INTERIOR_SCENE_FOR_KIND[poi.kind];
  }

  /**
   * Enter a POI's interior: fetch the interior WorldSpecLite, then start the
   * interior scene (if one is registered) seeded with that spec + this router so
   * the interior can call exit(). Remembers the POI tile as the return point.
   *
   * Resilient: a non-enterable POI or a missing interior scene no-ops (the
   * overworld keeps running). A network error is logged and also no-ops, so
   * stepping on a POI can never soft-lock the game.
   */
  async enter(poi: RoutablePOI): Promise<void> {
    if (this.entering) return;
    if (!this.isEnterable(poi)) return;

    const sceneKey = this.sceneKeyFor(poi);
    // No interior scene registered yet (pre-Wave-3): remember intent + bail.
    if (!sceneKey) return;

    this.entering = true;
    this.returnTile = { x: poi.x, y: poi.y };
    try {
      const interior = await this.fetchInterior(poi);
      if (!interior) return; // fetch failed — stay in the overworld.
      this.cfg.onEnterInterior?.(poi, interior);
      this.cfg.scenePlugin.start(sceneKey, {
        runId: this.cfg.runId,
        interior,
        router: this,
        // Pass the POI's resolved interior kind so the scene picks the right
        // palette/generator even though the interior `start` POI doesn't carry it.
        interiorKind: poi.interior_kind ?? (poi.kind === "town" ? "town" : "cave"),
        onNpcTalk: this.cfg.onNpcTalk,
        onEncounter: this.cfg.onEncounter,
      });
    } catch (e) {
      console.error("SceneRouter.enter failed:", e);
      this.returnTile = null;
    } finally {
      this.entering = false;
    }
  }

  /** Fetch the interior spec for a POI; returns null on any failure. */
  private async fetchInterior(poi: RoutablePOI): Promise<InteriorSpec | null> {
    try {
      const res = await fetch(
        `/api/runs/${this.cfg.runId}/interior/${encodeURIComponent(
          this.poiId(poi)
        )}`
      );
      if (!res.ok) return null;
      return (await res.json()) as InteriorSpec;
    } catch (e) {
      console.error("SceneRouter.fetchInterior error:", e);
      return null;
    }
  }

  npcAnchorsFromInterior(interior: InteriorSpec): NPCAnchor[] {
    return interior.pois.flatMap((poi) => poi.npc_anchors ?? []);
  }

  /**
   * Exit the current interior back to the overworld at the tile we entered from,
   * restarting OverworldScene seeded with the return tile so the player reappears
   * on the POI. No-ops if we never entered an interior.
   */
  exit(): void {
    if (!this.returnTile) return;
    const tile = this.returnTile;
    this.returnTile = null;
    this.cfg.onExitInterior?.(tile);
    this.cfg.scenePlugin.start(OVERWORLD_SCENE_KEY, {
      runId: this.cfg.runId,
      returnTile: tile,
    });
  }
}
