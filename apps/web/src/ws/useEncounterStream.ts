/**
 * useEncounterStream — WebSocket hook for live encounter events.
 *
 * Connects to WS /api/encounters/{encounterId}/stream and emits typed events.
 * The server (WS-B) pushes JSON messages; we parse them and accumulate state.
 *
 * Message shape from server (at minimum):
 *   { type: "token",      data: TokenDelta }      // live streamed token delta
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

/**
 * Streamed token delta ({ type: "token", data: TokenDelta }).
 * The server emits these incrementally before the canonical `utterance` for the
 * same (turn, actor_id). `text` is the delta to append to the live buffer.
 */
export interface TokenDelta {
  turn: number;
  actor_id: string;
  /** Delta text to append to the live buffer for this (turn, actor_id). */
  text: string;
  /** Optional role hint; the view also resolves role/color from the roster. */
  actor_role?: "party" | "enemy" | "judge";
}

/**
 * A live (in-progress) utterance assembled from streamed `token` deltas, keyed
 * by (turn, actor_id). Closed/reconciled when the canonical `utterance` arrives.
 * `done` flags an empty-buffer utterance so the view can typewriter the full
 * text as a fallback.
 */
export interface LiveUtterance {
  turn: number;
  actor_id: string;
  actor_role?: "party" | "enemy" | "judge";
  text: string;
  /** True once the matching `utterance` closed the buffer (drives fallback). */
  done: boolean;
  /** When true, no tokens streamed — view should typewriter the whole text. */
  fallback: boolean;
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
  /**
   * In-progress utterances assembled from streamed `token` deltas, keyed by
   * `${turn}:${actor_id}`. An entry with `fallback: true` carries the full
   * closed text with no streamed tokens — the view typewriters it. Entries are
   * removed once their canonical `utterance` lands in `transcript`.
   */
  liveTokens: Record<string, LiveUtterance>;
  /** Latest known phase, tracked from `phase` events and `state` snapshots. */
  phase: EncounterPhase;
  /** True while a round is streaming (between drive() and round_done). */
  running: boolean;
  /** Drive a round over the WS so events stream live. `actor_id` = the party
   * agent the player picked (null/omitted = agents auto-pick). Returns false if
   * the socket isn't open yet. */
  drive: (opts?: { rounds?: number; actor_id?: string | null }) => boolean;
  /** Close and clean up the websocket connection manually */
  disconnect: () => void;
}

/** Stable key for the live-token buffer / de-dupe against (turn, actor_id). */
function liveKey(turn: number, actorId: string): string {
  return `${turn}:${actorId}`;
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
  const [liveTokens, setLiveTokens] = useState<Record<string, LiveUtterance>>({});
  const [phase, setPhase] = useState<EncounterPhase>("intro");
  // True while a round is streaming (between drive() and round_done).
  const [running, setRunning] = useState(false);

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

  // Drive a round OVER THE WEBSOCKET so token/utterance/verdict events stream
  // straight to the screen. (Driving via REST runs the round server-side but
  // never pushes the live events to this client — that's why nothing streamed.)
  const drive = useCallback((opts?: { rounds?: number; actor_id?: string | null }) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    setRunning(true);
    ws.send(
      JSON.stringify({ rounds: opts?.rounds ?? 1, actor_id: opts?.actor_id ?? null })
    );
    return true;
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
      setLiveTokens({});
      setPhase("intro");
      phaseRef.current = "intro";
      return;
    }

    // Reset state for new encounter
    setTranscript([]);
    setVerdicts([]);
    setLiveTokens({});
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

          if (msg.type === "token") {
            // Live token delta — append to the buffer keyed by (turn, actor_id).
            const t = msg.data as TokenDelta;
            const key = liveKey(t.turn, t.actor_id);
            setLiveTokens((prev) => {
              const cur = prev[key];
              // Once an utterance has closed this key, ignore stray late tokens
              // so we never re-open a finished line.
              if (cur?.done) return prev;
              const next: LiveUtterance = {
                turn: t.turn,
                actor_id: t.actor_id,
                actor_role: t.actor_role ?? cur?.actor_role,
                text: (cur?.text ?? "") + (t.text ?? ""),
                done: false,
                fallback: false,
              };
              return { ...prev, [key]: next };
            });
          } else if (msg.type === "utterance") {
            const u = msg.data as Utterance;
            const key = liveKey(u.turn, u.actor_id);
            // The canonical utterance closes its live buffer. Reconcile on the
            // same (turn, actor_id) so we render exactly once: append to the
            // transcript (de-duped on turn+actor_id+ts) and retire the buffer.
            setTranscript((prev) => {
              const exists = prev.some(
                (x) => x.turn === u.turn && x.actor_id === u.actor_id && x.ts === u.ts
              );
              return exists ? prev : [...prev, u];
            });
            setLiveTokens((prev) => {
              const cur = prev[key];
              const streamed = (cur?.text ?? "").length > 0;
              if (streamed) {
                // Tokens streamed → transcript bubble takes over; drop buffer.
                if (!cur) return prev;
                const { [key]: _removed, ...rest } = prev;
                return rest;
              }
              // Empty buffer → no tokens arrived; hand the whole text to the
              // view as a typewriter fallback (keep keyed entry, mark done).
              return {
                ...prev,
                [key]: {
                  turn: u.turn,
                  actor_id: u.actor_id,
                  actor_role: u.actor_role,
                  text: u.text,
                  done: true,
                  fallback: true,
                },
              };
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
            // Hydrate transcript + verdicts from full state snapshot.
            // A full snapshot supersedes any in-flight token buffers; drop ones
            // already represented in the snapshot transcript so we don't render
            // a live line and its finished bubble at once.
            if (s.transcript?.length) {
              setTranscript(s.transcript);
              setLiveTokens((prev) => {
                let changed = false;
                const next: Record<string, LiveUtterance> = {};
                for (const [k, v] of Object.entries(prev)) {
                  const covered = s.transcript.some(
                    (x) => x.turn === v.turn && x.actor_id === v.actor_id
                  );
                  if (covered) {
                    changed = true;
                  } else {
                    next[k] = v;
                  }
                }
                return changed ? next : prev;
              });
            }
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
          } else if (msg.type === "round_done") {
            setRunning(false);
          } else if (msg.type === "error") {
            setRunning(false);
            console.error("[useEncounterStream] server error:", msg.message);
          }
        } catch (e) {
          console.error("[useEncounterStream] parse error", e);
        }
      };

      ws.onerror = () => {
        if (unmountedRef.current) return;
        setStatus("error");
        setRunning(false);
      };

      ws.onclose = () => {
        if (unmountedRef.current) return;
        setStatus("closed");
        setRunning(false);
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

  return { status, encounter, transcript, verdicts, liveTokens, phase, running, drive, disconnect };
}
