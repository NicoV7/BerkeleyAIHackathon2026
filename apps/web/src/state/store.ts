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
  playerName: string;
  screen: Screen;
  activeEncounterId: string | null;
  /** Player's per-round reasoning scores from the latest battle (for the dual
   *  "human beside machine" trend on the training screen). */
  lastYouScores: number[];
  setRun: (runId: string, topic: string, playerName?: string | null) => void;
  setScreen: (screen: Screen) => void;
  setEncounter: (id: string | null) => void;
  setYouScores: (scores: number[]) => void;
}

export const useGame = create<GameState>((set) => ({
  runId: null,
  topic: "",
  playerName: readStoredPlayerName(),
  screen: "menu",
  activeEncounterId: null,
  lastYouScores: [],
  setRun: (runId, topic, playerName) => {
    const name = normalizePlayerName(playerName ?? readStoredPlayerName());
    rememberPlayerName(name);
    set({ runId, topic, playerName: name, screen: "overworld" });
  },
  setScreen: (screen) => set({ screen }),
  setEncounter: (activeEncounterId) =>
    set({ activeEncounterId, screen: activeEncounterId ? "encounter" : "overworld" }),
  setYouScores: (lastYouScores) => set({ lastYouScores }),
}));
