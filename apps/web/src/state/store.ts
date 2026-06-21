import { create } from "zustand";

// Minimal global game store. Workstreams extend their own slices; keep this
// shell stable (runId, screen routing, active encounter).
export type Screen = "menu" | "overworld" | "encounter" | "party" | "training" | "demo";

interface GameState {
  runId: string | null;
  topic: string;
  screen: Screen;
  activeEncounterId: string | null;
  /** Player's per-round reasoning scores from the latest battle (for the dual
   *  "human beside machine" trend on the training screen). */
  lastYouScores: number[];
  /** True while the player is inside an ACTIVE (not-yet-resolved) battle. While
   *  set, the global nav is locked so the only way out is Flee / win / lose.
   *  BattleDebateView owns this flag (sets on mount, clears on over/flee). */
  battleLocked: boolean;
  setRun: (runId: string, topic: string) => void;
  setScreen: (screen: Screen) => void;
  setEncounter: (id: string | null) => void;
  setYouScores: (scores: number[]) => void;
  setBattleLocked: (locked: boolean) => void;
}

export const useGame = create<GameState>((set) => ({
  runId: null,
  topic: "",
  screen: "menu",
  activeEncounterId: null,
  lastYouScores: [],
  battleLocked: false,
  setRun: (runId, topic) => set({ runId, topic, screen: "overworld" }),
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
