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

// ---- Types (mirrors app/schemas.py MonsterSummary + CaptureResult) ----

interface MonsterSummary {
  id: string;
  name: string;
  type: string;
  owner: string;
  level: int;
  xp: int;
  max_hp: int;
  evolution_stage: int;
  skills: (string | Record<string, unknown>)[];
}

// Avoid TS "int" error — alias to number
type int = number;

interface CaptureResult {
  success: boolean;
  monster: MonsterSummary | null;
  message: string;
}

// ---- Type badge colours (mirrors the 6 DebateTypes) ----
const TYPE_COLOURS: Record<string, string> = {
  LOGOS:    "bg-blue-700",
  PATHOS:   "bg-rose-700",
  ETHOS:    "bg-amber-700",
  CHAOS:    "bg-purple-700",
  SOCRATIC: "bg-teal-700",
  RHETORIC: "bg-green-700",
};

function TypeBadge({ type }: { type: string }) {
  const colour = TYPE_COLOURS[type.toUpperCase()] ?? "bg-gray-700";
  return (
    <span className={`${colour} text-white text-xs font-semibold px-2 py-0.5 rounded`}>
      {type}
    </span>
  );
}

// ---- XP bar ----
function XpBar({ xp, level }: { xp: number; level: number }) {
  const needed = 100 * level;
  const pct = Math.min(100, Math.round((xp / needed) * 100));
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="opacity-60 w-6 shrink-0">XP</span>
      <div className="flex-1 bg-white/10 rounded-full h-1.5 overflow-hidden">
        <div className="bg-yellow-400 h-full" style={{ width: `${pct}%` }} />
      </div>
      <span className="opacity-60 w-16 text-right shrink-0">
        {xp}/{needed}
      </span>
    </div>
  );
}

// ---- Single party member card ----
function MonsterCard({
  monster,
  encounterId,
}: {
  monster: MonsterSummary;
  encounterId: string | null;
}) {
  const evolutionLabel = monster.evolution_stage === 0
    ? "Base"
    : monster.evolution_stage === 1
    ? "Stage 1"
    : `Stage ${monster.evolution_stage}`;

  const skillLabels = (monster.skills ?? []).map((s) =>
    typeof s === "string" ? s : JSON.stringify(s)
  );

  return (
    <div className="bg-white/5 border border-white/10 rounded-lg p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold">{monster.name}</span>
        <TypeBadge type={monster.type} />
      </div>

      <div className="grid grid-cols-3 gap-2 text-xs opacity-70">
        <div>
          <div className="uppercase tracking-wide text-[10px] opacity-60">Level</div>
          <div className="font-mono font-semibold">{monster.level}</div>
        </div>
        <div>
          <div className="uppercase tracking-wide text-[10px] opacity-60">HP</div>
          <div className="font-mono font-semibold">{monster.max_hp}</div>
        </div>
        <div>
          <div className="uppercase tracking-wide text-[10px] opacity-60">Stage</div>
          <div className="font-mono font-semibold">{evolutionLabel}</div>
        </div>
      </div>

      <XpBar xp={monster.xp} level={monster.level} />

      {skillLabels.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-1">
          {skillLabels.map((s) => (
            <span
              key={s}
              className="text-xs bg-indigo-900/60 border border-indigo-500/30 rounded px-1.5 py-0.5"
            >
              {s}
            </span>
          ))}
        </div>
      )}

      {/* Link to Gambit editor — WS-C owns that component */}
      {encounterId && (
        <a
          href={`#gambits/${monster.id}`}
          className="block text-xs text-indigo-400 hover:text-indigo-300 mt-1"
          onClick={(e) => {
            e.preventDefault();
            // WS-C wires up the gambit editor; we just signal intent via hash
            window.location.hash = `gambits/${monster.id}`;
          }}
        >
          Edit Gambits →
        </a>
      )}
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
  const colour = result.success
    ? "bg-green-900/70 border-green-500/40"
    : "bg-red-900/70 border-red-500/40";

  return (
    <div
      className={`${colour} border rounded-lg p-3 flex items-center justify-between gap-2 text-sm`}
    >
      <span>
        {result.success ? "Captured!" : "Failed"} — {result.message}
      </span>
      <button
        onClick={onDismiss}
        className="text-xs opacity-60 hover:opacity-100 shrink-0"
      >
        dismiss
      </button>
    </div>
  );
}

// ---- Main component ----
export default function PartyScreen() {
  const { runId, activeEncounterId } = useGame();
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
      <div className="text-center opacity-50 py-8">No active run. Start a run first.</div>
    );
  }

  return (
    <div className="w-full max-w-2xl mx-auto px-4 py-6 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold">Party</h2>
        <button
          onClick={fetchParty}
          disabled={loading}
          className="text-xs bg-white/5 hover:bg-white/10 border border-white/10 rounded px-3 py-1 disabled:opacity-50"
        >
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
        <div className="text-sm text-red-400 bg-red-900/30 border border-red-500/30 rounded p-3">
          {error}
        </div>
      )}

      {!loading && party.length === 0 && !error && (
        <div className="text-center opacity-50 py-12">
          <div className="text-4xl mb-3">👾</div>
          <div className="text-sm">No party members yet. Capture a wild monster!</div>
          <div className="text-xs opacity-60 mt-1">
            Weaken an enemy below 25% HP then use the capture action in battle.
          </div>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {party.map((m) => (
          <MonsterCard
            key={m.id}
            monster={m}
            encounterId={activeEncounterId}
          />
        ))}
      </div>

      {/* Gambit editor entry point — WS-C mounts GambitEditor at this route */}
      {party.length > 0 && (
        <p className="text-xs opacity-40 text-center pt-2">
          Click "Edit Gambits" on any card to open the AI behaviour editor (WS-C).
        </p>
      )}
    </div>
  );
}
