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
export type Screen = "menu" | "overworld" | "encounter" | "party" | "training" | "demo";

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
  setRun: (runId, topic, playerName, theme = "") => {
    const name = normalizePlayerName(playerName ?? readStoredPlayerName());
    rememberPlayerName(name);
    set({ runId, topic, playerName: name, theme: theme ?? "", screen: "overworld" });
  },
  setScreen: (screen) => set({ screen }),
  setEncounter: (activeEncounterId) =>
    set({
      activeEncounterId,
      screen: activeEncounterId ? "encounter" : "overworld",
      // Leaving a battle (flee / clear) always releases the nav lock.
      battleLocked: Boolean(activeEncounterId),
    }),
  setYouScores: (lastYouScores) => set({ lastYouScores }),
  setBattleLocked: (battleLocked) => set({ battleLocked }),
}));
