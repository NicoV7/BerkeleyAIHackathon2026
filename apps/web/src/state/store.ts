import { create } from "zustand";

export const DEFAULT_PLAYER_NAME = "Player";
export const PLAYER_NAME_STORAGE_KEY = "debate-rpg.playerName";

export function normalizePlayerName(value: string | null | undefined): string {
  const trimmed = value?.trim() ?? "";
  return trimmed || DEFAULT_PLAYER_NAME;
}

function readStoredPlayerName(): string {
  if (typeof window === "undefined") return DEFAULT_PLAYER_NAME;
  try {
    return normalizePlayerName(window.localStorage.getItem(PLAYER_NAME_STORAGE_KEY));
  } catch {
    return DEFAULT_PLAYER_NAME;
  }
}

function rememberPlayerName(name: string) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PLAYER_NAME_STORAGE_KEY, name);
  } catch {
    /* localStorage is a convenience mirror; gameplay should not depend on it. */
  }
}

// Minimal global game store. Workstreams extend their own slices; keep this
// shell stable (runId, screen routing, active encounter).
//
// WS-6 tab restructure: `training` is no longer a top-tab screen (it moved into
// Camp). `encounter` is reachable ONLY via setEncounter (battle-only), and is
// kept in the union so the encounter screen still renders when a battle is live.
export type Screen = "menu" | "overworld" | "encounter" | "party" | "demo";

/**
 * Overlay surfaces opened from the Adventure menu (HUD button) ON TOP of the
 * current screen, without changing `screen`. `null` = no overlay.
 *
 * WS-0-UI owns this contract; Wave-2 owners implement the bodies:
 *   - "inventory" → WS-2 inventory screen
 *   - "quests"    → WS-2 quest log
 *   - "map"       → WS-2 / world map (full map view; HUD minimap is separate)
 *
 * Diegetic surfaces (Camp, Shop) are entered via world POIs, NOT this menu, so
 * they are intentionally NOT in this union — they have their own store fields
 * (`atCamp`, `shopNpcId`) and render in OverlayHost. See UI_CONTRACT.md §Nav model.
 */
export type Overlay = "inventory" | "quests" | "map" | null;

interface GameState {
  runId: string | null;
  topic: string;
  /** Theme picked at run start; each battle draws a random topic within it. */
  theme: string;
  playerName: string;
  screen: Screen;
  activeEncounterId: string | null;
  /** Player's per-round reasoning scores from the latest battle (for the dual
   *  "human beside machine" trend on the training screen). */
  lastYouScores: number[];
  /** True while the player is inside an ACTIVE (not-yet-resolved) battle. While
   *  set, the global nav is locked so the only way out is Flee / win / lose.
   *  BattleDebateView owns this flag (sets on mount, clears on over/flee). */
  battleLocked: boolean;
  /** Adventure-menu overlay surface drawn on top of the current screen, or null.
   *  Owned by the HUD Adventure menu; surface bodies are Wave-2. */
  overlay: Overlay;
  /** Diegetic Camp surface (WS-3). True while the player is camped (entered by
   *  talking to an innkeeper / walking into a camp POI). Renders in OverlayHost. */
  atCamp: boolean;
  /** Diegetic Shop surface (WS-3). The NPC id whose shop is open, or null.
   *  Entered by talking to a merchant / walking into a shop POI. */
  shopNpcId: string | null;
  setRun: (
    runId: string,
    topic: string,
    playerName?: string | null,
    theme?: string | null
  ) => void;
  setScreen: (screen: Screen) => void;
  setEncounter: (id: string | null) => void;
  setYouScores: (scores: number[]) => void;
  setBattleLocked: (locked: boolean) => void;
  /** Open an Adventure-menu overlay surface (inventory/quests/map). */
  openOverlay: (overlay: NonNullable<Overlay>) => void;
  /** Close any open overlay surface (universal back/close). */
  closeOverlay: () => void;
  /** Enter the diegetic Camp surface (closes any menu overlay first). */
  openCamp: () => void;
  /** Leave the Camp surface. */
  closeCamp: () => void;
  /** Enter a diegetic Shop for an NPC (closes any menu overlay first). */
  openShop: (npcId: string) => void;
  /** Leave the Shop surface. */
  closeShop: () => void;
}

export const useGame = create<GameState>((set) => ({
  runId: null,
  topic: "",
  theme: "",
  playerName: readStoredPlayerName(),
  screen: "menu",
  activeEncounterId: null,
  lastYouScores: [],
  battleLocked: false,
  overlay: null,
  atCamp: false,
  shopNpcId: null,
  setRun: (runId, topic, playerName, theme = "") => {
    const name = normalizePlayerName(playerName ?? readStoredPlayerName());
    rememberPlayerName(name);
    set({
      runId,
      topic,
      playerName: name,
      theme: theme ?? "",
      screen: "overworld",
      overlay: null,
      // A fresh/loaded run never starts inside a diegetic surface.
      atCamp: false,
      shopNpcId: null,
    });
  },
  setScreen: (screen) => set({ screen }),
  setEncounter: (activeEncounterId) =>
    set({
      activeEncounterId,
      screen: activeEncounterId ? "encounter" : "overworld",
      // Leaving a battle (flee / clear) always releases the nav lock.
      battleLocked: Boolean(activeEncounterId),
      // A battle takes over the screen — never leave an overlay/diegetic surface
      // floating into it.
      overlay: null,
      atCamp: false,
      shopNpcId: null,
    }),
  setYouScores: (lastYouScores) => set({ lastYouScores }),
  setBattleLocked: (battleLocked) => set({ battleLocked }),
  // Adventure-menu overlays and diegetic surfaces are mutually exclusive: opening
  // one closes the other so only a single surface ever floats over the overworld.
  openOverlay: (overlay) => set({ overlay, atCamp: false, shopNpcId: null }),
  closeOverlay: () => set({ overlay: null }),
  openCamp: () => set({ atCamp: true, shopNpcId: null, overlay: null }),
  closeCamp: () => set({ atCamp: false }),
  openShop: (shopNpcId) => set({ shopNpcId, atCamp: false, overlay: null }),
  closeShop: () => set({ shopNpcId: null }),
}));
