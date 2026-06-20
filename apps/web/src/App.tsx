import { useEffect, useState } from "react";
import { useGame } from "./state/store";
import { api } from "./api/client";

// Wave 0 shell: a menu that starts a run + a health badge + screen switch.
// Wave 1 workstreams replace the placeholder panels:
//   overworld -> WS-A (Phaser canvas)
//   encounter -> WS-B/WS-C (BattleDebateView)
//   party     -> WS-E (PartyScreen)
//   training  -> WS-F (TrainingScreen)
export default function App() {
  const { runId, screen, topic, setRun, setScreen } = useGame();
  const [health, setHealth] = useState<string>("…");
  const [topicInput, setTopicInput] = useState("Pineapple belongs on pizza");

  useEffect(() => {
    api
      .health()
      .then((h) => setHealth(h.status))
      .catch(() => setHealth("down"));
  }, []);

  async function startRun() {
    try {
      const run = await api.post<{ id: string; debate_topic: string }>("/api/runs", {
        topic: topicInput,
      });
      setRun(run.id, run.debate_topic);
    } catch {
      // /api/runs lands in WS-A; until then just enter the overworld locally.
      setRun("local-dev", topicInput);
    }
  }

  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center justify-between px-4 py-2 border-b border-white/10">
        <h1 className="font-bold tracking-tight">⚔️ Debate RPG</h1>
        <div className="flex items-center gap-3 text-xs">
          <span>
            api:{" "}
            <span className={health === "ok" ? "text-green-400" : "text-yellow-400"}>{health}</span>
          </span>
          {runId && <span className="opacity-60">topic: {topic}</span>}
        </div>
      </header>

      {!runId ? (
        <main className="flex-1 grid place-items-center">
          <div className="w-[28rem] max-w-[90vw] space-y-3 text-center">
            <p className="opacity-70 text-sm">
              Choose the topic every enemy in this run will debate.
            </p>
            <input
              className="w-full bg-white/5 border border-white/10 rounded px-3 py-2"
              value={topicInput}
              onChange={(e) => setTopicInput(e.target.value)}
            />
            <button
              className="w-full bg-indigo-600 hover:bg-indigo-500 rounded px-3 py-2 font-semibold"
              onClick={startRun}
            >
              Start Run
            </button>
          </div>
        </main>
      ) : (
        <>
          <nav className="flex gap-2 px-4 py-2 border-b border-white/10 text-sm">
            {(["overworld", "encounter", "party", "training"] as const).map((s) => (
              <button
                key={s}
                className={`px-3 py-1 rounded ${
                  screen === s ? "bg-indigo-600" : "bg-white/5 hover:bg-white/10"
                }`}
                onClick={() => setScreen(s)}
              >
                {s}
              </button>
            ))}
          </nav>
          <main className="flex-1 grid place-items-center">
            <PlaceholderPanel screen={screen} />
          </main>
        </>
      )}
    </div>
  );
}

function PlaceholderPanel({ screen }: { screen: string }) {
  const owners: Record<string, string> = {
    overworld: "WS-A — Phaser tile overworld mounts here",
    encounter: "WS-B/WS-C — live debate battle UI mounts here",
    party: "WS-E — party + capture screen mounts here",
    training: "WS-F — GEPA/GRPO training UI mounts here",
  };
  return (
    <div className="text-center opacity-50">
      <div className="text-5xl mb-3">🚧</div>
      <div className="font-mono text-sm">{owners[screen] ?? screen}</div>
    </div>
  );
}
