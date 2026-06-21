/**
 * BattleDebateView — live human-argues encounter screen (WS-G §3).
 *
 * The player IS the lead party monster: they type an argument each round, the
 * judge scores them, and the enemy rebuts autonomously. HP/verdict/phase all
 * stream over the WS. Auto (3) still runs a fully autonomous debate.
 *
 * Drive model (single path, all over the WS — no REST /turn or /auto):
 *  - Submit Argument  ws.send({action:"argue", text, skill_id})
 *  - Next Round       drive(1)   (autonomous showcase / fallback)
 *  - Auto (3)         drive(3)
 *  - Capture          POST /api/encounters/{id}/capture {wild_id}  (when capturable)
 *  - Flee             POST /api/encounters/{id}/flee  then leave
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { components } from "@debate/shared/types";
import { useGame } from "../state/store";
import {
  parseSkills,
  typeColor,
  skillTooltip,
  effectivenessInfo,
  type ParsedSkill,
} from "../lib/skills";
import { HPCorner } from "./battle/HPCorner";
import { JudgePanel } from "./battle/JudgePanel";
import { BattleLog } from "./battle/BattleLog";
import { BattleStage } from "./battle/BattleStage";
import { BattleIntroCard } from "./battle/BattleIntroCard";

// Argue Copilot contract (POST /api/encounters/{eid}/assist). Sourced from the
// generated OpenAPI schema so the request/response stay in lockstep with the API.
type AssistResult = components["schemas"]["AssistResult"];
type AssistSuggestion = components["schemas"]["AssistSuggestion"];
// Memory Recall (Wave C: the headline ability). Hits POST /api/encounters/{eid}/memory-recall
// and renders a 4-second full-screen overlay showing the raw Redis transcript key,
// the highlighted enemy line, and the typewritten counter.
type MemoryRecallResult = components["schemas"]["MemoryRecallResult"];

// MP cost mirrors `mp_cost: 60` in apps/api/app/skills/memory_recall.md. The
// button is greyed when current MP < this; the local fallback (when the MP map
// is empty — e.g. before Wave B integrates) assumes max_mp so the demo plays.
const MEMORY_RECALL_MP_COST = 60;
// Wall-clock duration the overlay stays up. The backend caps the LLM call to
// ~20s; 4s of theatre after the response is the headline-feature moment.
const MEMORY_RECALL_OVERLAY_MS = 4000;
const MEMORY_RECALL_TYPE_SPEED_MS = 22;
import {
  sfxBlip,
  sfxSubmit,
  sfxHit,
  sfxCapture,
  sfxWin,
  sfxLose,
  setSfxEnabled,
} from "../lib/sfx";
import { type TrendSeries } from "./ReasoningTrend";
import SummonOverlay, { type SummonResult } from "../game/SummonOverlay";
import { useIrisTransition } from "./fx/IrisWipe";
import {
  CombatantState,
  JudgeVerdict,
  Utterance,
  useEncounterStream,
} from "../ws/useEncounterStream";
import LevelUpOverlay from "./LevelUpOverlay";

// ---------------------------------------------------------------------------
// Debate sides — the player's monster argues FOR the topic, the enemy AGAINST.
// The backend sets `side` ("for"/"against") on CombatantState; if absent we
// infer from role (party => FOR, enemy => AGAINST) so labels always render.
// ---------------------------------------------------------------------------

type DebateSide = "for" | "against";

function combatantSide(c: CombatantState): DebateSide {
  // `side` is an optional backend-provided hint; read it defensively so this
  // works whether or not the generated type declares it yet.
  const raw = (c as { side?: string }).side;
  if (raw === "for" || raw === "against") return raw;
  return c.role === "party" ? "for" : "against";
}

function sideLabel(side: DebateSide): string {
  return side === "for" ? "FOR" : "AGAINST";
}

function sideColor(side: DebateSide): string {
  return side === "for" ? "var(--win)" : "var(--danger)";
}

// ---------------------------------------------------------------------------
// HP bar — chunky segmented, drains on hp events
// ---------------------------------------------------------------------------

function HpBar({ hp, max_hp }: { hp: number; max_hp: number }) {
  const segs = 16;
  const pct = max_hp > 0 ? Math.max(0, Math.min(1, hp / max_hp)) : 0;
  const filled = Math.round(pct * segs);
  const color = pct > 0.6 ? "var(--win)" : pct > 0.3 ? "var(--warn)" : "var(--danger)";
  return (
    <div className="flex gap-[2px] h-3">
      {Array.from({ length: segs }).map((_, i) => (
        <div
          key={i}
          className="flex-1 transition-colors duration-300"
          style={{
            background: i < filled ? color : "rgba(232,230,216,0.10)",
          }}
        />
      ))}
    </div>
  );
}

/**
 * Gacha Wave B MP bar — blue, segmented like the HP bar so the player reads
 * "second resource of the same shape" at a glance. Drains on skill use and
 * refills +10 per round end (the orchestrator emits `mp` WS events that the
 * encounter stream patches into combatant.mp).
 */
function MpBar({ mp, max_mp }: { mp: number; max_mp: number }) {
  const segs = 10;
  const pct = max_mp > 0 ? Math.max(0, Math.min(1, mp / max_mp)) : 0;
  const filled = Math.round(pct * segs);
  return (
    <div className="flex gap-[2px] h-1.5">
      {Array.from({ length: segs }).map((_, i) => (
        <div
          key={i}
          className="flex-1 transition-colors duration-300"
          style={{
            background: i < filled ? "var(--accent)" : "rgba(232,230,216,0.10)",
          }}
        />
      ))}
    </div>
  );
}

function CombatantCard({
  c,
  isLead,
  isActive,
  floatDmg,
  floatKey,
}: {
  c: CombatantState;
  isLead: boolean;
  isActive?: boolean;
  floatDmg: number | null;
  floatKey?: number | string;
}) {
  const roleColor = c.role === "party" ? "var(--party)" : "var(--enemy)";
  const side = combatantSide(c);
  return (
    <div
      className="pixel-panel p-2 min-w-[150px] flex-1 relative transition-shadow"
      style={{
        borderColor: roleColor,
        boxShadow: isActive ? `0 0 0 2px ${roleColor}, 3px 3px 0 #000` : undefined,
        opacity: isActive === false ? 0.78 : 1,
      }}
    >
      {isActive && (
        <div
          className="absolute -top-2 left-1 font-hud text-[8px] px-1"
          style={{ background: roleColor, color: "#000" }}
        >
          ACTIVE
        </div>
      )}
      {floatDmg != null && floatDmg > 0 && (
        <div
          key={`dmg-${floatKey ?? 0}`}
          className="dmg-float font-display absolute right-2 top-1 text-lg"
          style={{ color: "var(--danger)" }}
        >
          -{floatDmg}
        </div>
      )}
      <div className="flex items-center justify-between">
        <span className="font-hud text-[10px]" style={{ color: roleColor }}>
          {c.role === "party" ? "PARTY" : "ENEMY"}
          {isLead && " ★"}
        </span>
        <span
          className="font-hud text-[9px] px-1"
          style={{ background: typeColor(c.type), color: "#000" }}
        >
          {c.type}
        </span>
      </div>
      {/* Debate stance: FOR (player side) vs AGAINST (enemy side). */}
      <div
        className="font-hud text-[9px] px-1 inline-block mt-1"
        style={{ background: sideColor(side), color: "#000" }}
        title={side === "for" ? "Arguing FOR the topic" : "Arguing AGAINST the topic"}
      >
        {sideLabel(side)}
      </div>
      <div className="font-hud text-sm truncate mt-0.5">{c.name}</div>
      <div className="font-body text-[11px] mb-1" style={{ color: "var(--muted)" }}>
        {c.hp}/{c.max_hp} HP
      </div>
      <HpBar hp={c.hp} max_hp={c.max_hp} />
      {/* Gacha Wave B: MP bar (blue) under HP. Renders only when the backend
          has populated MP on this combatant — older snapshots stay HP-only. */}
      {typeof c.mp === "number" && typeof c.max_mp === "number" && (
        <div className="mt-1.5">
          <div
            className="font-body text-[10px] mb-0.5 flex items-center justify-between"
            style={{ color: "var(--muted)" }}
          >
            <span style={{ color: "var(--accent)" }}>MP</span>
            <span>{c.mp}/{c.max_mp}</span>
          </div>
          <MpBar mp={c.mp} max_mp={c.max_mp} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Typewriter (newest utterance types out char-by-char)
// ---------------------------------------------------------------------------

function useTypewriter(text: string, active: boolean, speed = 18) {
  const [shown, setShown] = useState(active ? "" : text);
  useEffect(() => {
    if (!active) {
      setShown(text);
      return;
    }
    setShown("");
    let i = 0;
    const id = setInterval(() => {
      i++;
      setShown(text.slice(0, i));
      if (i >= text.length) clearInterval(id);
    }, speed);
    return () => clearInterval(id);
  }, [text, active, speed]);
  return shown;
}

function UtteranceBubble({
  u,
  liveNames,
  isNewest,
}: {
  u: Utterance;
  liveNames: Record<string, string>;
  isNewest: boolean;
}) {
  const isParty = u.actor_role === "party";
  const isJudge = u.actor_role === "judge";
  const color = isJudge ? "var(--accent)" : isParty ? "var(--party)" : "var(--enemy)";
  const text = useTypewriter(u.text, isNewest);
  const actorName = liveNames[u.actor_id] ?? u.actor_id;
  // Debaters carry a stance; the judge is neutral. Side comes from the optional
  // backend hint on the utterance, falling back to role (party=FOR, enemy=AGAINST).
  const rawSide = (u as { side?: string }).side;
  const turnSide: DebateSide | null = isJudge
    ? null
    : rawSide === "for" || rawSide === "against"
      ? rawSide
      : isParty
        ? "for"
        : "against";

  return (
    <div
      className="pixel-panel p-2 text-sm"
      style={{ borderColor: color, boxShadow: "3px 3px 0 #000" }}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className="font-hud text-[10px]" style={{ color }}>
          {actorName}
        </span>
        {turnSide && (
          <span
            className="font-hud text-[8px] px-1"
            style={{ background: sideColor(turnSide), color: "#000" }}
          >
            {sideLabel(turnSide)}
          </span>
        )}
        {u.skill_used && (
          <span className="font-hud text-[9px] px-1" style={{ background: "rgba(255,255,255,0.1)" }}>
            {u.skill_used}
          </span>
        )}
        <span className="ml-auto font-hud text-[9px]" style={{ color: "var(--muted)" }}>
          T{u.turn}
        </span>
      </div>
      <p className="font-body leading-relaxed whitespace-pre-wrap" style={{ color: "var(--ink)" }}>
        {text}
        {isNewest && text.length < u.text.length && (
          <span className="caret-blink">▋</span>
        )}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Verdict strike — gold score punches in
// ---------------------------------------------------------------------------

function SubScore({ label, value }: { label: string; value: number }) {
  return (
    <span className="font-hud text-[9px] inline-flex items-center gap-1">
      <span style={{ color: "var(--muted)" }}>{label}</span>
      <span style={{ color: "var(--ink)" }}>{Math.round(value)}</span>
    </span>
  );
}

function VerdictBadge({ v, fresh }: { v: JudgeVerdict; fresh: boolean }) {
  const positive = v.score >= 50;
  // Prefer the punchy one-liner (`why`) as the headline; `rationale` is the
  // fuller explanation shown beneath it.
  const headline = v.why?.trim();
  const detail = v.rationale?.trim();
  return (
    <div
      className="pixel-inset p-2"
      style={{ borderColor: positive ? "var(--win)" : "var(--danger)" }}
    >
      <div className="flex items-baseline gap-2">
        <span
          className={`font-display text-lg ${fresh ? "score-punch" : ""}`}
          style={{ color: "var(--accent)" }}
        >
          {Math.round(v.score)}
        </span>
        <span className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
          T{v.turn}
        </span>
        <span
          className="font-hud text-[9px] ml-auto"
          style={{ color: v.damage > 0 ? "var(--danger)" : "var(--muted)" }}
        >
          -{v.damage} HP
        </span>
      </div>

      {/* Logic / persuasion sub-scores (optional additive judge fields). */}
      {(typeof v.logic === "number" || typeof v.persuasion === "number") && (
        <div className="flex gap-3 mt-1">
          {typeof v.logic === "number" && <SubScore label="LOGIC" value={v.logic} />}
          {typeof v.persuasion === "number" && (
            <SubScore label="PERSUASION" value={v.persuasion} />
          )}
        </div>
      )}

      {headline && (
        <p className="font-body text-[11px] mt-1" style={{ color: "var(--ink)" }}>
          {headline}
        </p>
      )}
      {detail && (
        <p
          className="font-body text-[11px] mt-1 italic"
          style={{ color: "var(--muted)" }}
        >
          {detail}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Memory Recall overlay (Wave C: the headline ability)
// ---------------------------------------------------------------------------
//
// Full-screen overlay shown for ~4 seconds after the player spends 60 MP on
// Memory Recall. Three layers, top to bottom:
//   * the literal Redis key `enc:{eid}:transcript` in monospace,
//   * the 5 most-recent transcript lines scrolling in (the matched line glows),
//   * the counter_text typed out letter-by-letter via a simple setInterval.
//
// Damage is delivered via the per-card `floatByTarget` damage-number animation
// already wired into `CombatantCard` — this overlay does not have to render it.
// ---------------------------------------------------------------------------

function MemoryRecallOverlay({
  encounterId,
  result,
  onClose,
}: {
  encounterId: string;
  result: MemoryRecallResult;
  onClose: () => void;
}) {
  // Typewriter for the counter text — simple setInterval, dies on unmount or
  // when the text changes (so a back-to-back recall replays cleanly).
  const [typed, setTyped] = useState("");
  useEffect(() => {
    setTyped("");
    const text = result.counter_text ?? "";
    if (!text) return;
    let i = 0;
    const id = setInterval(() => {
      i++;
      setTyped(text.slice(0, i));
      if (i >= text.length) clearInterval(id);
    }, MEMORY_RECALL_TYPE_SPEED_MS);
    return () => clearInterval(id);
  }, [result.counter_text]);

  // Auto-dismiss after the overlay window. Player can also click to close.
  useEffect(() => {
    const id = setTimeout(onClose, MEMORY_RECALL_OVERLAY_MS);
    return () => clearTimeout(id);
  }, [onClose]);

  const slice = result.transcript_slice ?? [];
  const highlightedLine = result.highlighted_line ?? "";

  return (
    <div
      className="absolute inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.82)" }}
      onClick={onClose}
      role="dialog"
      aria-label="Memory Recall"
    >
      <div
        className="pixel-panel p-4 max-w-2xl w-[90%] space-y-3"
        style={{ borderColor: "var(--accent)", boxShadow: "4px 4px 0 #000" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Top: the literal Redis key the cache was peeked from. */}
        <div
          className="font-mono text-[11px] px-2 py-1"
          style={{
            background: "rgba(255,255,255,0.06)",
            color: "var(--accent)",
            borderLeft: "3px solid var(--accent)",
          }}
        >
          GET enc:{encounterId}:transcript
        </div>

        {/* Middle: the 5 most-recent transcript lines, with the highlighted
            one glowing. Lines scroll in via a CSS-cheap stagger. */}
        <div className="space-y-1">
          <div
            className="font-hud text-[9px] uppercase tracking-wider"
            style={{ color: "var(--muted)" }}
          >
            Transcript slice (last {slice.length})
          </div>
          {slice.length === 0 && (
            <div
              className="font-body text-[12px] italic"
              style={{ color: "var(--muted)" }}
            >
              (cache miss — no transcript lines yet)
            </div>
          )}
          {slice.map((line, i) => {
            const isHighlighted =
              !!highlightedLine && line.toLowerCase().includes(highlightedLine.toLowerCase());
            return (
              <div
                key={`mr-line-${i}`}
                className="font-mono text-[11px] px-2 py-1 transition-colors"
                style={{
                  background: isHighlighted ? "rgba(255,222,89,0.18)" : "transparent",
                  color: isHighlighted ? "var(--accent)" : "var(--ink)",
                  borderLeft: isHighlighted ? "3px solid var(--accent)" : "3px solid transparent",
                  opacity: 0,
                  animation: `mr-line-in 280ms ease-out ${i * 90}ms forwards`,
                  textShadow: isHighlighted ? "0 0 8px var(--accent)" : undefined,
                }}
              >
                {line}
              </div>
            );
          })}
        </div>

        {/* Bottom: typewritten counter in the coach's voice. */}
        <div className="pixel-inset p-2" style={{ borderColor: "var(--party)" }}>
          <div
            className="font-hud text-[9px] mb-1"
            style={{ color: "var(--party)" }}
          >
            COUNTER
          </div>
          <p
            className="font-body text-[13px] leading-relaxed"
            style={{ color: "var(--ink)" }}
          >
            {typed}
            {typed.length < (result.counter_text ?? "").length && (
              <span className="caret-blink">▋</span>
            )}
          </p>
        </div>

        <div
          className="flex items-center justify-between font-hud text-[9px] pt-1"
          style={{ color: "var(--muted)" }}
        >
          <span>
            -{result.mp_spent} MP &middot; {result.mp_remaining} MP left
          </span>
          <span style={{ color: result.damage > 0 ? "var(--danger)" : "var(--muted)" }}>
            -{result.damage} HP
          </span>
        </div>
      </div>

      {/* Local keyframes for the line-in stagger; cheap, no CSS module needed. */}
      <style>{`
        @keyframes mr-line-in {
          0% { opacity: 0; transform: translateX(-6px); }
          100% { opacity: 1; transform: translateX(0); }
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Estimate badge (A4 optimistic judge) — instant heuristic score, "estimating…"
// state. NO damage shown: HP only changes when the real verdict settles this.
// ---------------------------------------------------------------------------

function EstimateBadge({ score, turn }: { score: number; turn: number }) {
  return (
    <div
      className="pixel-inset p-2 caret-blink"
      style={{ borderColor: "var(--accent)", borderStyle: "dashed" }}
      title="Instant estimate — the judge is still scoring this argument"
    >
      <div className="flex items-baseline gap-2">
        <span className="font-display text-lg" style={{ color: "var(--accent)" }}>
          ~{Math.round(score)}
        </span>
        <span className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
          T{turn}
        </span>
        <span className="font-hud text-[9px] ml-auto" style={{ color: "var(--muted)" }}>
          estimating…
        </span>
      </div>
      <p className="font-body text-[11px] mt-1 italic" style={{ color: "var(--muted)" }}>
        Optimistic score — settling to the judge's verdict, HP unchanged until then.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function BattleDebateView() {
  const { activeEncounterId, runId, topic: runTopic, playerName, setEncounter, setYouScores, setBattleLocked } =
    useGame();
  const { transition } = useIrisTransition();
  const {
    status,
    encounter,
    transcript,
    verdicts,
    capturableIds,
    mpInsufficient,
    estimates,
    running,
    runningTurn,
    drive,
    argue,
  } = useEncounterStream(activeEncounterId);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [summonOpen, setSummonOpen] = useState(false);
  const [summonNotice, setSummonNotice] = useState<string | null>(null);

  // Player input
  const [argText, setArgText] = useState("");
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [skills, setSkills] = useState<ParsedSkill[]>([]);
  const [leadId, setLeadId] = useState<string | null>(null);
  const [captureFlash, setCaptureFlash] = useState(false);

  // Pre-fight intro card: theme reveal → Pro/Con pick → countdown → "Debate!!".
  // `introDone` gates all argue/input until the card dismisses; `chosenStance`
  // is the player's Pro/Con pick, used for local labels and threaded into argue.
  const [introDone, setIntroDone] = useState(false);
  const [chosenStance, setChosenStance] = useState<DebateSide | null>(null);

  // Argue Copilot — coach the player's draft before they send it.
  const [coaching, setCoaching] = useState(false);
  const [coachError, setCoachError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<AssistSuggestion[]>([]);

  // Memory Recall (Wave C). Tracks whether a recall request is in flight, the
  // last result (drives the overlay), and a per-target damage float so the
  // existing CombatantCard animation surfaces the recall's HP delta.
  const [recalling, setRecalling] = useState(false);
  const [recallError, setRecallError] = useState<string | null>(null);
  const [recallResult, setRecallResult] = useState<MemoryRecallResult | null>(null);
  const [recallFloat, setRecallFloat] = useState<{ targetId: string; dmg: number; key: number } | null>(null);

  // Retro SFX (spec §7 stretch) — mute toggle + edge-detection refs.
  const [sfxOn, setSfxOn] = useState(true);
  const prevVerdictCount = useRef(0);
  const playedEndSfx = useRef(false);

  // DOM refs for damage-fly animation (judge → HP corner).
  const judgePanelRef = useRef<HTMLDivElement>(null);
  const playerHpRef = useRef<HTMLDivElement>(null);
  const enemyHpRef = useRef<HTMLDivElement>(null);

  // Active damage numeral fly: spawned at judge center, flies to HP corner.
  const [activeDmgFly, setActiveDmgFly] = useState<{
    fromX: number; fromY: number; dx: number; dy: number; damage: number; key: number;
  } | null>(null);
  const prevVerdictForFly = useRef<JudgeVerdict | null>(null);

  const combatants: CombatantState[] = useMemo(
    () => encounter?.combatants ?? [],
    [encounter]
  );

  // Lead party monster = chosen avatar first, then the old stat fallback.
  const leadParty = useMemo(() => {
    const party = combatants.filter((c) => c.role === "party");
    return (
      party.find((c) => c.is_avatar) ??
      (party.length ? party.slice().sort((a, b) => b.max_hp - a.max_hp)[0] : null)
    );
  }, [combatants]);

  // Lead enemy (first living enemy, else first enemy) — used as the defender for
  // skill type-effectiveness ("super effective" vs the current opponent).
  const leadEnemy = useMemo(() => {
    const enemies = combatants.filter((c) => c.role === "enemy");
    return enemies.find((c) => c.hp > 0) ?? enemies[0] ?? null;
  }, [combatants]);

  const displayedCombatants = useMemo(() => {
    const leadId = leadParty?.monster_id;
    return combatants.slice().sort((a, b) => {
      const rank = (c: CombatantState) =>
        c.role === "party" ? (c.monster_id === leadId ? 0 : 1) : 2;
      return rank(a) - rank(b);
    });
  }, [combatants, leadParty?.monster_id]);

  // Fetch the player's party once to source the lead monster's skills.
  useEffect(() => {
    if (!runId) return;
    api
      .get<
        Array<{
          id: string;
          type: string;
          level: number;
          skills: unknown[];
          is_avatar?: boolean;
        }>
      >(`/api/runs/${runId}/party`)
      .then((party) => {
        if (!party.length) return;
        // Lead = selected avatar when present; otherwise highest level.
        const lead =
          party.find((m) => m.is_avatar) ??
          party.slice().sort((a, b) => (b.level ?? 0) - (a.level ?? 0))[0];
        setLeadId(lead.id);
        setSkills(parseSkills(lead.skills));
      })
      .catch(() => {
        /* skills are optional — text-only still works */
      });
  }, [runId]);

  // Live "You" reasoning series: player verdicts (target = a party monster).
  const partyIds = useMemo(
    () => new Set(combatants.filter((c) => c.role === "party").map((c) => c.monster_id)),
    [combatants]
  );
  const youSeries: TrendSeries = useMemo(() => {
    const pts = verdicts.filter((v) => partyIds.has(v.target)).map((v) => v.score);
    return { label: playerName, color: "var(--party)", points: pts };
  }, [verdicts, partyIds, playerName]);

  // Publish the player's curve so the training screen can show it beside the agent.
  useEffect(() => {
    if (youSeries.points.length) setYouScores(youSeries.points);
  }, [youSeries, setYouScores]);

  // A4: pending optimistic estimates that have NOT yet been settled by a
  // verdict. The hook already retires an estimate when its verdict lands, but we
  // also guard here against any (turn, actor) already represented in `verdicts`
  // so the "estimating…" badge never lingers next to its real score.
  const pendingEstimates = useMemo(() => {
    return Object.values(estimates)
      .filter(
        (e) =>
          !verdicts.some((v) => v.turn === e.turn && v.actor_id === e.actor_id)
      )
      .sort((a, b) => b.turn - a.turn);
  }, [estimates, verdicts]);

  // Per-card floating damage: latest verdict's damage keyed by target id. A
  // freshly-cast Memory Recall transiently overrides the verdict damage on the
  // target it hit so the player sees the recall damage float without waiting
  // for the next judge verdict.
  const lastVerdict = verdicts[verdicts.length - 1];
  const floatByTarget: Record<string, number> = {};
  if (lastVerdict) floatByTarget[lastVerdict.target] = lastVerdict.damage;
  if (recallFloat) floatByTarget[recallFloat.targetId] = recallFloat.dmg;
  const floatKey = recallFloat ? `recall-${recallFloat.key}` : lastVerdict?.turn;

  // Auto-scroll transcript
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript.length]);

  // Clear the transient Memory Recall damage float after it finishes animating,
  // so the next judge verdict isn't shadowed by a stale recall number.
  useEffect(() => {
    if (!recallFloat) return;
    const id = setTimeout(() => setRecallFloat(null), 1800);
    return () => clearTimeout(id);
  }, [recallFloat]);

  // SFX: punchy "hit" whenever a NEW verdict lands (brighter on a good score).
  useEffect(() => {
    if (verdicts.length > prevVerdictCount.current) {
      const latest = verdicts[verdicts.length - 1];
      if (latest) sfxHit(latest.score >= 50);
    }
    prevVerdictCount.current = verdicts.length;
  }, [verdicts.length, verdicts]);

  const liveNames: Record<string, string> = { judge: "Judge" };
  for (const c of combatants) liveNames[c.monster_id] = c.name;

  // Damage fly: fire when a new verdict with damage arrives.
  useEffect(() => {
    if (!lastVerdict || lastVerdict === prevVerdictForFly.current || !lastVerdict.damage) return;
    prevVerdictForFly.current = lastVerdict;
    const judgeEl = judgePanelRef.current;
    const targetIsParty = partyIds.has(lastVerdict.target);
    const targetEl = targetIsParty ? playerHpRef.current : enemyHpRef.current;
    if (!judgeEl || !targetEl) return;
    const from = judgeEl.getBoundingClientRect();
    const to = targetEl.getBoundingClientRect();
    setActiveDmgFly({
      fromX: from.left + from.width / 2,
      fromY: from.top + from.height / 2,
      dx: (to.left + to.width / 2) - (from.left + from.width / 2),
      dy: (to.top + to.height / 2) - (from.top + from.height / 2),
      damage: lastVerdict.damage,
      key: Date.now(),
    });
    const id = setTimeout(() => setActiveDmgFly(null), 900);
    return () => clearTimeout(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastVerdict]);

  // Derived: latest utterance per role for speech bubbles + recap line.
  const latestPlayerUtterance = useMemo(
    () => [...transcript].reverse().find((u) => u.actor_role === "party") ?? null,
    [transcript]
  );
  const latestEnemyUtterance = useMemo(
    () => [...transcript].reverse().find((u) => u.actor_role === "enemy") ?? null,
    [transcript]
  );
  const latestEnemyLine = latestEnemyUtterance?.text ?? null;
  const newestInTranscript = transcript[transcript.length - 1] ?? null;
  const playerIsNewest = latestPlayerUtterance === newestInTranscript;
  const enemyIsNewest = latestEnemyUtterance === newestInTranscript;

  const phase = encounter?.phase ?? "intro";
  const isCapturable = phase === "capturable";
  const isOver = phase === "won" || phase === "lost";
  // Input is gated on the intro card finishing (introDone) AND the phase.
  const canArgue =
    introDone && (phase === "debating" || phase === "intro" || phase === "capturable");

  // Battle isolation: lock the global nav while the battle is live, release it
  // the moment it resolves (won/lost) so the post-battle "Leave" can navigate.
  useEffect(() => {
    setBattleLocked(!!activeEncounterId && !isOver);
  }, [activeEncounterId, isOver, setBattleLocked]);

  // Player's debate side — the intro-card pick (chosenStance) wins for local
  // labels; falls back to the backend hint / role inference before a pick.
  const playerSide: DebateSide =
    chosenStance ?? (leadParty ? combatantSide(leadParty) : "for");

  // Active-turn indicator. While a round is running, infer who is "speaking"
  // from the newest transcript line; otherwise it's the player's move.
  const lastActorRole = transcript.length
    ? transcript[transcript.length - 1].actor_role
    : null;
  const turnIndicator: { label: string; color: string } = isOver
    ? { label: phase === "won" ? "Victory" : "Defeat", color: phase === "won" ? "var(--win)" : "var(--danger)" }
    : running
      ? lastActorRole === "party"
        ? { label: "Enemy arguing…", color: "var(--enemy)" }
        : { label: "Debating…", color: "var(--accent)" }
      : { label: "Your turn", color: "var(--party)" };

  // SFX: win/lose jingle once when the battle ends.
  useEffect(() => {
    if (playedEndSfx.current) return;
    if (phase === "won") {
      playedEndSfx.current = true;
      sfxWin();
    } else if (phase === "lost") {
      playedEndSfx.current = true;
      sfxLose();
    }
  }, [phase]);

  function submitArgument() {
    const text = argText.trim();
    if (!text || busy || isOver || running || !introDone) return;
    setActionError(null);
    sfxSubmit();
    // Thread the player's chosen Pro/Con stance additively (frontend-only seam).
    argue(text, selectedSkill, chosenStance);
    setArgText("");
  }

  // Ask the coach (lead party monster) to improve the current draft. Empty
  // drafts are allowed — the coach can argue from scratch.
  async function improveArgument() {
    if (!activeEncounterId || coaching) return;
    setCoachError(null);
    setCoaching(true);
    sfxBlip();
    try {
      const result = await api.post<AssistResult>(
        `/api/encounters/${activeEncounterId}/assist`,
        { draft: argText.trim(), skill_id: selectedSkill ?? undefined }
      );
      setSuggestions(result.suggestions ?? []);
      if (!result.suggestions?.length) {
        setCoachError("coach had nothing to add — try a draft");
      }
    } catch {
      setCoachError("coach is offline — try again");
    } finally {
      setCoaching(false);
    }
  }

  // Memory Recall (Wave C): spend 60 MP, surface the Redis transcript on screen,
  // and counter the highlighted enemy line in the lead party monster's voice.
  // Damage is delivered through the existing per-card floating-damage animation
  // by stashing it onto `recallFloat` so `floatByTarget` can pick it up.
  async function castMemoryRecall() {
    if (!activeEncounterId || recalling || isOver || running) return;
    if (!leadEnemy || !leadParty) return;
    setRecallError(null);
    setRecalling(true);
    sfxBlip();
    try {
      const result = await api.post<MemoryRecallResult>(
        `/api/encounters/${activeEncounterId}/memory-recall`,
        {}
      );
      setRecallResult(result);
      if (result.damage > 0) {
        sfxHit(true);
        setRecallFloat({
          targetId: leadEnemy.monster_id,
          dmg: result.damage,
          key: Date.now(),
        });
      }
    } catch (e) {
      setRecallError(e instanceof Error ? e.message : "Memory Recall failed");
    } finally {
      setRecalling(false);
    }
  }

  // Local MP fallback until Wave B's MP map is wired into the WS state. We
  // assume the coach has at least max_mp (60) so the button is enabled in the
  // demo; once Wave B integrates, swap this for the real MP value.
  // (Backend still enforces the actual MP gate, so a button click below the
  // real threshold will surface as `recallError` rather than letting the
  // player cheat.)
  const coachMp = MEMORY_RECALL_MP_COST; // optimistic local fallback
  const canRecall =
    !!leadParty &&
    !!leadEnemy &&
    !isOver &&
    !running &&
    !recalling &&
    coachMp >= MEMORY_RECALL_MP_COST;

  // Adopt a coach suggestion: load it into the textarea, and if its suggested
  // skill matches one of the lead's chips, select that chip too.
  function useSuggestion(s: AssistSuggestion) {
    setArgText(s.improved);
    if (s.skill_id && skills.some((sk) => sk.id === s.skill_id)) {
      setSelectedSkill(s.skill_id);
    }
    setSuggestions([]);
    setCoachError(null);
    sfxSubmit();
  }

  function toggleSfx() {
    setSfxOn((prev) => {
      const next = !prev;
      setSfxEnabled(next);
      return next;
    });
  }

  function handleSummoned(result: SummonResult) {
    setSummonNotice(`${result.figure.name} is ready for the next exchange.`);
    setActionError(null);
  }

  async function restAction(path: string, body?: unknown) {
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
    const wildId = capturableIds[0] ?? combatants.find((c) => c.role === "enemy")?.monster_id;
    if (!wildId || !activeEncounterId) return;
    sfxCapture();
    setCaptureFlash(true);
    setTimeout(() => setCaptureFlash(false), 1200);
    await restAction("/capture", { wild_id: wildId });
  }

  async function handleFlee() {
    // When the battle is already over, just leave — the encounter is finalized.
    if (!isOver) await restAction("/flee");
    // Release the nav lock and return to the overworld (setEncounter(null) sets
    // screen -> "overworld" and clears battleLocked).
    setBattleLocked(false);
    transition(() => setEncounter(null));
  }

  // ---- No active encounter: inviting empty state ----
  if (!activeEncounterId) {
    return (
      <div className="flex-1 grid place-items-center p-6">
        <div className="pixel-panel p-6 text-center max-w-sm">
          <div className="text-4xl mb-3">⚔️</div>
          <div className="font-hud text-sm mb-2">No debate yet</div>
          <div className="font-body text-sm" style={{ color: "var(--muted)" }}>
            Find an opponent in the overworld to start a debate.
          </div>
        </div>
      </div>
    );
  }

  const newestTurn = transcript.length ? transcript[transcript.length - 1] : null;

  // Recap of the opponent's most recent point (shown above the floating input).
  const enemyRecap = latestEnemyLine
    ? latestEnemyLine.length > 140
      ? latestEnemyLine.slice(0, 140) + "…"
      : latestEnemyLine
    : null;

  // -------------------------------------------------------------------------
  // Floating input slot — lives INSIDE the stage (lower-center). Always shows
  // the live status/recap; the argument controls appear on the player's turn.
  // ARGUE is the green primary; meta-actions sit in the slim action bar.
  // -------------------------------------------------------------------------
  const inputSlot = !isOver ? (
    <div
      className="pixel-panel"
      style={{
        background: "rgba(14,16,24,0.96)",
        borderColor: canArgue ? "var(--win)" : "rgba(232,230,216,0.18)",
        padding: "10px 12px",
        maxHeight: "62%",
        overflowY: "auto",
        boxShadow: "4px 4px 0 #000",
      }}
    >
      {/* Status / recap line (always visible) */}
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <span
          className="inline-flex items-center gap-1 font-hud text-[11px] px-1.5 py-0.5"
          style={{ border: `1px solid ${turnIndicator.color}`, color: turnIndicator.color }}
        >
          {running && <span className="caret-blink">●</span>}
          {turnIndicator.label}
          {running && runningTurn != null ? ` · T${runningTurn}` : ""}
        </span>
        <span className="font-hud text-[11px]" style={{ color: "var(--muted)" }}>
          You argue{" "}
          <span style={{ color: sideColor(playerSide) }}>{sideLabel(playerSide)}</span>
        </span>
      </div>
      {enemyRecap && (
        <p
          className="font-body mb-2 leading-snug"
          style={{ color: "var(--ink)", fontSize: 15 }}
        >
          <span className="font-hud text-[10px]" style={{ color: "var(--enemy)" }}>
            REBUTTAL{" "}
          </span>
          {enemyRecap}
        </p>
      )}

      {canArgue ? (
        <>
          {/* Skill chips */}
          {skills.length > 0 && (
            <div className="space-y-1 mb-2">
              <div className="flex gap-1.5 flex-wrap">
                {skills.map((s) => {
                  const active = selectedSkill === s.id;
                  const eff = effectivenessInfo(s.type, leadEnemy?.type);
                  const leadMp =
                    typeof leadParty?.mp === "number" ? leadParty.mp : Infinity;
                  const cost = Number(s.mp_cost ?? 0);
                  const unaffordable = cost > 0 && leadMp < cost;
                  const tip = `${skillTooltip(s)}${
                    eff.label ? ` vs ${leadEnemy?.type ?? "enemy"}: ${eff.label} (×${eff.multiplier})` : ""
                  }${cost > 0 ? ` · MP ${cost}` : ""}${
                    unaffordable ? ` (not enough MP: ${leadMp}/${cost})` : ""
                  }`;
                  return (
                    <button
                      key={s.id}
                      title={tip}
                      onClick={() => setSelectedSkill(active ? null : s.id)}
                      disabled={running || isOver || unaffordable}
                      className="pixel-btn text-[10px] py-1 relative"
                      style={
                        active
                          ? { background: typeColor(s.type), color: "#000", borderColor: "#000" }
                          : { borderColor: typeColor(s.type), opacity: unaffordable ? 0.45 : 1 }
                      }
                    >
                      {s.name} ×{s.power}
                      {eff.label && (
                        <span
                          className="ml-1 font-hud text-[8px]"
                          style={{ color: active ? "#000" : eff.color }}
                        >
                          {eff.multiplier > 1 ? "▲" : "▼"}
                        </span>
                      )}
                      {cost > 0 && (
                        <span
                          className="ml-1 font-hud text-[8px] px-1"
                          style={{
                            background: "rgba(0,0,0,0.35)",
                            color: active ? "#000" : "var(--accent)",
                            border: "1px solid var(--accent)",
                          }}
                        >
                          {cost} MP
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
              {mpInsufficient && (
                <div className="font-hud text-[9px] px-1" style={{ color: "var(--danger)" }}>
                  {mpInsufficient.detail}
                </div>
              )}
              {selectedSkill && leadEnemy && (() => {
                const sel = skills.find((s) => s.id === selectedSkill);
                if (!sel) return null;
                const eff = effectivenessInfo(sel.type, leadEnemy.type);
                if (!eff.label) {
                  return (
                    <div className="font-hud text-[9px] px-1" style={{ color: "var(--muted)" }}>
                      {sel.type || "—"} vs {leadEnemy.type}: Neutral (×1)
                    </div>
                  );
                }
                return (
                  <div className="font-hud text-[9px] px-1" style={{ color: eff.color }}>
                    {sel.type} vs {leadEnemy.type}: {eff.label} (×{eff.multiplier})
                  </div>
                );
              })()}
            </div>
          )}

          {/* Textarea + primary ARGUE */}
          <div className="flex gap-2 items-end">
            <textarea
              className="pixel-field flex-1 font-body resize-none h-16"
              style={{ fontSize: 16 }}
              placeholder="Make your argument…"
              value={argText}
              onChange={(e) => setArgText(e.target.value)}
              onKeyDown={(e) => {
                if (!e.metaKey && !e.ctrlKey && !e.altKey) sfxBlip();
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitArgument();
              }}
            />
            <div className="flex flex-col gap-1.5">
              <button
                className="pixel-btn text-[10px]"
                disabled={!canArgue || coaching || isOver || running}
                onClick={improveArgument}
                title="Ask your coach to improve this argument"
              >
                {coaching ? "Coach…" : "✨ Improve"}
              </button>
              <button
                className="pixel-btn font-display"
                style={{ background: "var(--win)", color: "#000", borderColor: "#000" }}
                disabled={!argText.trim() || isOver || running}
                onClick={submitArgument}
                title={running ? "Wait for the current round to finish" : "Submit your argument"}
              >
                {running ? "Debating…" : "ARGUE"}
              </button>
            </div>
          </div>

          {/* Coach feedback */}
          {coaching && (
            <div
              className="pixel-inset p-2 font-body text-[11px] flex items-center gap-2 mt-2"
              style={{ borderColor: "var(--party)", color: "var(--muted)" }}
            >
              <span className="caret-blink">▋</span>
              Your coach is thinking…
            </div>
          )}
          {!coaching && coachError && (
            <div
              className="pixel-inset p-2 font-body text-[11px] mt-2"
              style={{ borderColor: "var(--warn)", color: "var(--warn)" }}
            >
              {coachError}
            </div>
          )}
          {!coaching &&
            suggestions.map((s, i) => (
              <div
                key={`sugg-${i}`}
                className="pixel-inset p-2 space-y-1.5 mt-2"
                style={{ borderColor: "var(--party)" }}
              >
                <div className="flex items-center gap-2">
                  <span className="font-hud text-[9px]" style={{ color: "var(--party)" }}>
                    ✨ COACH
                  </span>
                  {s.skill_id && (
                    <span
                      className="font-hud text-[9px] px-1"
                      style={{
                        background: typeColor(skills.find((sk) => sk.id === s.skill_id)?.type),
                        color: "#000",
                      }}
                    >
                      {s.skill_id}
                    </span>
                  )}
                  {s.angle && (
                    <span className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
                      {s.angle}
                    </span>
                  )}
                  <button
                    className="pixel-btn pixel-btn--party text-[9px] py-0.5 ml-auto"
                    onClick={() => useSuggestion(s)}
                  >
                    Use this
                  </button>
                </div>
                <p
                  className="font-body text-[13px] leading-relaxed whitespace-pre-wrap"
                  style={{ color: "var(--ink)" }}
                >
                  {s.improved}
                </p>
                {s.rationale && (
                  <p className="font-body text-[10px] italic" style={{ color: "var(--muted)" }}>
                    {s.rationale}
                  </p>
                )}
              </div>
            ))}
        </>
      ) : running ? (
        <div className="font-body text-[13px] flex items-center gap-2" style={{ color: "var(--muted)" }}>
          <span className="caret-blink">▋</span>
          The judge and your opponent are thinking…
        </div>
      ) : null}
    </div>
  ) : null;

  return (
    <div className="flex flex-col h-full overflow-hidden relative" style={{ background: "var(--bg)" }}>
      <LevelUpOverlay combatants={combatants} />
      <SummonOverlay
        runId={runId}
        open={summonOpen}
        topic={encounter?.topic ?? runTopic}
        onClose={() => setSummonOpen(false)}
        onSummoned={handleSummoned}
      />
      {captureFlash && (
        <div
          className="capture-flash absolute inset-0 z-50 pointer-events-none"
          style={{ background: "var(--accent)" }}
        />
      )}
      {recallResult && activeEncounterId && (
        <MemoryRecallOverlay
          encounterId={activeEncounterId}
          result={recallResult}
          onClose={() => setRecallResult(null)}
        />
      )}

      {/* Pre-fight intro card: theme → Pro/Con pick → countdown → Debate!!.
          Mounted once combatants exist; blocks input via `introDone` until it
          dismisses. The chosen stance is stored for local labels + argue(). */}
      {!introDone && combatants.length > 0 && (
        <BattleIntroCard
          topic={encounter?.topic ?? runTopic}
          onComplete={(s) => {
            setChosenStance(s);
            setIntroDone(true);
          }}
        />
      )}

      {/* Damage numeral flies from the judge panel to the target HP corner. */}
      {activeDmgFly && (
        <div
          style={{
            position: "fixed",
            left: activeDmgFly.fromX,
            top: activeDmgFly.fromY,
            "--fly-dx": `${activeDmgFly.dx}px`,
            "--fly-dy": `${activeDmgFly.dy}px`,
            animation: "dmg-fly-to-corner 0.82s ease-in-out forwards",
            zIndex: 30,
            color: "var(--danger)",
            fontSize: 22,
            fontFamily: "var(--font-display)",
            textShadow: "2px 2px 0 #000",
            pointerEvents: "none",
          } as React.CSSProperties}
        >
          -{activeDmgFly.damage}
        </div>
      )}

      {/* THREE FULL-HEIGHT COLUMNS: Log | Stage | Judge */}
      <div className="flex flex-1 overflow-hidden min-h-0">
        {/* LEFT — full-height debate log (to the very top) */}
        <div
          className="shrink-0 flex flex-col overflow-hidden"
          style={{
            width: "24%",
            minWidth: 220,
            borderRight: "2px solid rgba(232,230,216,0.10)",
            background: "rgba(14,16,24,0.92)",
          }}
        >
          <BattleLog transcript={transcript} liveNames={liveNames} newestTurn={newestTurn} />
        </div>

        {/* CENTER — stage hosts HP clusters + floating input as children */}
        <div className="flex-1 min-w-0 flex flex-col overflow-hidden">
          <BattleStage
            playerUtterance={latestPlayerUtterance}
            enemyUtterance={latestEnemyUtterance}
            playerIsNewest={playerIsNewest}
            enemyIsNewest={enemyIsNewest}
            leadPartyName={leadParty?.name ?? null}
            leadEnemyName={leadEnemy?.name ?? null}
            leadPartyType={leadParty?.type ?? null}
            leadEnemyType={leadEnemy?.type ?? null}
            topic={encounter?.topic ?? runTopic}
            isOver={isOver}
            phase={phase}
            playerHpSlot={
              <HPCorner
                ref={playerHpRef}
                side="player"
                combatant={leadParty}
                floatDmg={leadParty ? (floatByTarget[leadParty.monster_id] ?? null) : null}
                floatKey={floatKey}
              />
            }
            enemyHpSlot={
              <HPCorner
                ref={enemyHpRef}
                side="enemy"
                combatant={leadEnemy}
                floatDmg={leadEnemy ? (floatByTarget[leadEnemy.monster_id] ?? null) : null}
                floatKey={floatKey}
              />
            }
            inputSlot={inputSlot}
          />
        </div>

        {/* RIGHT — big focal judge panel, full height */}
        <div
          className="shrink-0 flex flex-col overflow-hidden"
          style={{
            width: "26%",
            minWidth: 240,
            borderLeft: "2px solid rgba(232,230,216,0.10)",
          }}
        >
          <JudgePanel
            ref={judgePanelRef}
            lastVerdict={lastVerdict ?? null}
            pendingEstimates={pendingEstimates}
            youSeries={youSeries}
            recentVerdicts={verdicts}
          />
        </div>
      </div>

      {/* SLIM ACTION BAR — meta actions (secondary/ghost); ARGUE lives in stage */}
      <div
        className="px-3 flex items-center gap-2 flex-wrap shrink-0"
        style={{
          height: 56,
          borderTop: "2px solid rgba(232,230,216,0.12)",
          background: "rgba(14,16,24,0.95)",
        }}
      >
        {actionError && (
          <span className="font-body text-[11px] flex-1" style={{ color: "var(--danger)" }}>
            {actionError}
          </span>
        )}
        {summonNotice && !actionError && (
          <span className="font-body text-[11px] flex-1" style={{ color: "var(--party)" }}>
            {summonNotice}
          </span>
        )}

        <button
          className="pixel-btn text-[10px]"
          onClick={toggleSfx}
          title={sfxOn ? "Mute sound effects" : "Unmute sound effects"}
        >
          {sfxOn ? "🔊" : "🔇"}
        </button>
        <button
          className="pixel-btn"
          disabled={isOver || running}
          onClick={() => drive(1)}
          title={running ? "A round is already running" : "Run one autonomous round"}
        >
          {running ? "Debating…" : "Next Round"}
        </button>
        <button
          className="pixel-btn"
          disabled={isOver || running}
          onClick={() => drive(3)}
          title={running ? "A round is already running" : "Run three autonomous rounds"}
        >
          Auto (3)
        </button>
        <button
          className="pixel-btn"
          disabled={!canRecall}
          onClick={castMemoryRecall}
          title={
            !canRecall
              ? `Memory Recall needs ${MEMORY_RECALL_MP_COST} MP`
              : "Peek the encounter transcript and counter the enemy's strongest line"
          }
        >
          {recalling ? "Recalling…" : `Memory Recall (${MEMORY_RECALL_MP_COST} MP)`}
        </button>
        {recallError && (
          <span className="font-body text-[11px]" style={{ color: "var(--danger)" }}>
            {recallError}
          </span>
        )}
        <button
          className="pixel-btn"
          disabled={!runId || isOver || running}
          onClick={() => setSummonOpen(true)}
        >
          Summon
        </button>
        {isCapturable && (
          <button className="pixel-btn pixel-btn--accent" disabled={busy} onClick={handleCapture}>
            Capture
          </button>
        )}
        <button className="pixel-btn ml-auto" disabled={busy} onClick={handleFlee}>
          {isOver ? "Leave" : "Flee"}
        </button>
      </div>
    </div>
  );
}

export default BattleDebateView;
