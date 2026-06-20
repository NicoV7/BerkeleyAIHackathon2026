/**
 * BattleDebateView — live encounter screen.
 *
 * Reads activeEncounterId from the game store, opens the WS stream,
 * renders combatant HP bars, a scrolling chat-style transcript with
 * per-side coloring, running judge verdicts, and action buttons.
 *
 * Buttons:
 *  - Next Round  POST /api/encounters/{id}/turn
 *  - Auto (N)    POST /api/encounters/{id}/auto  { rounds: 3 }
 *  - Capture     POST /api/encounters/{id}/capture  (shown when capturable)
 *  - Flee        POST /api/encounters/{id}/flee
 *
 * Export this component; App.tsx (WS-orchestrator) wires it in Wave 2.
 */
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import {
  CombatantState,
  JudgeVerdict,
  Utterance,
  useEncounterStream,
} from "../ws/useEncounterStream";
import { Scoreboard } from "./Scoreboard";

/** Cosmetic pre-round stance options (wiring is a future task). */
const STANCES = ["Aggressive", "Measured", "Defensive"] as const;
type Stance = (typeof STANCES)[number];

/** A verdict is "winning" when its unsigned 0-100 score is above the 50 average. */
function isWinningScore(score: number): boolean {
  return score - 50 > 0;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function HpBar({ hp, max_hp }: { hp: number; max_hp: number }) {
  const pct = max_hp > 0 ? Math.max(0, Math.min(100, (hp / max_hp) * 100)) : 0;
  const color =
    pct > 60
      ? "bg-green-500"
      : pct > 30
        ? "bg-yellow-500"
        : "bg-red-500";
  return (
    <div className="w-full h-2 bg-white/10 rounded overflow-hidden">
      <div
        className={`h-full transition-all duration-500 ${color}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function CombatantCard({ c, winning }: { c: CombatantState; winning: boolean }) {
  const border = c.role === "party" ? "border-indigo-500/50" : "border-rose-500/50";
  const label = c.role === "party" ? "text-indigo-300" : "text-rose-300";
  // Non-text win signal: a bright ring + glow that animates on when this side
  // just landed the winning argument — readable across a room.
  const glow = winning
    ? "ring-2 ring-amber-400 shadow-[0_0_28px_rgba(251,191,36,0.7)] scale-[1.02]"
    : "ring-0";
  return (
    <div
      className={`border ${border} rounded p-2 min-w-[140px] flex-1 transition-all duration-500 ${glow}`}
    >
      <div className="flex items-center gap-1">
        <span className={`text-xs font-semibold uppercase tracking-wide ${label}`}>
          {c.role}
        </span>
        {winning && (
          <span className="text-xs font-black text-amber-300 animate-pulse">★</span>
        )}
      </div>
      <div className="font-bold truncate">{c.name}</div>
      <div className="text-xs text-white/50 mb-1">
        [{c.type}] {c.hp}/{c.max_hp} HP
      </div>
      <HpBar hp={c.hp} max_hp={c.max_hp} />
    </div>
  );
}

function UtteranceBubble({ u, liveNames }: { u: Utterance; liveNames: Record<string, string> }) {
  const isParty = u.actor_role === "party";
  const isJudge = u.actor_role === "judge";

  const bg = isJudge
    ? "bg-yellow-900/40 border-yellow-600/40"
    : isParty
      ? "bg-indigo-900/40 border-indigo-500/40"
      : "bg-rose-900/40 border-rose-500/40";

  const nameColor = isJudge
    ? "text-yellow-400"
    : isParty
      ? "text-indigo-300"
      : "text-rose-300";

  const actorName = liveNames[u.actor_id] ?? u.actor_id;

  return (
    <div className={`border rounded p-2 text-sm ${bg}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-xs font-semibold ${nameColor}`}>{actorName}</span>
        {u.skill_used && (
          <span className="text-xs bg-white/10 rounded px-1 py-0.5">{u.skill_used}</span>
        )}
        <span className="ml-auto text-xs text-white/30">turn {u.turn}</span>
      </div>
      <p className="text-white/90 leading-relaxed whitespace-pre-wrap">{u.text}</p>
    </div>
  );
}

function VerdictBadge({ v }: { v: JudgeVerdict }) {
  // Scores are unsigned 0-100; 50 is the break-even average.
  const positive = isWinningScore(v.score);
  return (
    <div
      className={`text-xs rounded px-2 py-1 border ${
        positive ? "border-green-500/50 bg-green-900/30" : "border-red-500/50 bg-red-900/30"
      }`}
    >
      <span className="font-semibold">[Judge T{v.turn}]</span>{" "}
      {v.target} · score {v.score.toFixed(0)}/100 · -{v.damage} HP ·{" "}
      <span className="opacity-70 italic">{v.rationale}</span>
    </div>
  );
}

/**
 * JumboVerdictBanner — the hero of the screen. One per frame: the LATEST
 * verdict gets a full-width, oversized "ARGUMENT WON BECAUSE …" headline,
 * with logic / persuasion as small chips and the damage dealt. Designed to be
 * the single most legible element so a spectator instantly knows WHY.
 */
function JumboVerdictBanner({ v }: { v: JudgeVerdict }) {
  const won = isWinningScore(v.score);
  const why = v.why ?? v.rationale ?? "—";
  const heading = won ? "ARGUMENT WON BECAUSE" : "ARGUMENT FELL SHORT";
  const frame = won
    ? "border-amber-400/60 bg-gradient-to-r from-amber-900/40 via-yellow-900/30 to-amber-900/40 shadow-[0_0_40px_rgba(251,191,36,0.45)]"
    : "border-rose-500/50 bg-gradient-to-r from-rose-950/50 to-red-950/40";

  return (
    <div className={`rounded-xl border-2 ${frame} px-5 py-4 transition-all duration-500`}>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[11px] font-black uppercase tracking-[0.25em] text-amber-300/90">
          {heading}
        </span>
        <span className="text-[11px] text-white/40">· turn {v.turn}</span>
      </div>
      <div className="text-2xl md:text-4xl font-black leading-tight text-white drop-shadow">
        “{why}”
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Chip label="LOGIC" value={v.logic} tone="indigo" />
        <Chip label="PERSUASION" value={v.persuasion} tone="fuchsia" />
        <Chip label="SCORE" value={v.score} tone={won ? "green" : "red"} />
        <span className="ml-auto text-base font-black text-rose-300 tabular-nums">
          −{v.damage} HP
        </span>
      </div>
    </div>
  );
}

function Chip({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | undefined;
  tone: "indigo" | "fuchsia" | "green" | "red";
}) {
  const tones: Record<string, string> = {
    indigo: "border-indigo-400/50 text-indigo-200 bg-indigo-500/10",
    fuchsia: "border-fuchsia-400/50 text-fuchsia-200 bg-fuchsia-500/10",
    green: "border-green-400/50 text-green-200 bg-green-500/10",
    red: "border-red-400/50 text-red-200 bg-red-500/10",
  };
  return (
    <span
      className={`text-[11px] font-bold uppercase tracking-wide rounded-full border px-2.5 py-1 tabular-nums ${tones[tone]}`}
    >
      {label} {value == null ? "—" : Math.round(value)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function BattleDebateView() {
  const { activeEncounterId, setEncounter } = useGame();
  const { status, encounter, transcript, verdicts, phase } =
    useEncounterStream(activeEncounterId);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [stance, setStance] = useState<Stance>("Measured");

  // Auto-scroll transcript to bottom on new messages
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript.length]);

  // Name lookup map from live encounter combatants (fallback to ids)
  const liveNames: Record<string, string> = {};
  const combatants: CombatantState[] = encounter?.combatants ?? [];
  for (const c of combatants) {
    liveNames[c.monster_id] = c.name;
  }
  // Add judge pseudo-id
  liveNames["judge"] = "Judge";

  const isCapturable = phase === "capturable";
  const isOver = phase === "won" || phase === "lost";

  // Hero: the single latest verdict drives the jumbo banner (one per frame).
  const latestVerdict: JudgeVerdict | null =
    verdicts.length > 0 ? verdicts[verdicts.length - 1] : null;

  // Which side just landed the winning argument? Used for the non-text glow.
  const winningCombatantId =
    latestVerdict && isWinningScore(latestVerdict.score)
      ? (latestVerdict.actor_id ?? latestVerdict.target)
      : null;
  const preRound = phase === "intro" && transcript.length === 0;

  async function doAction(path: string, body?: unknown) {
    if (!activeEncounterId || busy) return;
    setActionError(null);
    setBusy(true);
    try {
      await api.post(`/api/encounters/${activeEncounterId}${path}`, body);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleCapture() {
    // Find a capturable wild enemy id; fall back to first enemy
    const enemy = combatants.find((c) => c.role === "enemy");
    if (!enemy || !activeEncounterId) return;
    setActionError(null);
    setBusy(true);
    try {
      await api.post(`/api/encounters/${activeEncounterId}/capture`, {
        wild_id: enemy.monster_id,
      });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function handleFlee() {
    // Flee exits the encounter locally; WS-B may also expose a flee endpoint.
    setEncounter(null);
  }

  // ---- No active encounter ----
  if (!activeEncounterId) {
    return (
      <div className="flex-1 grid place-items-center opacity-50">
        <div className="text-center">
          <div className="text-4xl mb-3">⚔️</div>
          <div className="text-sm font-mono">No active encounter</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full max-h-screen overflow-hidden">
      {/* Header: topic + status */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-white/10">
        <div className="text-sm font-semibold truncate">
          {encounter?.topic ?? "Loading encounter…"}
        </div>
        <div className="flex items-center gap-2 text-xs text-white/50">
          <span
            className={
              status === "open"
                ? "text-green-400"
                : status === "connecting"
                  ? "text-yellow-400"
                  : "text-red-400"
            }
          >
            ● {status}
          </span>
          <span>T{encounter?.turn_no ?? 0}</span>
          <span
            className={
              phase === "won"
                ? "text-green-400"
                : phase === "lost"
                  ? "text-red-400"
                  : phase === "capturable"
                    ? "text-yellow-400"
                    : "text-white/50"
            }
          >
            {phase}
          </span>
        </div>
      </div>

      {/* Scoreboard: per-side average + ▲/▼ trend (derived from score-50) */}
      <div className="px-3 pt-3">
        <Scoreboard verdicts={verdicts} combatants={combatants} />
      </div>

      {/* Pre-round stance picker (cosmetic for now) */}
      {preRound && (
        <div className="px-3 pt-3 flex items-center gap-2 text-sm">
          <span className="text-xs uppercase tracking-wide text-white/50">Stance</span>
          <select
            value={stance}
            onChange={(e) => setStance(e.target.value as Stance)}
            className="rounded bg-white/5 border border-white/15 px-2 py-1 text-sm"
          >
            {STANCES.map((s) => (
              <option key={s} value={s} className="bg-zinc-900">
                {s}
              </option>
            ))}
          </select>
          <span className="text-xs text-white/30">— sets your opening tone</span>
        </div>
      )}

      {/* Combatant HP bars (animated via CSS transition on width + glow) */}
      <div className="flex gap-2 p-3 border-b border-white/10 flex-wrap">
        {combatants.length === 0 ? (
          <div className="text-xs text-white/30">Awaiting combatant data…</div>
        ) : (
          combatants.map((c) => (
            <CombatantCard
              key={c.monster_id}
              c={c}
              winning={c.monster_id === winningCombatantId}
            />
          ))
        )}
      </div>

      {/* JUMBO hero verdict banner — the single most legible element */}
      {latestVerdict && (
        <div className="px-3 pt-3">
          <JumboVerdictBanner v={latestVerdict} />
        </div>
      )}

      {/* Split: transcript left, verdicts right */}
      <div className="flex flex-1 overflow-hidden gap-2 p-2">
        {/* Transcript */}
        <div className="flex-1 flex flex-col overflow-hidden">
          <div className="text-xs text-white/30 uppercase tracking-wide mb-1 px-1">
            Transcript ({transcript.length} turns)
          </div>
          <div className="flex-1 overflow-y-auto space-y-2 pr-1">
            {transcript.map((u, i) => (
              <UtteranceBubble key={`${u.turn}-${u.actor_id}-${i}`} u={u} liveNames={liveNames} />
            ))}
            {transcript.length === 0 && (
              <div className="text-sm text-white/30 italic px-1">
                Debate will appear here once the first round starts…
              </div>
            )}
            <div ref={transcriptEndRef} />
          </div>
        </div>

        {/* Verdicts panel */}
        <div className="w-64 shrink-0 flex flex-col overflow-hidden">
          <div className="text-xs text-white/30 uppercase tracking-wide mb-1 px-1">
            Judge Verdicts
          </div>
          <div className="flex-1 overflow-y-auto space-y-2">
            {verdicts.map((v, i) => (
              <VerdictBadge key={`${v.turn}-${v.target}-${i}`} v={v} />
            ))}
            {verdicts.length === 0 && (
              <div className="text-xs text-white/30 italic px-1">No verdicts yet</div>
            )}
          </div>
        </div>
      </div>

      {/* Action bar */}
      <div className="border-t border-white/10 px-3 py-2 flex items-center gap-2 flex-wrap">
        {actionError && (
          <span className="text-xs text-red-400 flex-1">{actionError}</span>
        )}

        <button
          disabled={busy || isOver}
          onClick={() => doAction("/turn")}
          className="px-3 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40"
        >
          {busy ? "…" : "Next Round"}
        </button>

        <button
          disabled={busy || isOver}
          onClick={() => doAction("/auto", { rounds: 3 })}
          className="px-3 py-1.5 text-sm rounded bg-indigo-800 hover:bg-indigo-700 disabled:opacity-40"
        >
          Auto (3)
        </button>

        {isCapturable && (
          <button
            disabled={busy}
            onClick={handleCapture}
            className="px-3 py-1.5 text-sm rounded bg-yellow-600 hover:bg-yellow-500 disabled:opacity-40"
          >
            Capture
          </button>
        )}

        <button
          disabled={busy}
          onClick={handleFlee}
          className="ml-auto px-3 py-1.5 text-sm rounded bg-white/5 hover:bg-white/10"
        >
          Flee
        </button>
      </div>
    </div>
  );
}

export default BattleDebateView;
