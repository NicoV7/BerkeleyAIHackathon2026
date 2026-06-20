/**
 * PartyScreen — WS-E
 *
 * Displays the player's current party, the last capture result, and links to
 * the Gambit editor (WS-C owns that component; we never embed it here).
 *
 * Props: none — reads runId from global store.
 */
import { useEffect, useState, useCallback } from "react";
import { useGame } from "../state/store";
import { api } from "../api/client";
import { parseSkills, typeColor } from "../lib/skills";

// ---- Types (mirrors app/schemas.py MonsterSummary + CaptureResult) ----

interface MonsterSummary {
  id: string;
  name: string;
  type: string;
  owner: string;
  level: number;
  xp: number;
  max_hp: number;
  evolution_stage: number;
  skills: (string | Record<string, unknown>)[];
}

interface CaptureResult {
  success: boolean;
  monster: MonsterSummary | null;
  message: string;
}

function TypeBadge({ type }: { type: string }) {
  return (
    <span
      className="font-hud text-[10px] px-1.5 py-0.5"
      style={{ background: typeColor(type), color: "#000" }}
    >
      {type}
    </span>
  );
}

// ---- XP bar ----
function XpBar({ xp, level }: { xp: number; level: number }) {
  const needed = 100 * level;
  const pct = Math.min(100, Math.round((xp / needed) * 100));
  return (
    <div className="flex items-center gap-2 font-hud text-[10px]">
      <span style={{ color: "var(--muted)" }} className="w-6 shrink-0">
        XP
      </span>
      <div className="flex-1 h-2 overflow-hidden" style={{ background: "rgba(232,230,216,0.1)" }}>
        <div className="h-full" style={{ width: `${pct}%`, background: "var(--accent)" }} />
      </div>
      <span style={{ color: "var(--muted)" }} className="w-16 text-right shrink-0">
        {xp}/{needed}
      </span>
    </div>
  );
}

// ---- Skill chip ----
function SkillChip({ skill }: { skill: ReturnType<typeof parseSkills>[number] }) {
  return (
    <div
      className="pixel-inset px-1.5 py-1 flex flex-col gap-0.5 min-w-[7rem]"
      style={{ borderColor: typeColor(skill.type) }}
      title={skill.description}
    >
      <div className="flex items-center gap-1">
        <span className="font-hud text-[10px]">{skill.name}</span>
        {skill.power !== 1 && (
          <span className="font-hud text-[9px]" style={{ color: "var(--accent)" }}>
            ×{skill.power}
          </span>
        )}
      </div>
      {skill.type && (
        <span className="font-hud text-[8px]" style={{ color: typeColor(skill.type) }}>
          {skill.type}
        </span>
      )}
      {skill.description && (
        <span className="font-body text-[10px]" style={{ color: "var(--muted)" }}>
          {skill.description}
        </span>
      )}
    </div>
  );
}

// ---- Single party member card ----
function MonsterCard({ monster }: { monster: MonsterSummary }) {
  const evolutionLabel = monster.evolution_stage === 0
    ? "Base"
    : monster.evolution_stage === 1
    ? "Stage 1"
    : `Stage ${monster.evolution_stage}`;

  const skills = parseSkills(monster.skills);

  return (
    <div className="pixel-panel p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="font-hud text-sm">{monster.name}</span>
        <TypeBadge type={monster.type} />
      </div>

      <div className="grid grid-cols-3 gap-2">
        {[
          ["Level", monster.level],
          ["HP", monster.max_hp],
          ["Stage", evolutionLabel],
        ].map(([label, val]) => (
          <div key={label}>
            <div className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
              {label}
            </div>
            <div className="font-hud text-sm">{val}</div>
          </div>
        ))}
      </div>

      <XpBar xp={monster.xp} level={monster.level} />

      {skills.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {skills.map((s) => (
            <SkillChip key={s.id} skill={s} />
          ))}
        </div>
      )}

      {/* Gambits are authored BEFORE fighting — always reachable (§5.3). */}
      <a
        href={`#gambits/${monster.id}`}
        className="inline-block font-hud text-[10px] mt-1"
        style={{ color: "var(--accent)" }}
        onClick={(e) => {
          e.preventDefault();
          window.location.hash = `gambits/${monster.id}`;
        }}
      >
        Edit Gambits →
      </a>
    </div>
  );
}

// ---- Capture result banner ----
function CaptureBanner({
  result,
  onDismiss,
}: {
  result: CaptureResult;
  onDismiss: () => void;
}) {
  return (
    <div
      className="pixel-panel p-3 flex items-center justify-between gap-2 font-body text-sm"
      style={{ borderColor: result.success ? "var(--win)" : "var(--danger)" }}
    >
      <span>
        {result.success ? "Captured!" : "Failed"} — {result.message}
      </span>
      <button
        onClick={onDismiss}
        className="font-hud text-[10px] shrink-0"
        style={{ color: "var(--muted)" }}
      >
        dismiss
      </button>
    </div>
  );
}

// ---- Main component ----
export default function PartyScreen() {
  const { runId } = useGame();
  const [party, setParty] = useState<MonsterSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [captureResult, setCaptureResult] = useState<CaptureResult | null>(null);

  const fetchParty = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<MonsterSummary[]>(`/api/runs/${runId}/party`);
      setParty(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load party.");
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    fetchParty();
  }, [fetchParty]);

  if (!runId) {
    return (
      <div className="text-center py-8 font-body" style={{ color: "var(--muted)" }}>
        No active run. Start a run first.
      </div>
    );
  }

  return (
    <div className="w-full max-w-2xl mx-auto px-4 py-6 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-base">Party</h2>
        <button onClick={fetchParty} disabled={loading} className="pixel-btn text-[10px]">
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {captureResult && (
        <CaptureBanner
          result={captureResult}
          onDismiss={() => setCaptureResult(null)}
        />
      )}

      {error && (
        <div
          className="pixel-panel p-3 font-body text-sm"
          style={{ borderColor: "var(--danger)", color: "var(--danger)" }}
        >
          {error}
        </div>
      )}

      {!loading && party.length === 0 && !error && (
        <div className="pixel-panel p-6 text-center">
          <div className="text-4xl mb-3">👾</div>
          <div className="font-hud text-sm mb-1">No party members yet</div>
          <div className="font-body text-xs" style={{ color: "var(--muted)" }}>
            Weaken an enemy below 25% HP, then capture it in battle.
          </div>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {party.map((m) => (
          <MonsterCard key={m.id} monster={m} />
        ))}
      </div>
    </div>
  );
}
