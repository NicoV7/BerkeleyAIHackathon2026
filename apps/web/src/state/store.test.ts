/**
 * store.test.ts — unit tests for the zustand game store (state/store.ts).
 *
 * Pins the screen-routing side effects baked into the setters:
 *   - setRun stores runId + topic + playerName AND routes to "overworld"
 *   - setEncounter(id) routes to "encounter"; setEncounter(null) -> "overworld"
 *   - setScreen is a plain screen transition with no other side effects
 *
 * Owned by T1 frontend unit wave. Uses the store directly via getState/setState
 * (no React render needed for pure store logic).
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useGame } from "./store";

// Snapshot the pristine initial state so each test starts clean (zustand stores
// are module singletons that persist mutations across tests).
const INITIAL = {
  runId: null,
  topic: "",
  playerName: "Player",
  screen: "menu" as const,
  activeEncounterId: null,
  lastYouScores: [],
};

beforeEach(() => {
  // Arrange (shared): reset to the documented initial shell state.
  useGame.setState({ ...INITIAL });
});

describe("useGame initial state", () => {
  it("starts on the menu screen with no run or encounter", () => {
    // Act
    const s = useGame.getState();

    // Assert
    expect(s.runId).toBeNull();
    expect(s.topic).toBe("");
    expect(s.playerName).toBe("Player");
    expect(s.screen).toBe("menu");
    expect(s.activeEncounterId).toBeNull();
  });
});

describe("useGame.setRun", () => {
  it("stores runId, topic, playerName and routes to the overworld", () => {
    // Act
    useGame.getState().setRun("run-123", "Climate Policy", "Ada");

    // Assert
    const s = useGame.getState();
    expect(s.runId).toBe("run-123");
    expect(s.topic).toBe("Climate Policy");
    expect(s.playerName).toBe("Ada");
    expect(s.screen).toBe("overworld");
  });

  it("defaults a blank playerName to Player", () => {
    // Act
    useGame.getState().setRun("run-123", "Climate Policy", "   ");

    // Assert
    expect(useGame.getState().playerName).toBe("Player");
  });
});

describe("useGame.setEncounter", () => {
  it("routes to the encounter screen when given an id", () => {
    // Act
    useGame.getState().setEncounter("enc-7");

    // Assert
    const s = useGame.getState();
    expect(s.activeEncounterId).toBe("enc-7");
    expect(s.screen).toBe("encounter");
  });

  it("clears the encounter and routes back to the overworld when given null", () => {
    // Arrange: an encounter is active.
    useGame.getState().setEncounter("enc-7");

    // Act
    useGame.getState().setEncounter(null);

    // Assert
    const s = useGame.getState();
    expect(s.activeEncounterId).toBeNull();
    expect(s.screen).toBe("overworld");
  });
});

describe("useGame.setScreen", () => {
  it("transitions the screen without touching run or encounter state", () => {
    // Arrange: an active run + encounter.
    useGame.getState().setRun("run-123", "Topic");
    useGame.getState().setEncounter("enc-7");

    // Act: a plain navigation to the party screen.
    useGame.getState().setScreen("party");

    // Assert: only screen changed; run/encounter preserved.
    const s = useGame.getState();
    expect(s.screen).toBe("party");
    expect(s.runId).toBe("run-123");
    expect(s.activeEncounterId).toBe("enc-7");
  });

  it("supports every declared Screen value", () => {
    // Arrange
    const screens = [
      "menu",
      "overworld",
      "encounter",
      "party",
      "training",
    ] as const;

    // Act + Assert: each transition lands on the requested screen.
    for (const target of screens) {
      useGame.getState().setScreen(target);
      expect(useGame.getState().screen).toBe(target);
    }
  });
});
