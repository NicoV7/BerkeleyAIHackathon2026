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
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import { parseSkills, typeColor, type ParsedSkill } from "../lib/skills";
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
import {
  CombatantState,
  JudgeVerdict,
  Utterance,
  useEncounterStream,
} from "../ws/useEncounterStream";

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

function CombatantCard({
  c,
  isLead,
  floatDmg,
  floatKey,
}: {
  c: CombatantState;
  isLead: boolean;
  floatDmg: number | null;
  floatKey?: number | string;
}) {
  const sideColor = c.role === "party" ? "var(--party)" : "var(--enemy)";
  return (
    <div
      className="pixel-panel p-2 min-w-[150px] flex-1 relative"
      style={{ borderColor: sideColor }}
    >
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
        <span className="font-hud text-[10px]" style={{ color: sideColor }}>
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
      <div className="font-hud text-sm truncate mt-0.5">{c.name}</div>
      <div className="font-body text-[11px] mb-1" style={{ color: "var(--muted)" }}>
        {c.hp}/{c.max_hp} HP
      </div>
      <HpBar hp={c.hp} max_hp={c.max_hp} />
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

  return (
    <div
      className="pixel-panel p-2 text-sm"
      style={{ borderColor: color, boxShadow: "3px 3px 0 #000" }}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className="font-hud text-[10px]" style={{ color }}>
          {actorName}
        </span>
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

function VerdictBadge({ v, fresh }: { v: JudgeVerdict; fresh: boolean }) {
  const positive = v.score >= 50;
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
          T{v.turn} · -{v.damage} HP
        </span>
      </div>
      <p className="font-body text-[11px] mt-1 italic" style={{ color: "var(--muted)" }}>
        {v.rationale}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function BattleDebateView() {
  const { activeEncounterId, runId, setEncounter, setYouScores } = useGame();
  const { status, encounter, transcript, verdicts, capturableIds, drive, argue } =
    useEncounterStream(activeEncounterId);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Player input
  const [argText, setArgText] = useState("");
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [skills, setSkills] = useState<ParsedSkill[]>([]);
  const [leadId, setLeadId] = useState<string | null>(null);
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

  // Fetch the player's party once to source the lead monster's skills.
  useEffect(() => {
    if (!runId) return;
    api
      .get<Array<{ id: string; type: string; level: number; skills: unknown[] }>>(
        `/api/runs/${runId}/party`
      )
      .then((party) => {
        if (!party.length) return;
        // Lead = highest level (tiebreak first).
        const lead = party.slice().sort((a, b) => (b.level ?? 0) - (a.level ?? 0))[0];
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
    return { label: "You", color: "var(--party)", points: pts };
  }, [verdicts, partyIds]);

  // Publish the player's curve so the training screen can show it beside the agent.
  useEffect(() => {
    if (youSeries.points.length) setYouScores(youSeries.points);
  }, [youSeries, setYouScores]);

  // Per-card floating damage: latest verdict's damage keyed by target id.
  const lastVerdict = verdicts[verdicts.length - 1];
  const floatByTarget: Record<string, number> = {};
  if (lastVerdict) floatByTarget[lastVerdict.target] = lastVerdict.damage;

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
    if (!text || busy || isOver) return;
    setActionError(null);
    sfxSubmit();
    argue(text, selectedSkill);
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
    setEncounter(null);
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
    <div className="flex flex-col h-full max-h-screen overflow-hidden relative">
      {captureFlash && (
        <div
          className="capture-flash absolute inset-0 z-50 pointer-events-none"
          style={{ background: "var(--accent)" }}
        />
      )}

      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2" style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}>
        <div className="font-hud text-xs truncate">{encounter?.topic ?? "Loading…"}</div>
        <div className="flex items-center gap-3 font-hud text-[10px]">
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

      {/* Combatant HP */}
      <div className="flex gap-2 p-3 flex-wrap" style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}>
        {combatants.length === 0 ? (
          <div className="font-body text-xs" style={{ color: "var(--muted)" }}>
            Awaiting combatant data…
          </div>
        ) : (
          combatants.map((c) => (
            <CombatantCard
              key={c.monster_id}
              c={c}
              isLead={c.monster_id === leadParty?.monster_id}
              floatDmg={floatByTarget[c.monster_id] ?? null}
              floatKey={lastVerdict?.turn}
            />
          ))
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
          <div className="font-hud text-[10px] px-1" style={{ color: "var(--muted)" }}>
            Judge Verdicts
          </div>
          <div className="flex-1 overflow-y-auto space-y-2">
            {verdicts
              .slice()
              .reverse()
              .map((v, i) => (
                <VerdictBadge key={`${v.turn}-${v.target}-${i}`} v={v} fresh={i === 0} />
              ))}
            {verdicts.length === 0 && (
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
          {skills.length > 0 && (
            <div className="flex gap-1.5 flex-wrap">
              {skills.map((s) => {
                const active = selectedSkill === s.id;
                return (
                  <button
                    key={s.id}
                    title={s.description}
                    onClick={() => setSelectedSkill(active ? null : s.id)}
                    className="pixel-btn text-[10px] py-1"
                    style={
                      active
                        ? { background: typeColor(s.type), color: "#000", borderColor: "#000" }
                        : { borderColor: typeColor(s.type) }
                    }
                  >
                    {s.name} ×{s.power}
                  </button>
                );
              })}
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
            <button
              className="pixel-btn pixel-btn--party"
              disabled={!argText.trim() || isOver}
              onClick={submitArgument}
            >
              Argue
            </button>
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

        <button className="pixel-btn" disabled={isOver} onClick={() => drive(1)}>
          Next Round
        </button>
        <button className="pixel-btn" disabled={isOver} onClick={() => drive(3)}>
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
