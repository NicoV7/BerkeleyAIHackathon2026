// TrainingScreen (WS-F) — pick a party member, run GEPA (show score delta), or
// run a GRPO-HITL cycle: show K transcripts, rank them, submit, show the
// adopted result. Self-contained; wired into App.tsx in Wave 2.
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";

interface MonsterSummary {
  id: string;
  name: string;
  type: string;
  level: number;
}
interface RunState {
  id: string;
  party: MonsterSummary[];
}
interface Utterance {
  turn: number;
  actor_id: string;
  actor_role: "party" | "enemy" | "judge";
  text: string;
}
interface TrainJob {
  job_id: string;
  monster_id: string;
  kind: "gepa" | "grpo";
  status: string;
  score_delta?: number | null;
}
interface PreferenceVariant {
  variant_id: string;
  transcript: Utterance[];
  judge_score: number;
}
interface PreferenceBatch {
  job_id: string;
  monster_id: string;
  variants: PreferenceVariant[];
}

export default function TrainingScreen() {
  const { runId } = useGame();
  const [party, setParty] = useState<MonsterSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // GEPA
  const [gepaDelta, setGepaDelta] = useState<number | null>(null);

  // GRPO
  const [batch, setBatch] = useState<PreferenceBatch | null>(null);
  const [ranking, setRanking] = useState<string[]>([]);
  const [adopted, setAdopted] = useState<TrainJob | null>(null);

  useEffect(() => {
    if (!runId) return;
    api
      .get<RunState>(`/api/runs/${runId}/party`)
      .then((r) => {
        const p = (r as any).party ?? (r as any) ?? [];
        const list: MonsterSummary[] = Array.isArray(p) ? p : p.party ?? [];
        setParty(list);
        if (list.length && !selected) setSelected(list[0].id);
      })
      .catch((e) => setError(`Could not load party: ${e.message}`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  async function runGepa() {
    if (!selected) return;
    setBusy("gepa");
    setError(null);
    setGepaDelta(null);
    try {
      const job = await api.post<TrainJob>(`/api/monsters/${selected}/train/gepa`, {
        rounds: 2,
      });
      setGepaDelta(job.score_delta ?? 0);
    } catch (e: any) {
      setError(`GEPA failed: ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  async function startGrpo() {
    if (!selected) return;
    setBusy("grpo");
    setError(null);
    setBatch(null);
    setAdopted(null);
    setRanking([]);
    try {
      const b = await api.post<PreferenceBatch>(`/api/monsters/${selected}/train/grpo`, {});
      setBatch(b);
      // default ranking: order as returned (player can reorder)
      setRanking(b.variants.map((v) => v.variant_id));
    } catch (e: any) {
      setError(`GRPO start failed: ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  function move(variantId: string, dir: -1 | 1) {
    setRanking((cur) => {
      const i = cur.indexOf(variantId);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= cur.length) return cur;
      const next = [...cur];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }

  async function submitRanking() {
    if (!batch) return;
    setBusy("submit");
    setError(null);
    try {
      const job = await api.post<TrainJob>(`/api/training/${batch.job_id}/preference`, {
        ranking,
      });
      setAdopted(job);
    } catch (e: any) {
      setError(`Submit failed: ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  const variantsByRank = batch
    ? ranking.map((id) => batch.variants.find((v) => v.variant_id === id)!).filter(Boolean)
    : [];

  return (
    <div className="p-4 space-y-6 max-w-3xl mx-auto text-sm">
      <h2 className="text-lg font-bold">🧬 Training Lab</h2>

      {/* Party picker */}
      <section className="space-y-2">
        <div className="opacity-70">Pick a party member to train:</div>
        <div className="flex flex-wrap gap-2">
          {party.length === 0 && <span className="opacity-50">No party members loaded.</span>}
          {party.map((m) => (
            <button
              key={m.id}
              onClick={() => setSelected(m.id)}
              className={`px-3 py-1 rounded border ${
                selected === m.id
                  ? "bg-indigo-600 border-indigo-400"
                  : "bg-white/5 border-white/10 hover:bg-white/10"
              }`}
            >
              {m.name} <span className="opacity-60">· {m.type} · L{m.level}</span>
            </button>
          ))}
        </div>
      </section>

      {error && <div className="text-red-400 bg-red-950/40 rounded px-3 py-2">{error}</div>}

      {/* GEPA */}
      <section className="space-y-2 border border-white/10 rounded p-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-semibold">GEPA — reflective prompt evolution</div>
            <div className="opacity-60 text-xs">
              Offline self-play + LLM critique. Returns a genome score delta.
            </div>
          </div>
          <button
            disabled={!selected || busy !== null}
            onClick={runGepa}
            className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40"
          >
            {busy === "gepa" ? "Evolving…" : "Run GEPA"}
          </button>
        </div>
        {gepaDelta !== null && (
          <div
            className={`text-sm font-mono ${gepaDelta >= 0 ? "text-emerald-400" : "text-yellow-400"}`}
          >
            score delta: {gepaDelta >= 0 ? "+" : ""}
            {gepaDelta.toFixed(1)} — genome {gepaDelta >= 0 ? "adopted" : "kept (no gain)"}
          </div>
        )}
      </section>

      {/* GRPO-HITL */}
      <section className="space-y-3 border border-white/10 rounded p-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-semibold">GRPO-HITL — rank K variants</div>
            <div className="opacity-60 text-xs">
              Sample 3 mutations, roll out, you rank the transcripts, best is adopted.
            </div>
          </div>
          <button
            disabled={!selected || busy !== null}
            onClick={startGrpo}
            className="px-3 py-1.5 rounded bg-sky-600 hover:bg-sky-500 disabled:opacity-40"
          >
            {busy === "grpo" ? "Rolling out…" : "Start GRPO cycle"}
          </button>
        </div>

        {batch && (
          <div className="space-y-3">
            <div className="opacity-70 text-xs">
              Rank best → worst with the arrows, then submit.
            </div>
            {variantsByRank.map((v, idx) => (
              <div key={v.variant_id} className="border border-white/10 rounded p-2 space-y-1">
                <div className="flex items-center justify-between">
                  <div className="font-semibold">
                    #{idx + 1} · Variant {v.variant_id.slice(0, 6)}{" "}
                    <span className="opacity-60 font-mono">judge {v.judge_score.toFixed(0)}</span>
                  </div>
                  <div className="flex gap-1">
                    <button
                      onClick={() => move(v.variant_id, -1)}
                      className="px-2 rounded bg-white/10 hover:bg-white/20"
                    >
                      ↑
                    </button>
                    <button
                      onClick={() => move(v.variant_id, 1)}
                      className="px-2 rounded bg-white/10 hover:bg-white/20"
                    >
                      ↓
                    </button>
                  </div>
                </div>
                <div className="space-y-0.5 max-h-40 overflow-auto text-xs">
                  {v.transcript
                    .filter((u) => u.actor_role !== "judge")
                    .map((u, i) => (
                      <div key={i}>
                        <span
                          className={
                            u.actor_role === "party" ? "text-emerald-400" : "text-rose-400"
                          }
                        >
                          {u.actor_role}:
                        </span>{" "}
                        {u.text}
                      </div>
                    ))}
                </div>
              </div>
            ))}
            <button
              disabled={busy !== null || !!adopted}
              onClick={submitRanking}
              className="px-3 py-1.5 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40"
            >
              {busy === "submit" ? "Submitting…" : "Submit ranking"}
            </button>
          </div>
        )}

        {adopted && (
          <div className="text-emerald-400 font-mono text-sm">
            ✓ adopted top variant · score delta {adopted.score_delta?.toFixed(2) ?? "?"} · status{" "}
            {adopted.status}
          </div>
        )}
      </section>
    </div>
  );
}
