/**
 * EncounterTrigger — thin bridge between OverworldScene collision events
 * and the React/Zustand encounter flow.
 *
 * When the player walks onto an enemy tile, OverworldScene calls
 * cfg.onEncounter(wildId).  The React wrapper (Overworld.tsx) passes a
 * handler here that calls useGame().setEncounter(encounterId) after
 * optionally creating the encounter via POST /api/encounters (WS-B).
 */

export interface EncounterBridge {
  /**
   * Called by OverworldScene when the player collides with a wild monster.
   * `wildId` is the Monster.id of the wild enemy. Interior scenes may pass
   * null to request a fresh random encounter because their enemies are generated
   * client-side decorations rather than rows from the run's wild Monster table.
   * Returns an encounter id (from WS-B) or null if creation fails.
   */
  onCollision: (wildId?: string | null) => Promise<string | null>;
}

/**
 * Build an EncounterBridge that:
 * 1. Posts to /api/encounters to create a real battle (WS-B).
 * 2. Falls back to returning the wildId directly if WS-B is absent.
 */
export function buildEncounterBridge(
  runId: string,
  setEncounter: (id: string | null) => void
): EncounterBridge {
  return {
    onCollision: async (wildId?: string | null): Promise<string | null> => {
      try {
        const body =
          wildId === null || wildId === undefined
            ? { run_id: runId }
            : { run_id: runId, wild_id: wildId };
        const res = await fetch("/api/encounters", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res.ok) {
          const data = (await res.json()) as { id: string };
          setEncounter(data.id);
          return data.id;
        }
      } catch {
        // WS-B not ready yet — use wildId as a placeholder encounter id
      }
      // Fallback: use the wild monster id as the encounter ref so
      // the frontend can still navigate to the encounter screen.
      const fallbackId = wildId ?? `encounter:${Date.now()}`;
      setEncounter(fallbackId);
      return fallbackId;
    },
  };
}
