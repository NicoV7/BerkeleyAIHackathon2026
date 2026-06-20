// TrainingScreen (WS-F) — pick a party member, run GEPA (show score delta), or
// run a GRPO-HITL cycle: show K transcripts, rank them, submit, show the
// adopted result. Self-contained; wired into App.tsx in Wave 2.
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useGame } from "../state/store";
import { ReasoningTrend, type TrendSeries } from "./ReasoningTrend";

interface MonsterSummary {
  id: string;
  name: string;
  type: string;
  level: number;
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

// Nominal baseline for the agent's reasoning curve before the first cycle.
const AGENT_BASELINE = 60;

export default function TrainingScreen() {
  const { runId, lastYouScores } = useGame();
  const [party, setParty] = useState<MonsterSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Agent reasoning curve: each adopted training cycle nudges it by score_delta.
  const [agentScores, setAgentScores] = useState<number[]>([]);
  const [lastDelta, setLastDelta] = useState<number | null>(null);

  function recordDelta(delta: number) {
    setLastDelta(delta);
    setAgentScores((prev) => {
      const base = prev.length ? prev[prev.length - 1] : AGENT_BASELINE;
      const next = Math.max(0, Math.min(100, base + delta));
      return prev.length ? [...prev, next] : [AGENT_BASELINE, next];
    });
  }

  // GEPA
  const [gepaDelta, setGepaDelta] = useState<number | null>(null);

  // GRPO
  const [batch, setBatch] = useState<PreferenceBatch | null>(null);
  const [ranking, setRanking] = useState<string[]>([]);
  const [adopted, setAdopted] = useState<TrainJob | null>(null);

  // Reset the agent curve whenever the selected monster changes.
  useEffect(() => {
    setAgentScores([]);
    setLastDelta(null);
    setGepaDelta(null);
    setBatch(null);
    setAdopted(null);
    setRanking([]);
  }, [selected]);

  useEffect(() => {
    if (!runId) return;
    api
      .get<MonsterSummary[]>(`/api/runs/${runId}/party`)
      .then((list) => {
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
      const delta = job.score_delta ?? 0;
      setGepaDelta(delta);
      recordDelta(delta);
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
      recordDelta(job.score_delta ?? 0);
    } catch (e: any) {
      setError(`Submit failed: ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  const variantsByRank = batch
    ? ranking.map((id) => batch.variants.find((v) => v.variant_id === id)!).filter(Boolean)
    : [];

  const agentBefore = agentScores.length ? agentScores[0] : null;
  const agentAfter = agentScores.length ? agentScores[agentScores.length - 1] : null;
  const progressSeries: TrendSeries[] = [];
  if (lastYouScores.length)
    progressSeries.push({ label: "You", color: "var(--party)", points: lastYouScores });
  if (agentScores.length)
    progressSeries.push({ label: "Trained agent", color: "var(--accent)", points: agentScores });

  return (
    <div className="p-4 space-y-6 max-w-3xl mx-auto text-sm">
      <h2 className="font-display text-base">🧬 Training Lab</h2>

      {/* Dual money shot: human curve beside machine curve */}
      {(progressSeries.length > 0 || lastDelta !== null) && (
        <section className="pixel-panel p-3 space-y-2">
          <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
            Progress — human beside machine
          </div>
          {lastDelta !== null && agentBefore !== null && agentAfter !== null && (
            <div className="font-display text-xl" style={{ color: "var(--accent)" }}>
              Reasoning {Math.round(agentBefore)} → {Math.round(agentAfter)}{" "}
              <span style={{ color: lastDelta >= 0 ? "var(--win)" : "var(--danger)" }}>
                {lastDelta >= 0 ? "+" : ""}
                {lastDelta.toFixed(0)}
              </span>
            </div>
          )}
          {progressSeries.length > 0 ? (
            <ReasoningTrend series={progressSeries} title="Reasoning score / round" height={150} />
          ) : (
            <div className="font-body text-xs" style={{ color: "var(--muted)" }}>
              Run a training cycle to see the agent's curve; argue in a battle to see yours.
            </div>
          )}
        </section>
      )}

      {/* Party picker */}
      <section className="space-y-2">
        <div className="opacity-70">Pick a party member to train:</div>
        <div className="flex flex-wrap gap-2">
          {party.length === 0 && <span className="opacity-50">No party members loaded.</span>}
          {party.map((m) => (
            <button
              key={m.id}
              onClick={() => setSelected(m.id)}
              className={selected === m.id ? "pixel-btn pixel-btn--accent" : "pixel-btn"}
            >
              {m.name} <span className="opacity-60">· {m.type} · L{m.level}</span>
            </button>
          ))}
        </div>
      </section>

      {error && (
        <div
          className="pixel-panel px-3 py-2 font-body"
          style={{ borderColor: "var(--danger)", color: "var(--danger)" }}
        >
          {error}
        </div>
      )}

      {/* GEPA */}
      <section className="space-y-2 pixel-panel p-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-hud text-sm">GEPA — reflective prompt evolution</div>
            <div className="font-body text-xs" style={{ color: "var(--muted)" }}>
              Offline self-play + LLM critique. Returns a genome score delta.
            </div>
          </div>
          <button
            disabled={!selected || busy !== null}
            onClick={runGepa}
            className="pixel-btn pixel-btn--party"
          >
            {busy === "gepa" ? "Evolving…" : "Run GEPA"}
          </button>
        </div>
        {gepaDelta !== null && (
          <div
            className="text-sm font-body"
            style={{ color: gepaDelta >= 0 ? "var(--win)" : "var(--warn)" }}
          >
            score delta: {gepaDelta >= 0 ? "+" : ""}
            {gepaDelta.toFixed(1)} — genome {gepaDelta >= 0 ? "adopted" : "kept (no gain)"}
          </div>
        )}
      </section>

      {/* GRPO-HITL */}
      <section className="space-y-3 pixel-panel p-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-hud text-sm">GRPO-HITL — rank K variants</div>
            <div className="font-body text-xs" style={{ color: "var(--muted)" }}>
              Sample 3 mutations, roll out, you rank the transcripts, best is adopted.
            </div>
          </div>
          <button
            disabled={!selected || busy !== null}
            onClick={startGrpo}
            className="pixel-btn pixel-btn--party"
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
              <div key={v.variant_id} className="pixel-inset p-2 space-y-1">
                <div className="flex items-center justify-between">
                  <div className="font-hud text-xs">
                    #{idx + 1} · Variant {v.variant_id.slice(0, 6)}{" "}
                    <span className="font-body" style={{ color: "var(--muted)" }}>
                      judge {v.judge_score.toFixed(0)}
                    </span>
                  </div>
                  <div className="flex gap-1">
                    <button
                      onClick={() => move(v.variant_id, -1)}
                      className="pixel-btn"
                      style={{ padding: "2px 6px" }}
                    >
                      ↑
                    </button>
                    <button
                      onClick={() => move(v.variant_id, 1)}
                      className="pixel-btn"
                      style={{ padding: "2px 6px" }}
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
                          style={{
                            color: u.actor_role === "party" ? "var(--party)" : "var(--enemy)",
                          }}
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
              className="pixel-btn pixel-btn--accent"
            >
              {busy === "submit" ? "Submitting…" : "Submit ranking"}
            </button>
          </div>
        )}

        {adopted && (
          <div className="font-body text-sm" style={{ color: "var(--win)" }}>
            ✓ adopted top variant · score delta {adopted.score_delta?.toFixed(2) ?? "?"} · status{" "}
            {adopted.status}
          </div>
        )}
      </section>
    </div>
  );
}
