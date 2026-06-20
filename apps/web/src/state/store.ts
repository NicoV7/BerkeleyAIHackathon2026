import { create } from "zustand";

// Minimal global game store. Workstreams extend their own slices; keep this
// shell stable (runId, screen routing, active encounter).
export type Screen = "menu" | "overworld" | "encounter" | "party" | "training" | "demo";

interface GameState {
  runId: string | null;
  topic: string;
  screen: Screen;
  activeEncounterId: string | null;
  setRun: (runId: string, topic: string) => void;
  setScreen: (screen: Screen) => void;
  setEncounter: (id: string | null) => void;
}

export const useGame = create<GameState>((set) => ({
  runId: null,
  topic: "",
  screen: "menu",
  activeEncounterId: null,
  setRun: (runId, topic) => set({ runId, topic, screen: "overworld" }),
  setScreen: (screen) => set({ screen }),
  setEncounter: (activeEncounterId) =>
    set({ activeEncounterId, screen: activeEncounterId ? "encounter" : "overworld" }),
}));
