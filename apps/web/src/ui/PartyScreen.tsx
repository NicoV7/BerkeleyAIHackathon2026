/**
 * PartyScreen — WS-E (gacha wave: capture flow replaced by gacha pulls)
 *
 * Displays the player's current party and links to the Gambit editor (WS-C
 * owns that component; we never embed it here). The capture-result banner was
 * removed when the capture acquisition flow was deleted — the gacha screen
 * owns post-pull feedback now.
 *
 * Props: none — reads runId from global store.
 */
import { useEffect, useState, useCallback } from "react";
import { useGame } from "../state/store";
import { api } from "../api/client";
import { parseSkills, typeColor } from "../lib/skills";

// ---- Types (mirrors app/schemas.py MonsterSummary) ----

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
  // Gacha-wave stat fields (Wave B paints them into the UI; defaults keep this
  // component renderable against pre-wave-A backends).
  atk?: number;
  def?: number;
  mp?: number;
  max_mp?: number;
  domain?: string;
  wiki_hydrated?: boolean;
  is_avatar?: boolean;
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

/**
 * Tiny stat chip used by the gacha Wave B MonsterCard footer (ATK/DEF/MP +
 * optional domain). One chip per stat so the player can see the persona's
 * combat fingerprint at a glance without expanding the card.
 */
function StatChip({
  label,
  value,
  color,
}: {
  label: string;
  value: string | number;
  color?: string;
}) {
  return (
    <span
      className="pixel-inset px-1.5 py-0.5 font-hud text-[10px] inline-flex items-center gap-1"
      style={{ borderColor: color ?? "rgba(232,230,216,0.18)" }}
      title={`${label} ${value}`}
    >
      <span style={{ color: color ?? "var(--muted)" }}>{label}</span>
      <span style={{ color: "var(--ink)" }}>{value}</span>
    </span>
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
        <div className="flex items-center gap-1.5">
          {monster.is_avatar && (
            <span
              className="font-hud text-[9px] px-1.5 py-0.5"
              style={{ border: "1px solid var(--accent)", color: "var(--accent)" }}
            >
              ★ Avatar
            </span>
          )}
          <TypeBadge type={monster.type} />
        </div>
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

      {/* Gacha Wave B stat chips: ATK/DEF/MP alongside type/level so the player
          reads the monster's combat fingerprint from the card alone. Skip
          gracefully when the API hasn't filled the fields (old pre-gacha runs). */}
      {(typeof monster.atk === "number" ||
        typeof monster.def === "number" ||
        typeof monster.max_mp === "number" ||
        (monster.domain && monster.domain !== "GENERAL")) && (
        <div className="flex flex-wrap gap-1.5">
          {typeof monster.atk === "number" && (
            <StatChip label="ATK" value={monster.atk} color="var(--danger)" />
          )}
          {typeof monster.def === "number" && (
            <StatChip label="DEF" value={monster.def} color="var(--win)" />
          )}
          {typeof monster.max_mp === "number" && (
            <StatChip
              label="MP"
              value={
                typeof monster.mp === "number"
                  ? `${monster.mp}/${monster.max_mp}`
                  : monster.max_mp
              }
              color="var(--accent)"
            />
          )}
          {monster.domain && monster.domain !== "GENERAL" && (
            <StatChip label="DOMAIN" value={monster.domain} />
          )}
        </div>
      )}

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

// ---- Main component ----
export default function PartyScreen() {
  const { runId, playerName } = useGame();
  const [party, setParty] = useState<MonsterSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        <h2 className="font-display text-base">{playerName}'s Party</h2>
        <button onClick={fetchParty} disabled={loading} className="pixel-btn text-[10px]">
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

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
            Pull a persona from the gacha to start your party.
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
