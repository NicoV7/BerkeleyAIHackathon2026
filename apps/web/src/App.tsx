import { useEffect, useState } from "react";
import { useGame } from "./state/store";
import { api } from "./api/client";
import Overworld from "./ui/Overworld";
import { BattleDebateView } from "./ui/BattleDebateView";
import PartyScreen from "./ui/PartyScreen";
import { GambitEditor } from "./ui/GambitEditor";
import TrainingScreen from "./ui/TrainingScreen";
import DemoArcPanel from "./ui/DemoArcPanel";

// Wave 2: real screens wired in.
//   overworld -> WS-A (Phaser canvas)
//   encounter -> WS-B/WS-C (BattleDebateView)
//   party     -> WS-E (PartyScreen) + WS-C GambitEditor via #gambits/{id} hash
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
            {(["overworld", "encounter", "party", "training", "demo"] as const).map((s) => (
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
          <main className="flex-1 overflow-auto">
            <ScreenPanel screen={screen} />
          </main>
        </>
      )}
    </div>
  );
}

function ScreenPanel({ screen }: { screen: string }) {
  // PartyScreen signals "edit gambits" via window.location.hash = gambits/{id}.
  const [gambitMonster, setGambitMonster] = useState<string | null>(null);
  useEffect(() => {
    const sync = () => {
      const m = window.location.hash.match(/^#?gambits\/(.+)$/);
      setGambitMonster(m ? m[1] : null);
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  switch (screen) {
    case "overworld":
      return <Overworld />;
    case "encounter":
      return <BattleDebateView />;
    case "training":
      return <TrainingScreen />;
    case "demo":
      return <DemoArcPanel />;
    case "party":
      if (gambitMonster) {
        return (
          <div className="p-4 max-w-3xl mx-auto">
            <button
              className="mb-3 text-sm px-3 py-1 rounded bg-white/5 hover:bg-white/10"
              onClick={() => {
                window.location.hash = "";
              }}
            >
              ← back to party
            </button>
            <GambitEditor monsterId={gambitMonster} />
          </div>
        );
      }
      return <PartyScreen />;
    default:
      return <div className="grid place-items-center h-full opacity-50">{screen}</div>;
  }
}
