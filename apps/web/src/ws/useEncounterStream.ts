/**
 * useEncounterStream — WebSocket hook for live encounter events.
 *
 * Connects to WS /api/encounters/{encounterId}/stream and emits typed events.
 * The server (WS-B) pushes JSON messages; we parse them and accumulate state.
 *
 * Message shape from server (at minimum):
 *   { type: "utterance",  data: Utterance }
 *   { type: "verdict",    data: JudgeVerdict }
 *   { type: "state",      data: EncounterState }
 *   { type: "error",      message: string }
 *
 * The hook re-connects automatically when encounterId changes.
 * Returns null when encounterId is null (no active encounter).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../api/client";

// ---------------------------------------------------------------------------
// Types (mirroring schemas.py — generated types may not be available yet)
// ---------------------------------------------------------------------------

export interface Utterance {
  turn: number;
  actor_id: string;
  actor_role: "party" | "enemy" | "judge";
  skill_used?: string | null;
  text: string;
  ts: number;
}

export interface JudgeVerdict {
  turn: number;
  target: string;
  score: number;
  rationale: string;
  damage: number;
}

export interface CombatantState {
  monster_id: string;
  name: string;
  type: string;
  role: "party" | "enemy";
  hp: number;
  max_hp: number;
}

export interface EncounterState {
  id: string;
  run_id: string;
  topic: string;
  phase: "intro" | "debating" | "capturable" | "won" | "lost";
  turn_no: number;
  combatants: CombatantState[];
  transcript: Utterance[];
  verdicts: JudgeVerdict[];
}

export type ConnectionStatus = "connecting" | "open" | "closed" | "error";

export interface EncounterStreamState {
  status: ConnectionStatus;
  encounter: EncounterState | null;
  transcript: Utterance[];
  verdicts: JudgeVerdict[];
  /** Wild monster ids currently capturable (from the latest phase event). */
  capturableIds: string[];
  /** Drive N autonomous rounds (Auto / Next Round). */
  drive: (rounds: number) => void;
  /** Submit a human-typed argument as the player's turn. */
  argue: (text: string, skillId?: string | null) => void;
  /** Close and clean up the websocket connection manually */
  disconnect: () => void;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

const RECONNECT_DELAY_MS = 2000;

export function useEncounterStream(encounterId: string | null): EncounterStreamState {
  const [status, setStatus] = useState<ConnectionStatus>("closed");
  const [encounter, setEncounter] = useState<EncounterState | null>(null);
  const [transcript, setTranscript] = useState<Utterance[]>([]);
  const [verdicts, setVerdicts] = useState<JudgeVerdict[]>([]);
  const [capturableIds, setCapturableIds] = useState<string[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountedRef = useRef(false);
  // Commands queued while the socket is still connecting (drained on onopen).
  const pendingQueue = useRef<Record<string, unknown>[]>([]);

  /** Send a command now if open, else queue it until onopen. */
  const send = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    } else {
      pendingQueue.current.push(payload);
    }
  }, []);

  const drive = useCallback(
    (rounds: number) => send({ rounds }),
    [send]
  );
  const argue = useCallback(
    (text: string, skillId?: string | null) =>
      send({ action: "argue", text, skill_id: skillId ?? null }),
    [send]
  );

  const disconnect = useCallback(() => {
    if (reconnectTimer.current != null) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    pendingQueue.current = [];
    if (wsRef.current) {
      wsRef.current.onclose = null; // prevent reconnect loop
      wsRef.current.close();
      wsRef.current = null;
    }
    setStatus("closed");
  }, []);

  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);

  useEffect(() => {
    if (!encounterId) {
      disconnect();
      setEncounter(null);
      setTranscript([]);
      setVerdicts([]);
      setCapturableIds([]);
      return;
    }

    // Reset state for new encounter
    pendingQueue.current = [];
    setTranscript([]);
    setVerdicts([]);
    setCapturableIds([]);

    function connect() {
      if (unmountedRef.current) return;

      const url = wsUrl(`/api/encounters/${encounterId}/stream`);
      const ws = new WebSocket(url);
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        if (unmountedRef.current) { ws.close(); return; }
        setStatus("open");
        // Drain all commands queued before the socket opened.
        const queued = pendingQueue.current.splice(0);
        for (const payload of queued) ws.send(JSON.stringify(payload));
      };

      ws.onmessage = (evt: MessageEvent) => {
        if (unmountedRef.current) return;
        try {
          const msg = JSON.parse(evt.data as string) as {
            type: string;
            data?: unknown;
            message?: string;
          };

          if (msg.type === "utterance") {
            const u = msg.data as Utterance;
            setTranscript((prev) => {
              // de-dupe by (turn, actor_id, ts)
              const exists = prev.some(
                (x) => x.turn === u.turn && x.actor_id === u.actor_id && x.ts === u.ts
              );
              return exists ? prev : [...prev, u];
            });
          } else if (msg.type === "verdict") {
            const v = msg.data as JudgeVerdict;
            setVerdicts((prev) => {
              const exists = prev.some(
                (x) => x.turn === v.turn && x.target === v.target
              );
              return exists ? prev : [...prev, v];
            });
          } else if (msg.type === "hp") {
            // payload: { [monster_id]: hp } — merge into combatants immutably.
            const hpMap = msg.data as Record<string, number>;
            setEncounter((prev) =>
              prev
                ? {
                    ...prev,
                    combatants: prev.combatants.map((c) =>
                      c.monster_id in hpMap ? { ...c, hp: hpMap[c.monster_id] } : c
                    ),
                  }
                : prev
            );
          } else if (msg.type === "phase") {
            // payload: { phase, capturable_ids, turn_no }
            const p = msg.data as {
              phase: EncounterState["phase"];
              capturable_ids?: string[];
              turn_no?: number;
            };
            setEncounter((prev) =>
              prev
                ? { ...prev, phase: p.phase, turn_no: p.turn_no ?? prev.turn_no }
                : prev
            );
            setCapturableIds(p.capturable_ids ?? []);
          } else if (msg.type === "state") {
            const s = msg.data as EncounterState;
            setEncounter(s);
            // Hydrate transcript + verdicts from full state snapshot
            if (s.transcript?.length) setTranscript(s.transcript);
            if (s.verdicts?.length) setVerdicts(s.verdicts);
          } else if (msg.type === "error") {
            console.error("[useEncounterStream] server error:", msg.message);
          }
        } catch (e) {
          console.error("[useEncounterStream] parse error", e);
        }
      };

      ws.onerror = () => {
        if (unmountedRef.current) return;
        setStatus("error");
      };

      ws.onclose = () => {
        if (unmountedRef.current) return;
        setStatus("closed");
        // Auto-reconnect unless the encounter is finished
        if (encounterId && !unmountedRef.current) {
          reconnectTimer.current = setTimeout(() => {
            if (!unmountedRef.current) connect();
          }, RECONNECT_DELAY_MS);
        }
      };
    }

    connect();

    return () => {
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [encounterId]);

  return { status, encounter, transcript, verdicts, capturableIds, drive, argue, disconnect };
}
