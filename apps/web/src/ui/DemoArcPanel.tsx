/**
 * DemoArcPanel — the climax of the demo as ONE continuous flow.
 *
 * Beats: 1 Fight → 2 Capture → 3 Train → 4 Rematch → Improved
 *
 * This panel owns the *choreography* between beats; it never screen-swaps.
 * State changes happen on a single stage:
 *   - The fight and the rematch both EMBED the existing <BattleDebateView/>
 *     (it reads activeEncounterId from the global store; we set that store id
 *     so the same component renders both battles, marked differently).
 *   - The capture beat fires POST /api/encounters/{id}/capture.
 *   - The train beat fires POST /api/monsters/{id}/train/gepa and shows a
 *     determinate-feeling "optimizing…" affordance while GEPA blocks.
 *   - The before/after beat shows an OVERSIZED "before → after" training-score
 *     delta (GEPA self-play score_delta, explicitly labeled "training score" so
 *     it is never conflated with the live battle score). When the delta is ≤ 0
 *     it shows an honest "held its ground" state instead of faking a win.
 *
 * Mounting (note only — NOT wired here, App.tsx is not owned by this WS):
 *   App.tsx already routes screens via <ScreenPanel>. To surface this arc, add
 *   a "demo" screen and render <DemoArcPanel/> for it, e.g. inside the switch:
 *       case "demo": return <DemoArcPanel />;
 *   and add "demo" to the nav tuple. The global Screen type lives in
 *   state/store.ts; widening it is a store-owner change, so the simplest
 *   integration is to render <DemoArcPanel/> directly (it is the default export
 *   of this module) wherever the demo button lives. The panel is self-driving:
 *   it reads runId + activeEncounterId from the store and advances on its own
 *   button presses.
 */
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import { BattleDebateView } from "./BattleDebateView";
import {
  CombatantState,
  EncounterState,
  useEncounterStream,
} from "../ws/useEncounterStream";

// ---------------------------------------------------------------------------
// Types (mirroring the API schemas used by this flow)
// ---------------------------------------------------------------------------

interface MonsterSummary {
  id: string;
  name: string;
  type: string;
  owner?: string;
  level?: number;
}

interface CaptureResult {
  success: boolean;
  monster: MonsterSummary | null;
  message: string;
}

interface TrainJob {
  job_id: string;
  monster_id: string;
  kind: "gepa" | "grpo";
  status: "queued" | "running" | "awaiting_preference" | "done" | "failed";
  score_delta?: number | null;
}

/** The five demo beats, in order. */
type Beat = "fight" | "capture" | "train" | "rematch" | "improved";
const BEAT_ORDER: Beat[] = ["fight", "capture", "train", "rematch", "improved"];
const BEAT_LABELS: Record<Beat, string> = {
  fight: "Fight",
  capture: "Capture",
  train: "Train",
  rematch: "Rematch",
  improved: "Improved",
};

// Baseline "training score" we show the BEFORE number against. GEPA returns a
// delta, not an absolute, so we anchor a stable baseline and add the delta for
// the AFTER number. This keeps the oversized "before → after" readable.
const TRAINING_SCORE_BASELINE = 50;

// ---------------------------------------------------------------------------
// Beat rail (always-visible progress markers)
// ---------------------------------------------------------------------------

function BeatRail({ current }: { current: Beat }) {
  const currentIdx = BEAT_ORDER.indexOf(current);
  return (
    <div className="flex items-center justify-center gap-1 px-3 py-2 select-none">
      {BEAT_ORDER.map((b, i) => {
        const done = i < currentIdx;
        const active = i === currentIdx;
        const tone = active
          ? "bg-amber-400 text-black border-amber-300 shadow-[0_0_18px_rgba(251,191,36,0.6)] scale-105"
          : done
            ? "bg-emerald-600/30 text-emerald-200 border-emerald-500/50"
            : "bg-white/5 text-white/40 border-white/10";
        return (
          <div key={b} className="flex items-center gap-1">
            <div
              className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-black uppercase tracking-wide transition-all duration-300 ${tone}`}
            >
              <span className="tabular-nums">{i + 1}</span>
              <span>{BEAT_LABELS[b]}</span>
            </div>
            {i < BEAT_ORDER.length - 1 && (
              <span className={`text-sm ${done ? "text-emerald-400" : "text-white/20"}`}>→</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Capture beat
// ---------------------------------------------------------------------------

function CaptureBeat({
  combatants,
  busy,
  result,
  error,
  onCapture,
  onContinue,
}: {
  combatants: CombatantState[];
  busy: boolean;
  result: CaptureResult | null;
  error: string | null;
  onCapture: (wildId: string) => void;
  onContinue: () => void;
}) {
  const enemy = combatants.find((c) => c.role === "enemy");
  const captured = result?.success === true;

  return (
    <div className="flex-1 grid place-items-center p-6">
      <div className="w-full max-w-xl text-center space-y-5">
        <div className="text-6xl">{captured ? "🟢" : "🎯"}</div>
        <div className="text-2xl font-black tracking-tight">
          {captured ? "CAPTURED" : "Capture the weakened debater"}
        </div>

        {enemy && (
          <div className="inline-flex items-center gap-2 rounded-lg border border-rose-500/40 bg-rose-950/40 px-4 py-2">
            <span className="text-xs uppercase tracking-wide text-rose-300">wild</span>
            <span className="font-bold">{enemy.name}</span>
            <span className="text-xs text-white/50">
              {enemy.hp}/{enemy.max_hp} HP
            </span>
          </div>
        )}

        {result && (
          <div
            className={`rounded-lg border px-4 py-3 text-sm ${
              captured
                ? "border-emerald-500/50 bg-emerald-950/40 text-emerald-200"
                : "border-yellow-500/50 bg-yellow-950/40 text-yellow-200"
            }`}
          >
            {result.message || (captured ? "It's yours." : "The capture slipped.")}
          </div>
        )}

        {error && (
          <div className="rounded-lg border border-red-500/50 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        <div className="flex items-center justify-center gap-3">
          {!captured && (
            <button
              disabled={busy || !enemy}
              onClick={() => enemy && onCapture(enemy.monster_id)}
              className="px-5 py-2.5 rounded-lg bg-yellow-600 hover:bg-yellow-500 disabled:opacity-40 font-bold"
            >
              {busy ? "Throwing…" : "Capture"}
            </button>
          )}
          {captured && (
            <button
              onClick={onContinue}
              className="px-5 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 font-bold"
            >
              Train it →
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Train beat (in-progress affordance)
// ---------------------------------------------------------------------------

function TrainBeat({
  monster,
  busy,
  error,
  onTrain,
}: {
  monster: MonsterSummary | null;
  busy: boolean;
  error: string | null;
  onTrain: () => void;
}) {
  return (
    <div className="flex-1 grid place-items-center p-6">
      <div className="w-full max-w-xl text-center space-y-5">
        <div className={`text-6xl ${busy ? "animate-pulse" : ""}`}>🧬</div>
        <div className="text-2xl font-black tracking-tight">
          {busy ? "Optimizing argument strategy…" : "Train your captured debater"}
        </div>
        {monster && (
          <div className="text-sm text-white/60">
            {monster.name}
            {monster.type ? ` · ${monster.type}` : ""}
          </div>
        )}

        {busy && (
          <div className="space-y-2">
            {/* Determinate-feeling progress affordance: GEPA blocks with no
                progress events, so we narrate the phases instead of faking a %. */}
            <div className="h-2 w-full overflow-hidden rounded bg-white/10">
              <div className="h-full w-1/2 animate-[pulse_1.2s_ease-in-out_infinite] rounded bg-emerald-500" />
            </div>
            <div className="text-xs font-mono text-emerald-300/80">
              GEPA self-play · reflecting on real battle memories · evolving genome…
            </div>
          </div>
        )}

        {error && (
          <div className="rounded-lg border border-red-500/50 bg-red-950/40 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {!busy && (
          <button
            disabled={!monster}
            onClick={onTrain}
            className="px-5 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 font-bold"
          >
            Run training (GEPA)
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Before / after — oversized training-score delta
// ---------------------------------------------------------------------------

function BeforeAfterDelta({
  job,
  onRematch,
}: {
  job: TrainJob | null;
  onRematch: () => void;
}) {
  const delta = job?.score_delta ?? 0;
  const before = TRAINING_SCORE_BASELINE;
  const after = Math.round(before + delta);
  const improved = delta > 0;

  return (
    <div className="flex-1 grid place-items-center p-6">
      <div className="w-full max-w-3xl text-center space-y-6">
        <div className="text-[11px] font-black uppercase tracking-[0.3em] text-white/50">
          training score
        </div>

        {improved ? (
          <div className="flex items-center justify-center gap-6 md:gap-10">
            <div className="space-y-1">
              <div className="text-[11px] uppercase tracking-widest text-white/40">before</div>
              <div className="text-6xl md:text-8xl font-black tabular-nums text-white/50">
                {before}
              </div>
            </div>
            <div className="text-5xl md:text-7xl font-black text-amber-400 animate-pulse">→</div>
            <div className="space-y-1">
              <div className="text-[11px] uppercase tracking-widest text-emerald-300">after</div>
              <div className="text-6xl md:text-8xl font-black tabular-nums text-emerald-400 drop-shadow-[0_0_24px_rgba(52,211,153,0.55)]">
                {after}
              </div>
            </div>
          </div>
        ) : (
          // Honest "held its ground" state — no faked improvement.
          <div className="space-y-3">
            <div className="text-5xl">🛡️</div>
            <div className="text-4xl md:text-6xl font-black tracking-tight text-white/80">
              HELD ITS GROUND
            </div>
            <div className="text-sm text-white/50">
              No variant beat the baseline this round (Δ {delta.toFixed(1)}). The genome was kept —
              training is real, so it does not always win.
            </div>
          </div>
        )}

        {improved && (
          <div className="text-lg font-black text-emerald-300 tabular-nums">
            +{delta.toFixed(1)} training score
          </div>
        )}

        <button
          onClick={onRematch}
          className="px-6 py-3 rounded-lg bg-indigo-600 hover:bg-indigo-500 font-black text-lg"
        >
          Rematch with the trained version →
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export function DemoArcPanel() {
  const { runId, activeEncounterId, setEncounter } = useGame();

  const [beat, setBeat] = useState<Beat>("fight");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Read live encounter state (combatants/phase) for the capture beat. We tap
  // the same WS hook BattleDebateView uses, but only for the combatant roster —
  // BattleDebateView remains the sole interactive surface during fights.
  const { encounter, phase } = useEncounterStream(
    beat === "fight" ? activeEncounterId : null,
  );

  const [captureResult, setCaptureResult] = useState<CaptureResult | null>(null);
  const [capturedMonster, setCapturedMonster] = useState<MonsterSummary | null>(null);
  const [trainJob, setTrainJob] = useState<TrainJob | null>(null);

  const combatants: CombatantState[] = encounter?.combatants ?? [];

  // Surface a "ready to capture" affordance once the fight reaches a capturable
  // (or resolved-win) phase — the orchestrated transition from beat 1 → 2.
  const fightCapturable = phase === "capturable" || phase === "won";

  function advance() {
    const i = BEAT_ORDER.indexOf(beat);
    if (i < BEAT_ORDER.length - 1) setBeat(BEAT_ORDER[i + 1]);
  }

  // ---- Beat 2: capture ----
  async function doCapture(wildId: string) {
    if (!activeEncounterId) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<CaptureResult>(
        `/api/encounters/${activeEncounterId}/capture`,
        { wild_id: wildId },
      );
      setCaptureResult(res);
      if (res.success && res.monster) {
        setCapturedMonster(res.monster);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // ---- Beat 3: train (GEPA, blocking) ----
  async function doTrain() {
    if (!capturedMonster) return;
    setBusy(true);
    setError(null);
    try {
      const job = await api.post<TrainJob>(
        `/api/monsters/${capturedMonster.id}/train/gepa`,
        { rounds: 1 },
      );
      setTrainJob(job);
      // Move straight into the before/after reveal once GEPA returns.
      setBeat("improved");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // ---- Beat 4: rematch (new encounter with the trained monster) ----
  async function doRematch() {
    if (!runId) {
      setError("No run id available for rematch.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // A new encounter reuses the player's party (now including the trained,
      // captured monster) against a fresh wild. BattleDebateView picks it up via
      // the global activeEncounterId.
      const enc = await api.post<EncounterState>("/api/encounters", {
        run_id: runId,
      });
      setEncounter(enc.id);
      setBeat("rematch");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // After a rematch is set, BattleDebateView swaps the store screen to
  // "encounter"; keep this panel mounted by re-asserting the rematch beat. (No
  // store widening needed — the embedded view drives the battle from here.)

  const trainedDelta = trainJob?.score_delta ?? null;

  const rematchBanner = useMemo(() => {
    if (trainedDelta == null) return "TRAINED VERSION";
    return trainedDelta > 0
      ? `TRAINED VERSION · +${trainedDelta.toFixed(1)} training score`
      : "TRAINED VERSION · held its ground";
  }, [trainedDelta]);

  return (
    <div className="flex flex-col h-full">
      <BeatRail current={beat} />

      <div className="flex-1 flex flex-col overflow-hidden border-t border-white/10">
        {/* Beat 1 — FIGHT (embeds the real battle view) */}
        {beat === "fight" && (
          <div className="flex flex-col flex-1 overflow-hidden">
            <div className="flex-1 overflow-hidden">
              <BattleDebateView />
            </div>
            <div className="border-t border-white/10 px-4 py-3 flex items-center gap-3">
              <span className="text-xs text-white/50">
                {fightCapturable
                  ? "The wild debater is on the ropes — capture it to start the arc."
                  : "Win the debate to weaken the wild debater enough to capture."}
              </span>
              <button
                disabled={!activeEncounterId}
                onClick={advance}
                className={`ml-auto px-4 py-2 rounded-lg font-bold disabled:opacity-40 ${
                  fightCapturable
                    ? "bg-yellow-600 hover:bg-yellow-500"
                    : "bg-white/10 hover:bg-white/20"
                }`}
              >
                {fightCapturable ? "Go to capture →" : "Skip to capture →"}
              </button>
            </div>
          </div>
        )}

        {/* Beat 2 — CAPTURE */}
        {beat === "capture" && (
          <CaptureBeat
            combatants={combatants}
            busy={busy}
            result={captureResult}
            error={error}
            onCapture={doCapture}
            onContinue={advance}
          />
        )}

        {/* Beat 3 — TRAIN */}
        {beat === "train" && (
          <TrainBeat
            monster={capturedMonster}
            busy={busy}
            error={error}
            onTrain={doTrain}
          />
        )}

        {/* Beat improved — BEFORE/AFTER training-score delta */}
        {beat === "improved" && (
          <BeforeAfterDelta job={trainJob} onRematch={doRematch} />
        )}

        {/* Beat 4 — REMATCH (embeds the real battle view, marked "trained") */}
        {beat === "rematch" && (
          <div className="flex flex-col flex-1 overflow-hidden">
            <div className="flex items-center gap-2 px-4 py-2 border-b border-amber-400/40 bg-amber-950/30">
              <span className="rounded-full bg-amber-400 px-2.5 py-0.5 text-xs font-black uppercase tracking-wide text-black">
                {rematchBanner}
              </span>
              {busy && <span className="text-xs text-white/50">Setting up rematch…</span>}
              {error && <span className="text-xs text-red-400">{error}</span>}
            </div>
            <div className="flex-1 overflow-hidden">
              <BattleDebateView />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default DemoArcPanel;
