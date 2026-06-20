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
 *   { type: "hp",         data: HpUpdate }       // live per-combatant HP
 *   { type: "phase",      data: PhaseUpdate }    // phase transitions
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
  /** Unsigned 0-100 (50 = average). Compare against 50 for win/positive logic. */
  score: number;
  rationale: string;
  damage: number;
  // --- additive fields from WS-B verdict event (all optional for back-compat) ---
  /** One-line explanation of WHY the argument won/landed. Hero banner copy. */
  why?: string;
  /** Logic sub-score, unsigned 0-100. */
  logic?: number;
  /** Persuasion sub-score, unsigned 0-100. */
  persuasion?: number;
  /** Combatant id this verdict is attributed to (the speaker being judged). */
  actor_id?: string;
}

/** Live HP update for a single combatant ({ type: "hp", data: HpUpdate }). */
export interface HpUpdate {
  monster_id: string;
  hp: number;
  max_hp?: number;
}

/** Phase transition ({ type: "phase", data: PhaseUpdate }). */
export interface PhaseUpdate {
  phase: EncounterPhase;
}

export type EncounterPhase = "intro" | "debating" | "capturable" | "won" | "lost";

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
  phase: EncounterPhase;
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
  /** Latest known phase, tracked from `phase` events and `state` snapshots. */
  phase: EncounterPhase;
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
  const [phase, setPhase] = useState<EncounterPhase>("intro");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountedRef = useRef(false);
  // Mirror phase in a ref so the onclose closure reads the current value
  // (and we never reconnect once the encounter has resolved).
  const phaseRef = useRef<EncounterPhase>("intro");
  const isFinished = (p: EncounterPhase) => p === "won" || p === "lost";

  // Cancel any pending reconnect and detach the socket's onclose handler so a
  // resolved (won/lost) encounter does not loop on reconnect.
  const stopReconnect = useCallback(() => {
    if (reconnectTimer.current != null) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    if (wsRef.current) {
      wsRef.current.onclose = null;
    }
  }, []);

  const disconnect = useCallback(() => {
    if (reconnectTimer.current != null) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
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
      setPhase("intro");
      phaseRef.current = "intro";
      return;
    }

    // Reset state for new encounter
    setTranscript([]);
    setVerdicts([]);
    setPhase("intro");
    phaseRef.current = "intro";

    function connect() {
      if (unmountedRef.current) return;

      const url = wsUrl(`/api/encounters/${encounterId}/stream`);
      const ws = new WebSocket(url);
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        if (unmountedRef.current) { ws.close(); return; }
        setStatus("open");
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
          } else if (msg.type === "state") {
            const s = msg.data as EncounterState;
            setEncounter(s);
            // Hydrate transcript + verdicts from full state snapshot
            if (s.transcript?.length) setTranscript(s.transcript);
            if (s.verdicts?.length) setVerdicts(s.verdicts);
            if (s.phase) {
              setPhase(s.phase);
              phaseRef.current = s.phase;
              if (isFinished(s.phase)) stopReconnect();
            }
          } else if (msg.type === "hp") {
            // Live per-combatant HP update — patch the matching combatant in place.
            const h = msg.data as HpUpdate;
            setEncounter((prev) => {
              if (!prev) return prev;
              const combatants = prev.combatants.map((c) =>
                c.monster_id === h.monster_id
                  ? { ...c, hp: h.hp, max_hp: h.max_hp ?? c.max_hp }
                  : c
              );
              return { ...prev, combatants };
            });
          } else if (msg.type === "phase") {
            const p = (msg.data as PhaseUpdate).phase;
            setPhase(p);
            phaseRef.current = p;
            setEncounter((prev) => (prev ? { ...prev, phase: p } : prev));
            // Stop auto-reconnecting once the battle has resolved.
            if (isFinished(p)) stopReconnect();
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
        // Auto-reconnect unless the encounter has resolved (won/lost).
        if (encounterId && !unmountedRef.current && !isFinished(phaseRef.current)) {
          reconnectTimer.current = setTimeout(() => {
            if (!unmountedRef.current && !isFinished(phaseRef.current)) connect();
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

  return { status, encounter, transcript, verdicts, phase, disconnect };
}
