/**
 * useEncounterStream — WebSocket hook for live encounter events.
 *
 * Connects to WS /api/encounters/{encounterId}/stream and emits typed events.
 * The server (WS-B + token-streaming port) pushes JSON messages; we parse them
 * and accumulate state.
 *
 * Message shape from server (at minimum):
 *   { type: "token",      data: TokenDelta }      // live streamed token delta
 *   { type: "utterance",  data: Utterance }
 *   { type: "verdict",    data: JudgeVerdict }
 *   { type: "state",      data: EncounterState }
 *   { type: "hp",         data: HpUpdate }        // live per-combatant HP
 *   { type: "phase",      data: PhaseUpdate }     // phase transitions
 *   { type: "round_done", data: { phase } }       // one drive() cycle finished
 *   { type: "error",      message: string }
 *
 * The hook re-connects automatically when encounterId changes; reconnect stops
 * once the encounter resolves (won/lost).
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
  server_ts?: number;
  elapsed_ms?: number;
}

export interface JudgeVerdict {
  turn: number;
  target: string;
  /** Unsigned 0-100 (50 = average). Compare against 50 for win/positive logic. */
  score: number;
  rationale: string;
  damage: number;
  // --- additive fields from the Wave-1 WS-1 judge expansion (commit 352f8f4) ---
  // All optional for back-compat with older persisted verdicts.
  /** One-line explanation of WHY the argument landed. Hero banner copy. */
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
  /** Server wall-clock timestamp and elapsed generation time for latency telemetry. */
  server_ts?: number;
  elapsed_ms?: number;
}

/**
 * A live (in-progress) utterance assembled from streamed `token` deltas, keyed
 * by (turn, actor_id). Closed/reconciled when the canonical `utterance` arrives.
 * `fallback` flags an empty-buffer utterance so the view can typewriter the full
 * text as a fallback.
 */
export interface LiveUtterance {
  turn: number;
  actor_id: string;
  actor_role?: "party" | "enemy" | "judge";
  text: string;
  server_ts?: number;
  elapsed_ms?: number;
  /** True once the matching `utterance` closed the buffer (drives fallback). */
  done: boolean;
  /** When true, no tokens streamed — view should typewriter the whole text. */
  fallback: boolean;
}

/**
 * A4 optimistic-judge estimate ({ type: "estimate", data: EstimateScore }).
 * Emitted by the server immediately after the player submits — an instant
 * heuristic DISPLAY score, before the slot-bound LLM judge returns. It carries
 * NO damage and never moves HP; the UI shows it in an "estimating…" state and
 * settles it to the authoritative `verdict` score (matched by turn+actor_id).
 */
export interface EstimateScore {
  turn: number;
  actor_id: string;
  score: number;
  actor_role?: "party" | "enemy" | "judge";
  server_ts?: number;
  elapsed_ms?: number;
}

/** Live HP update for a single combatant ({ type: "hp", data: HpUpdate }). */
export interface HpUpdate {
  monster_id: string;
  hp: number;
  max_hp?: number;
}

/**
 * Live MP update for a single combatant ({ type: "mp", data: MpUpdate }).
 * Emitted by the orchestrator on (a) end-of-round +10 regen and (b) a skill
 * use deducting the cost. Mirrors HpUpdate's shape for symmetry.
 */
export interface MpUpdate {
  monster_id: string;
  mp: number;
  max_mp?: number;
}

/**
 * Server-side rejection from the WS argue path when the player's chosen skill
 * costs more MP than the lead party monster currently has. The view dims the
 * skill chip and shows the typed detail; the round itself was never started.
 */
export interface MpInsufficient {
  skill_id: string | null;
  detail: string;
}

/**
 * Level-up event ({ type: "LevelUp", monster_id, new_level, stat_gains }).
 *
 * Emitted by the server when finalize awards XP that crosses a level boundary
 * for a party monster. The fields are TOP-LEVEL (not nested under `data`) to
 * match the encounter finalize emission contract in
 * `apps/api/app/routers/debate.py::_finalize`.
 *
 * The hook re-broadcasts these as a global `CustomEvent("encounter:level-up")`
 * on `window` so the cinematic overlay can subscribe without threading the
 * event through React state. The detail payload is this interface.
 */
export interface LevelUpEvent {
  monster_id: string;
  new_level: number;
  stat_gains: { atk: number; def: number; mp: number; hp: number };
}

/** Browser CustomEvent name the WS hook dispatches for level-up cinematics. */
export const LEVEL_UP_EVENT = "encounter:level-up";

export type EncounterPhase = "intro" | "debating" | "capturable" | "won" | "lost";

/** Phase transition ({ type: "phase", data: PhaseUpdate }). */
export interface PhaseUpdate {
  phase: EncounterPhase;
  capturable_ids?: string[];
  turn_no?: number;
}

export interface CombatantState {
  monster_id: string;
  name: string;
  type: string;
  role: "party" | "enemy";
  hp: number;
  max_hp: number;
  // Gacha Wave B additive fields — optional so older snapshots still render.
  mp?: number;
  max_mp?: number;
  atk?: number;
  def?: number;
  domain?: string;
  is_avatar?: boolean;
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
  /** Wild monster ids currently capturable (from the latest phase event). */
  capturableIds: string[];
  /**
   * Last MP-gate rejection from the WS argue path (cleared on the next
   * successful drive/argue). The view dims the offending skill + shows `detail`.
   */
  mpInsufficient: MpInsufficient | null;
  /**
   * In-progress utterances assembled from streamed `token` deltas, keyed by
   * `${turn}:${actor_id}`. An entry with `fallback: true` carries the full
   * closed text with no streamed tokens — the view typewriters it. Entries are
   * removed once their canonical `utterance` lands in `transcript`.
   */
  liveTokens: Record<string, LiveUtterance>;
  /**
   * A4 optimistic-judge estimates, keyed by `${turn}:${actor_id}`. An entry is
   * the instant heuristic score shown right after submit; it is removed once the
   * authoritative `verdict` for the same (turn, actor) lands, so the view can
   * render an "estimating…" badge that settles to the real score.
   */
  estimates: Record<string, EstimateScore>;
  /** Latest known phase, tracked from `phase` events and `state` snapshots. */
  phase: EncounterPhase;
  /** True while a round is streaming (between drive() and round_done). */
  running: boolean;
  /**
   * Turn number of the round currently being driven (the turn we expect the
   * round to produce), or null when idle. Surfaced for the "Debating… (turn N)"
   * in-progress indicator so the UI never leaves the user staring at nothing.
   */
  runningTurn: number | null;
  /** Drive N autonomous rounds (Auto / Next Round). */
  drive: (rounds: number) => void;
  /** Submit a human-typed argument as the player's turn. `side` is the player's
   *  chosen Pro/Con stance, sent additively (frontend-only seam). */
  argue: (text: string, skillId?: string | null, side?: "for" | "against" | null) => void;
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
  const [capturableIds, setCapturableIds] = useState<string[]>([]);
  const [liveTokens, setLiveTokens] = useState<Record<string, LiveUtterance>>({});
  // A4: optimistic-judge estimates, keyed by `${turn}:${actor_id}`.
  const [estimates, setEstimates] = useState<Record<string, EstimateScore>>({});
  const [phase, setPhase] = useState<EncounterPhase>("intro");
  // Last MP-gate rejection; cleared when the next successful round drains.
  const [mpInsufficient, setMpInsufficient] = useState<MpInsufficient | null>(null);
  // True while a round is streaming (between drive() and round_done).
  const [running, setRunning] = useState(false);
  // Turn we are driving toward; surfaced for the in-progress indicator.
  const [runningTurn, setRunningTurn] = useState<number | null>(null);
  // Mirror the latest known turn so a freshly sent command can label itself
  // "turn N+1" even before any state/phase event lands.
  const turnRef = useRef(0);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountedRef = useRef(false);
  // Commands queued while the socket is still connecting (drained on onopen).
  const pendingQueue = useRef<Record<string, unknown>[]>([]);
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

  /**
   * Send a command now if the socket is OPEN, else queue it until onopen.
   * NOTE: we queue when the socket is anything other than OPEN (CONNECTING,
   * CLOSING, CLOSED, or absent) so the very first command after entering a
   * battle is never dropped — onopen (or a reconnect's onopen) drains it.
   */
  const send = useCallback((payload: Record<string, unknown>) => {
    // Clear any stale MP-gate rejection from the previous attempt — the user is
    // trying again, so dimming the skill chip / showing an old detail is wrong.
    setMpInsufficient(null);
    // Mark in-progress immediately so the UI shows feedback the instant the
    // user clicks, even while the socket is still CONNECTING.
    setRunning(true);
    setRunningTurn(turnRef.current + 1);
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
    // `side` is sent additively (frontend-only): the backend currently ignores
    // unknown fields and assigns party=for itself, but threading the player's
    // chosen Pro/Con stance here is the seam for when the loop adopts it.
    (text: string, skillId?: string | null, side?: "for" | "against" | null) =>
      send({ action: "argue", text, skill_id: skillId ?? null, ...(side ? { side } : {}) }),
    [send]
  );

  /**
   * Tear the socket down. `hard` (the default, used by manual disconnect and
   * encounter-id changes) closes the socket regardless of readyState and clears
   * the pending queue. A non-hard teardown is StrictMode-safe: it never closes a
   * socket that is still CONNECTING (closing a CONNECTING socket triggers the
   * "WebSocket is closed before the connection is established" warning AND loses
   * any queued command), and it preserves the pending queue so a command queued
   * before the (re)connect still drains on the surviving / next socket.
   */
  const disconnect = useCallback((hard = true) => {
    if (reconnectTimer.current != null) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    const ws = wsRef.current;
    if (ws) {
      if (!hard && ws.readyState === WebSocket.CONNECTING) {
        // StrictMode double-mount / HMR: leave the in-flight socket alone so the
        // remount can reuse it and drain the queue. Detaching here would close a
        // CONNECTING socket and silently drop queued Next Round / Argue commands.
        return;
      }
      ws.onclose = null; // prevent reconnect loop
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        /* already closing/closed */
      }
      wsRef.current = null;
    }
    if (hard) {
      pendingQueue.current = [];
      setStatus("closed");
      setRunning(false);
      setRunningTurn(null);
    }
  }, []);

  // Tracks the encounter id the current socket / queue belongs to, so a
  // StrictMode remount on the SAME id reuses everything (and does NOT wipe the
  // pending queue), while a genuine id change resets cleanly.
  const connectedIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!encounterId) {
      connectedIdRef.current = null;
      disconnect(true);
      setEncounter(null);
      setTranscript([]);
      setVerdicts([]);
      setCapturableIds([]);
      setLiveTokens({});
      setEstimates({});
      setPhase("intro");
      phaseRef.current = "intro";
      return;
    }

    // Mark this effect-run as mounted. (StrictMode runs cleanup between the two
    // mounts; the second mount resets this back to false.)
    unmountedRef.current = false;

    // Only reset per-encounter state when this is a genuinely new encounter id.
    // On a StrictMode double-mount (same id) we keep the pending queue and any
    // socket already in flight so the first queued command still runs.
    const isNewEncounter = connectedIdRef.current !== encounterId;
    if (isNewEncounter) {
      pendingQueue.current = [];
      setTranscript([]);
      setVerdicts([]);
      setCapturableIds([]);
      setLiveTokens({});
      setEstimates({});
      setPhase("intro");
      phaseRef.current = "intro";
      turnRef.current = 0;
      setRunning(false);
      setRunningTurn(null);
      // A real id change must tear down any old socket from the previous id.
      disconnect(true);
    }
    connectedIdRef.current = encounterId;

    function connect() {
      if (unmountedRef.current) return;
      // Reuse a socket that is already open or in flight (StrictMode remount).
      const existing = wsRef.current;
      if (
        existing &&
        (existing.readyState === WebSocket.OPEN ||
          existing.readyState === WebSocket.CONNECTING)
      ) {
        // If it is already open, drain anything queued during the gap.
        if (existing.readyState === WebSocket.OPEN) {
          const queued = pendingQueue.current.splice(0);
          if (queued.length) setRunning(true);
          for (const payload of queued) existing.send(JSON.stringify(payload));
        }
        return;
      }

      const url = wsUrl(`/api/encounters/${encounterId}/stream`);
      const ws = new WebSocket(url);
      wsRef.current = ws;
      setStatus("connecting");

      ws.onopen = () => {
        if (unmountedRef.current) { ws.close(); return; }
        setStatus("open");
        // Drain ALL commands queued before the socket opened (this is what makes
        // the first Next Round / Argue after entering a battle actually run,
        // even when it was clicked while the socket was still CONNECTING).
        const queued = pendingQueue.current.splice(0);
        if (queued.length) {
          setRunning(true);
          setRunningTurn(turnRef.current + 1);
        }
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
                server_ts: t.server_ts ?? cur?.server_ts,
                elapsed_ms: t.elapsed_ms ?? cur?.elapsed_ms,
                done: false,
                fallback: false,
              };
              return { ...prev, [key]: next };
            });
          } else if (msg.type === "estimate") {
            // A4: instant optimistic display score for the player's argument.
            // Store it keyed by (turn, actor) so the view can show "estimating…"
            // until the authoritative verdict lands and supersedes it.
            const est = msg.data as EstimateScore;
            const key = liveKey(est.turn, est.actor_id);
            setEstimates((prev) => ({ ...prev, [key]: est }));
          } else if (msg.type === "utterance") {
            const u = msg.data as Utterance;
            if (u.turn > turnRef.current) turnRef.current = u.turn;
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
                  server_ts: u.server_ts,
                  elapsed_ms: u.elapsed_ms,
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
            // A4 reconcile: the authoritative score has landed → retire the
            // optimistic estimate for this (turn, actor) so the badge settles.
            // The verdict is attributed to the speaker via `actor_id`.
            if (v.actor_id) {
              const key = liveKey(v.turn, v.actor_id);
              setEstimates((prev) => {
                if (!(key in prev)) return prev;
                const { [key]: _removed, ...rest } = prev;
                return rest;
              });
            }
          } else if (msg.type === "hp") {
            // Live per-combatant HP update — patch the matching combatant in place.
            // (Port from d5c4a6d: server now emits HpUpdate per combatant, not a map.)
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
          } else if (msg.type === "mp") {
            // Gacha Wave B: live per-combatant MP update. Same shape as HpUpdate,
            // emitted on end-of-round +10 regen AND on skill-use deduction. We
            // patch the combatant in place so the blue MP bar drains/fills with
            // no extra fetch.
            const m = msg.data as MpUpdate;
            setEncounter((prev) => {
              if (!prev) return prev;
              const combatants = prev.combatants.map((c) =>
                c.monster_id === m.monster_id
                  ? { ...c, mp: m.mp, max_mp: m.max_mp ?? c.max_mp }
                  : c
              );
              return { ...prev, combatants };
            });
          } else if (msg.type === "mp_insufficient") {
            // The WS argue path rejected the chosen skill (cost > current MP).
            // Surface a one-shot banner; the view dims the matching skill chip.
            const d = msg.data as MpInsufficient;
            setMpInsufficient(d);
            setRunning(false);
            setRunningTurn(null);
          } else if (msg.type === "phase") {
            const p = msg.data as PhaseUpdate;
            if (typeof p.turn_no === "number" && p.turn_no > turnRef.current) {
              turnRef.current = p.turn_no;
            }
            setPhase(p.phase);
            phaseRef.current = p.phase;
            setEncounter((prev) =>
              prev
                ? { ...prev, phase: p.phase, turn_no: p.turn_no ?? prev.turn_no }
                : prev
            );
            setCapturableIds(p.capturable_ids ?? []);
            // Stop auto-reconnecting once the battle has resolved.
            if (isFinished(p.phase)) {
              stopReconnect();
              setRunning(false);
              setRunningTurn(null);
            }
          } else if (msg.type === "state") {
            const s = msg.data as EncounterState;
            if (typeof s.turn_no === "number" && s.turn_no > turnRef.current) {
              turnRef.current = s.turn_no;
            }
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
            if (s.verdicts?.length) {
              setVerdicts(s.verdicts);
              // Drop any optimistic estimate already covered by a snapshot
              // verdict (the authoritative score is present in the snapshot).
              setEstimates((prev) => {
                let changed = false;
                const next: Record<string, EstimateScore> = {};
                for (const [k, est] of Object.entries(prev)) {
                  const covered = s.verdicts.some(
                    (x) => x.turn === est.turn && x.actor_id === est.actor_id
                  );
                  if (covered) changed = true;
                  else next[k] = est;
                }
                return changed ? next : prev;
              });
            }
            if (s.phase) {
              setPhase(s.phase);
              phaseRef.current = s.phase;
              if (isFinished(s.phase)) {
                stopReconnect();
                setRunning(false);
                setRunningTurn(null);
              }
            }
          } else if (msg.type === "LevelUp") {
            // Server emits the level-up payload at the TOP LEVEL of the
            // message (not under `data`) so the gains can be read directly.
            // Re-broadcast as a window CustomEvent so the overlay can mount
            // anywhere in the tree without prop-drilling. Defensive: never
            // let an event-dispatch failure crash the message loop.
            try {
              const detail: LevelUpEvent = {
                monster_id: (msg as unknown as LevelUpEvent).monster_id,
                new_level: (msg as unknown as LevelUpEvent).new_level,
                stat_gains: (msg as unknown as LevelUpEvent).stat_gains ?? {
                  atk: 0,
                  def: 0,
                  mp: 0,
                  hp: 0,
                },
              };
              window.dispatchEvent(
                new CustomEvent<LevelUpEvent>(LEVEL_UP_EVENT, { detail })
              );
            } catch {
              /* event dispatch is best-effort */
            }
          } else if (msg.type === "round_done") {
            // A drive() cycle finished. If more commands are still queued (e.g.
            // Auto chained), keep running; otherwise clear the indicator.
            if (pendingQueue.current.length === 0) {
              setRunning(false);
              setRunningTurn(null);
            }
          } else if (msg.type === "error") {
            setRunning(false);
            setRunningTurn(null);
            console.error("[useEncounterStream] server error:", msg.message);
          }
        } catch (e) {
          console.error("[useEncounterStream] parse error", e);
        }
      };

      ws.onerror = () => {
        if (unmountedRef.current) return;
        setStatus("error");
        // Keep `running`/`runningTurn` + the pending queue intact: onclose will
        // schedule a reconnect that drains the queue so the command still runs.
      };

      ws.onclose = () => {
        if (unmountedRef.current) return;
        setStatus("closed");
        // Auto-reconnect unless the encounter has resolved (won/lost). Keep the
        // pending queue so onopen on the reconnected socket drains it; do not
        // clear `running` here — a queued command is still pending.
        if (encounterId && !unmountedRef.current && !isFinished(phaseRef.current)) {
          reconnectTimer.current = setTimeout(() => {
            if (!unmountedRef.current && !isFinished(phaseRef.current)) connect();
          }, RECONNECT_DELAY_MS);
        } else {
          setRunning(false);
          setRunningTurn(null);
        }
      };
    }

    connect();

    return () => {
      unmountedRef.current = true;
      // StrictMode-safe teardown: a soft disconnect never closes a CONNECTING
      // socket and never wipes the pending queue, so the immediate remount can
      // reuse the in-flight socket and drain any queued command.
      disconnect(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [encounterId]);

  return {
    status,
    encounter,
    transcript,
    verdicts,
    capturableIds,
    mpInsufficient,
    liveTokens,
    estimates,
    phase,
    running,
    runningTurn,
    drive,
    argue,
    disconnect: () => disconnect(true),
  };
}
