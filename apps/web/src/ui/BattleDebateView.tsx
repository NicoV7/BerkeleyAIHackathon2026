/**
 * BattleDebateView — live human-argues encounter screen (WS-G §3).
 *
 * The player chooses a party agent: they can type an argument or spend MP on
 * that agent's memory skills, the judge scores it, and the enemy rebuts
 * autonomously. HP/verdict/phase all stream over the WS.
 *
 * Drive model (single path, all over the WS — no REST /turn or /auto):
 *  - Submit Argument  ws.send({action:"argue", text, skill_id, actor_id})
 *  - Next Round       drive(1)   (autonomous showcase / fallback)
 *  - Auto (3)         drive(3)
 *  - Capture          POST /api/encounters/{id}/capture {wild_id}  (when capturable)
 *  - Flee             POST /api/encounters/{id}/flee  then leave
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import {
  parseSkills,
  typeColor,
  skillTooltip,
  effectivenessInfo,
  effectLabel,
  type ParsedSkill,
} from "../lib/skills";

import {
  sfxBlip,
  sfxSubmit,
  sfxHit,
  sfxCapture,
  sfxWin,
  sfxLose,
  setSfxEnabled,
} from "../lib/sfx";
import { ReasoningTrend, type TrendSeries } from "./ReasoningTrend";
import { useIrisTransition } from "./fx/IrisWipe";
import {
  CombatantState,
  JudgeVerdict,
  SkillEffect,
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

function reactionLabel(state: string) {
  return state
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
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
  const reaction = u.reaction_state ? reactionLabel(u.reaction_state) : null;
  const moveLabel = reaction ?? u.skill_used;
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
        {moveLabel && (
          <span
            className="font-hud text-[9px] px-1"
            style={{
              background: reaction
                ? "rgba(255,255,255,0.18)"
                : "rgba(255,255,255,0.1)",
            }}
          >
            {moveLabel}
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

function SkillEffectBadge({ effect }: { effect: SkillEffect }) {
  return (
    <div className="pixel-inset p-2" style={{ borderColor: "var(--accent)" }}>
      <div className="flex items-center gap-2">
        <span className="font-hud text-[9px]" style={{ color: "var(--accent)" }}>
          {effect.effect_kind.replace("_", " ").toUpperCase()}
        </span>
        <span className="font-hud text-[9px] ml-auto" style={{ color: "var(--muted)" }}>
          T{effect.turn_no}
        </span>
      </div>
      <p className="font-body text-[11px] mt-1" style={{ color: "var(--ink)" }}>
        {effect.message || effect.skill_name || effect.skill_id}
      </p>
      {effect.duration_turns > 0 && (
        <p className="font-hud text-[8px] mt-1" style={{ color: "var(--muted)" }}>
          {effect.duration_turns} TURN
        </p>
      )}
    </div>
  );
}

const BATTLE_TILE = {
  GRASS: 0,
  ROAD: 3,
  FOREST: 5,
  TOWN: 8,
  CAVE: 9,
} as const;

function battleBackdropStyle(tile: number | null | undefined): CSSProperties {
  switch (tile) {
    case BATTLE_TILE.FOREST:
      return {
        backgroundColor: "#142919",
        backgroundImage: [
          "linear-gradient(90deg, rgba(18,12,8,0.55) 0 8px, transparent 8px 32px)",
          "linear-gradient(0deg, rgba(67,114,54,0.72) 0 14px, transparent 14px 32px)",
          "linear-gradient(90deg, rgba(50,92,43,0.55) 0 16px, transparent 16px 32px)",
        ].join(","),
        backgroundSize: "64px 64px, 32px 32px, 48px 48px",
      };
    case BATTLE_TILE.ROAD:
      return {
        backgroundColor: "#493827",
        backgroundImage: [
          "linear-gradient(90deg, transparent 0 18%, rgba(138,97,55,0.9) 18% 82%, transparent 82%)",
          "linear-gradient(0deg, rgba(213,160,91,0.22) 0 4px, transparent 4px 16px)",
          "linear-gradient(90deg, rgba(91,63,39,0.45) 0 2px, transparent 2px 18px)",
        ].join(","),
        backgroundSize: "100% 100%, 40px 40px, 28px 28px",
      };
    case BATTLE_TILE.TOWN:
      return {
        backgroundColor: "#524735",
        backgroundImage: [
          "linear-gradient(90deg, rgba(255,207,63,0.18) 0 18px, transparent 18px 56px)",
          "linear-gradient(0deg, rgba(255,255,255,0.08) 0 2px, transparent 2px 28px)",
          "linear-gradient(90deg, rgba(0,0,0,0.18) 0 2px, transparent 2px 28px)",
        ].join(","),
        backgroundSize: "96px 64px, 28px 28px, 28px 28px",
      };
    case BATTLE_TILE.CAVE:
      return {
        backgroundColor: "#18171d",
        backgroundImage: [
          "linear-gradient(135deg, rgba(99,94,104,0.28) 0 12px, transparent 12px 40px)",
          "linear-gradient(45deg, rgba(0,0,0,0.4) 0 10px, transparent 10px 36px)",
          "linear-gradient(0deg, rgba(86,76,84,0.22) 0 3px, transparent 3px 24px)",
        ].join(","),
        backgroundSize: "72px 72px, 56px 56px, 32px 32px",
      };
    case BATTLE_TILE.GRASS:
    default:
      return {
        backgroundColor: "#26351f",
        backgroundImage: [
          "linear-gradient(90deg, rgba(91,123,57,0.34) 0 6px, transparent 6px 24px)",
          "linear-gradient(0deg, rgba(71,93,48,0.45) 0 8px, transparent 8px 32px)",
          "linear-gradient(45deg, rgba(175,196,97,0.14) 0 4px, transparent 4px 20px)",
        ].join(","),
        backgroundSize: "32px 32px, 48px 48px, 28px 28px",
      };
  }
}

type PartyMember = {
  id: string;
  name: string;
  type: string;
  level: number;
  skills: unknown[];
  mp?: number;
  max_mp?: number;
};

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
    skillEffects,
    statuses,
    intelPreview,
    estimates,
    running,
    runningTurn,
    drive,
    argue,
    invokeSkill,
  } = useEncounterStream(activeEncounterId);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Player input
  const [argText, setArgText] = useState("");
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [partyMembers, setPartyMembers] = useState<PartyMember[]>([]);
  const [activePartyId, setActivePartyId] = useState<string | null>(null);
  const [captureFlash, setCaptureFlash] = useState(false);

  // Retro SFX (spec §7 stretch) — mute toggle + edge-detection refs.
  const [sfxOn, setSfxOn] = useState(true);
  const prevVerdictCount = useRef(0);
  const playedEndSfx = useRef(false);

  const combatants: CombatantState[] = useMemo(
    () => encounter?.combatants ?? [],
    [encounter]
  );

  // Lead party monster = highest level, party-first (matches backend _lead).
  const leadParty = useMemo(() => {
    const party = combatants.filter((c) => c.role === "party");
    return party.length ? party.slice().sort((a, b) => b.max_hp - a.max_hp)[0] : null;
  }, [combatants]);

  const activeParty = useMemo(() => {
    const selected = combatants.find(
      (c) => c.role === "party" && c.hp > 0 && c.monster_id === activePartyId
    );
    return selected ?? leadParty;
  }, [combatants, activePartyId, leadParty]);

  const activePartyMember = useMemo(() => {
    if (activeParty) {
      const matched = partyMembers.find((p) => p.id === activeParty.monster_id);
      if (matched) return matched;
    }
    return partyMembers.find((p) => p.id === activePartyId) ?? partyMembers[0] ?? null;
  }, [activeParty, activePartyId, partyMembers]);

  const skills = useMemo(
    () => parseSkills(activePartyMember?.skills ?? []),
    [activePartyMember]
  );
  const selectedSkillObj = useMemo(
    () => skills.find((skill) => skill.id === selectedSkill) ?? null,
    [selectedSkill, skills]
  );

  // Lead enemy (first living enemy, else first enemy) — used as the defender for
  // skill type-effectiveness ("super effective" vs the current opponent).
  const leadEnemy = useMemo(() => {
    const enemies = combatants.filter((c) => c.role === "enemy");
    return enemies.find((c) => c.hp > 0) ?? enemies[0] ?? null;
  }, [combatants]);

  // Fetch the player's party once to source each agent's battle skills.
  useEffect(() => {
    if (!runId) return;
    api
      .get<PartyMember[]>(`/api/runs/${runId}/party`)
      .then((party) => {
        if (!party.length) return;
        const lead = party.slice().sort((a, b) => (b.level ?? 0) - (a.level ?? 0))[0];
        setPartyMembers(party);
        setActivePartyId((current) => current ?? lead.id);
      })
      .catch(() => {
        /* skills are optional — text-only still works */
      });
  }, [runId]);

  useEffect(() => {
    if (!activePartyId && leadParty) setActivePartyId(leadParty.monster_id);
  }, [activePartyId, leadParty]);

  useEffect(() => {
    if (!selectedSkill) return;
    if (!skills.some((skill) => skill.id === selectedSkill)) setSelectedSkill(null);
  }, [selectedSkill, skills]);

  // Live "You" reasoning series: player verdicts (target = a party monster).
  const partyIds = useMemo(
    () => new Set(combatants.filter((c) => c.role === "party").map((c) => c.monster_id)),
    [combatants]
  );
  const partyCombatants = useMemo(
    () => combatants.filter((c) => c.role === "party"),
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

  // Per-card floating damage: latest verdict's damage keyed by target id.
  const lastVerdict = verdicts[verdicts.length - 1];
  const floatByTarget: Record<string, number> = {};
  if (lastVerdict) floatByTarget[lastVerdict.target] = lastVerdict.damage;
  const floatKey = lastVerdict?.turn;

  // Auto-scroll transcript
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript.length]);

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

  const phase = encounter?.phase ?? "intro";
  const isCapturable = phase === "capturable";
  const isOver = phase === "won" || phase === "lost";
  const canArgue = phase === "debating" || phase === "intro" || phase === "capturable";
  const promptMissingForSkill = Boolean(
    selectedSkillObj?.requires_prompt && !argText.trim()
  );
  const submitDisabled =
    (!argText.trim() && !selectedSkill) || promptMissingForSkill || isOver || running;
  const submitLabel = running
    ? "Debating…"
    : selectedSkillObj?.effect_kind === "intel_preview"
      ? "Preview"
      : selectedSkill && !argText.trim()
        ? "Use Skill"
        : "Argue";

  // Battle isolation: lock the global nav while the battle is live, release it
  // the moment it resolves (won/lost) so the post-battle "Leave" can navigate.
  useEffect(() => {
    setBattleLocked(!!activeEncounterId && !isOver);
  }, [activeEncounterId, isOver, setBattleLocked]);

  // Player's debate side (active party monster) — drives the "You argue FOR" copy.
  const playerSide: DebateSide = activeParty ? combatantSide(activeParty) : "for";

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
    if ((!text && !selectedSkill) || busy || isOver || running) return;
    if (selectedSkillObj?.effect_kind === "intel_preview") {
      setActionError(null);
      sfxSubmit();
      invokeSkill(selectedSkillObj.id, activeParty?.monster_id ?? activePartyId);
      return;
    }
    if (selectedSkillObj?.requires_prompt && !text) {
      setActionError(`${selectedSkillObj.name} needs your prompt this turn.`);
      return;
    }
    setActionError(null);
    sfxSubmit();
    argue(text, selectedSkill, activeParty?.monster_id ?? activePartyId);
    setArgText("");
  }

  function toggleSfx() {
    setSfxOn((prev) => {
      const next = !prev;
      setSfxEnabled(next);
      return next;
    });
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

  return (
    <div
      className="flex flex-col h-full max-h-screen overflow-hidden relative"
      style={{
        ...battleBackdropStyle(encounter?.location_tile),
        boxShadow: "inset 0 0 120px rgba(0,0,0,0.52)",
      }}
    >
      {/* Gacha Wave D — listens for `{type: "LevelUp"}` WS events from the
          encounter finalize and plays a 3s "+ATK/+DEF/+MP/+HP" cinematic.
          Self-handles its own event subscription; mounting it is the wiring.
          Combatants are passed so the headline can read "{name} LEVEL N". */}
      <LevelUpOverlay combatants={combatants} />

      {captureFlash && (
        <div
          className="capture-flash absolute inset-0 z-50 pointer-events-none"
          style={{ background: "var(--accent)" }}
        />
      )}

      {/* Header — shows the actual BATTLE (encounter) topic, which can differ
          from the run topic; falls back to the run topic only while loading. */}
      <div className="flex items-center justify-between px-4 py-2" style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}>
        <div className="min-w-0">
          <div className="font-hud text-[8px] uppercase tracking-wide" style={{ color: "var(--muted)" }}>
            You argue{" "}
            <span style={{ color: sideColor(playerSide) }}>{sideLabel(playerSide)}</span>
          </div>
          <div className="font-hud text-xs truncate" title={encounter?.topic ?? runTopic}>
            {encounter?.topic || runTopic || "Loading…"}
          </div>
        </div>
        <div className="flex items-center gap-3 font-hud text-[10px]">
          {/* Active-turn indicator */}
          <span
            className="px-1.5 py-0.5 inline-flex items-center gap-1"
            style={{ border: `1px solid ${turnIndicator.color}`, color: turnIndicator.color }}
          >
            {running && <span className="caret-blink">●</span>}
            {turnIndicator.label}
          </span>
          <span style={{ color: status === "open" ? "var(--win)" : status === "connecting" ? "var(--warn)" : "var(--danger)" }}>
            ● {status}
          </span>
          <span style={{ color: "var(--muted)" }}>T{encounter?.turn_no ?? 0}</span>
          <span
            style={{
              color:
                phase === "won"
                  ? "var(--win)"
                  : phase === "lost"
                    ? "var(--danger)"
                    : phase === "capturable"
                      ? "var(--accent)"
                      : "var(--muted)",
            }}
          >
            {phase}
          </span>
        </div>
      </div>

      {/* In-progress banner: clear feedback while a round streams so the
          user is never left staring at nothing for 30-120s. */}
      {running && !isOver && (
        <div
          className="flex items-center gap-2 px-4 py-1.5 font-hud text-[11px]"
          style={{ background: "rgba(255,255,255,0.04)", color: "var(--accent)", borderBottom: "2px solid rgba(232,230,216,0.12)" }}
        >
          <span className="caret-blink">▋</span>
          Debating…{runningTurn != null ? ` (turn ${runningTurn})` : ""} — the judge and your opponent are thinking, this can take a moment.
        </div>
      )}

      {/* Combatant HP */}
      <div className="flex gap-2 p-3 flex-wrap" style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}>
        {combatants.length === 0 ? (
          <div className="font-body text-xs" style={{ color: "var(--muted)" }}>
            Awaiting combatant data…
          </div>
        ) : (
          combatants.map((c) => {
            // Active combatant: the selected party agent on the player's turn, the lead
            // enemy while the opponent is arguing. No highlight once over.
            const activeId = isOver
              ? null
              : turnIndicator.label === "Enemy arguing…"
                ? leadEnemy?.monster_id
                : activeParty?.monster_id;
            return (
              <CombatantCard
                key={c.monster_id}
                c={c}
                isLead={c.monster_id === activeParty?.monster_id}
                isActive={activeId != null ? c.monster_id === activeId : undefined}
                floatDmg={floatByTarget[c.monster_id] ?? null}
                floatKey={floatKey}
              />
            );
          })
        )}
      </div>

      {/* Transcript + side panel */}
      <div className="flex flex-1 overflow-hidden gap-2 p-2">
        <div className="flex-1 flex flex-col overflow-hidden">
          <div className="font-hud text-[10px] mb-1 px-1" style={{ color: "var(--muted)" }}>
            Transcript ({transcript.length})
          </div>
          <div className="flex-1 overflow-y-auto space-y-2 pr-1">
            {transcript.map((u, i) => (
              <UtteranceBubble
                key={`${u.turn}-${u.actor_id}-${i}`}
                u={u}
                liveNames={liveNames}
                isNewest={i === transcript.length - 1 && u === newestTurn}
              />
            ))}
            {transcript.length === 0 && (
              <div className="font-body text-sm italic px-1" style={{ color: "var(--muted)" }}>
                Type your opening argument below to begin the debate…
              </div>
            )}
            <div ref={transcriptEndRef} />
          </div>
        </div>

        <div className="w-64 shrink-0 flex flex-col overflow-hidden gap-2">
          <ReasoningTrend series={[youSeries]} title="Your reasoning" />
          {(statuses.length > 0 || skillEffects.length > 0 || intelPreview) && (
            <div className="space-y-2">
              <div className="font-hud text-[10px] px-1" style={{ color: "var(--muted)" }}>
                Skill Effects
              </div>
              {intelPreview && (
                <div className="pixel-inset p-2" style={{ borderColor: "var(--accent)" }}>
                  <div className="font-hud text-[9px]" style={{ color: "var(--accent)" }}>
                    PREVIEW
                  </div>
                  <p className="font-body text-[11px] mt-1" style={{ color: "var(--ink)" }}>
                    {intelPreview.preview}
                  </p>
                </div>
              )}
              {statuses.slice(-3).map((effect, i) => (
                <SkillEffectBadge
                  key={`status-${effect.skill_id}-${effect.turn_no}-${i}`}
                  effect={effect}
                />
              ))}
              {skillEffects
                .filter((effect) => effect.duration_turns <= 0)
                .slice(-2)
                .map((effect, i) => (
                  <SkillEffectBadge
                    key={`effect-${effect.skill_id}-${effect.turn_no}-${i}`}
                    effect={effect}
                  />
                ))}
            </div>
          )}
          <div className="font-hud text-[10px] px-1" style={{ color: "var(--muted)" }}>
            Judge Verdicts
          </div>
          <div className="flex-1 overflow-y-auto space-y-2">
            {/* A4: optimistic estimates render above the settled verdicts and
                disappear once the judge's real score lands. */}
            {pendingEstimates.map((e) => (
              <EstimateBadge
                key={`est-${e.turn}-${e.actor_id}`}
                score={e.score}
                turn={e.turn}
              />
            ))}
            {verdicts
              .slice()
              .reverse()
              .map((v, i) => (
                <VerdictBadge key={`${v.turn}-${v.target}-${i}`} v={v} fresh={i === 0} />
              ))}
            {verdicts.length === 0 && pendingEstimates.length === 0 && (
              <div className="font-body text-[11px] italic px-1" style={{ color: "var(--muted)" }}>
                No verdicts yet
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Player argument bar */}
      {canArgue && !isOver && (
        <div className="px-3 py-2 space-y-2" style={{ borderTop: "2px solid rgba(232,230,216,0.12)" }}>
          {partyCombatants.length > 1 && (
            <div className="flex gap-1.5 flex-wrap items-center">
              <span className="font-hud text-[9px] mr-1" style={{ color: "var(--muted)" }}>
                Agent
              </span>
              {partyCombatants.map((agent) => {
                const active = activeParty?.monster_id === agent.monster_id;
                const hpPct = agent.max_hp > 0 ? agent.hp / agent.max_hp : 0;
                return (
                  <button
                    key={agent.monster_id}
                    className="pixel-btn text-[9px] py-1"
                    disabled={running || isOver || agent.hp <= 0}
                    onClick={() => {
                      setActivePartyId(agent.monster_id);
                      setSelectedSkill(null);
                      sfxBlip();
                    }}
                    title={`${agent.name} · ${agent.type} · ${agent.hp}/${agent.max_hp} HP`}
                    style={
                      active
                        ? { background: typeColor(agent.type), color: "#000", borderColor: "#000" }
                        : { borderColor: typeColor(agent.type), opacity: agent.hp <= 0 ? 0.45 : Math.max(0.68, hpPct) }
                    }
                  >
                    {agent.name}
                    {typeof agent.mp === "number" && typeof agent.max_mp === "number" && (
                      <span className="ml-1" style={{ color: active ? "#000" : "var(--accent)" }}>
                        {agent.mp}/{agent.max_mp}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
          {skills.length > 0 && (
            <div className="space-y-1">
              <div className="flex gap-1.5 flex-wrap">
                {skills.map((s) => {
                  const active = selectedSkill === s.id;
                  const eff = effectivenessInfo(s.type, leadEnemy?.type);
                  // Gacha Wave B: dim the chip when the active agent's MP can't
                  // cover this skill. Cost is sourced from the skill object
                  // (parseSkill falls back to SKILL_MP_COSTS). Free skills
                  // (cost 0) never dim.
                  const activeMp =
                    typeof activeParty?.mp === "number" ? activeParty.mp : Infinity;
                  const cost = Number(s.mp_cost ?? 0);
                  const unaffordable = cost > 0 && activeMp < cost;
                  const tip = `${skillTooltip(s)}${
                    eff.label ? ` vs ${leadEnemy?.type ?? "enemy"}: ${eff.label} (×${eff.multiplier})` : ""
                  }${cost > 0 ? ` · MP ${cost}` : ""}${
                    unaffordable ? ` (not enough MP: ${activeMp}/${cost})` : ""
                  }`;
                  return (
                    <button
                      key={s.id}
                      title={tip}
                      onClick={() => setSelectedSkill(active ? null : s.id)}
                      disabled={running || isOver || unaffordable}
                      className="pixel-btn text-[10px] py-1 relative max-w-[168px]"
                      style={
                        active
                          ? { background: typeColor(s.type), color: "#000", borderColor: "#000" }
                          : { borderColor: typeColor(s.type), opacity: unaffordable ? 0.45 : 1 }
                      }
                    >
                      <span className="block truncate">{s.name}</span>
                      <span className="block font-hud text-[8px]" style={{ color: active ? "#000" : "var(--muted)" }}>
                        {effectLabel(s.effect_kind)}
                        {s.duration_turns > 0 ? ` · ${s.duration_turns}T` : ""}
                      </span>
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
              {/* One-shot WS rejection banner: the picked skill cost more MP
                  than the active agent has. The hook clears this on the next attempt. */}
              {mpInsufficient && (
                <div
                  className="font-hud text-[9px] px-1"
                  style={{ color: "var(--danger)" }}
                >
                  {mpInsufficient.detail}
                </div>
              )}
              {/* Type-effectiveness callout for the currently selected skill. */}
              {selectedSkillObj && leadEnemy && (() => {
                const eff = effectivenessInfo(selectedSkillObj.type, leadEnemy.type);
                if (!eff.label) {
                  return (
                    <div className="font-hud text-[9px] px-1" style={{ color: "var(--muted)" }}>
                      {effectLabel(selectedSkillObj.effect_kind)} · {selectedSkillObj.type || "—"} vs {leadEnemy.type}: Neutral (×1)
                    </div>
                  );
                }
                return (
                  <div className="font-hud text-[9px] px-1" style={{ color: eff.color }}>
                    {effectLabel(selectedSkillObj.effect_kind)} · {selectedSkillObj.type} vs {leadEnemy.type}: {eff.label} (×{eff.multiplier})
                  </div>
                );
              })()}
            </div>
          )}
          <div className="flex gap-2 items-end">
            <textarea
              className="pixel-field flex-1 font-body text-sm resize-none h-16"
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
                className="pixel-btn pixel-btn--party"
                disabled={submitDisabled}
                onClick={submitArgument}
                title={
                  running
                    ? "Wait for the current round to finish"
                    : promptMissingForSkill
                      ? `${selectedSkillObj?.name ?? "Skill"} needs your prompt`
                    : selectedSkillObj?.effect_kind === "intel_preview"
                      ? `${activeParty?.name ?? "Agent"} previews the opponent`
                    : selectedSkill && !argText.trim()
                      ? `${activeParty?.name ?? "Agent"} uses ${selectedSkillObj?.name ?? selectedSkill}`
                      : "Submit your argument"
                }
              >
                {submitLabel}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Action bar */}
      <div className="px-3 py-2 flex items-center gap-2 flex-wrap" style={{ borderTop: "2px solid rgba(232,230,216,0.12)" }}>
        {actionError && (
          <span className="font-body text-[11px] flex-1" style={{ color: "var(--danger)" }}>
            {actionError}
          </span>
        )}
        {isOver && (
          <span className="font-display text-sm" style={{ color: phase === "won" ? "var(--win)" : "var(--danger)" }}>
            {phase === "won" ? "VICTORY" : "DEFEAT"}
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
