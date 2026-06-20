import { useEffect, useState } from "react";
import { useGame } from "./state/store";
import { api } from "./api/client";
import Overworld from "./ui/Overworld";
import { BattleDebateView } from "./ui/BattleDebateView";
import PartyScreen from "./ui/PartyScreen";
import { GambitEditor } from "./ui/GambitEditor";
import TrainingScreen from "./ui/TrainingScreen";

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

  const SUGGESTED = [
    "Pineapple belongs on pizza",
    "AI should be open source",
    "Cats are better than dogs",
  ];

  return (
    <div className="min-h-screen flex flex-col">
      <header
        className="flex items-center justify-between px-4 py-2"
        style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}
      >
        <h1 className="font-display text-sm">⚔️ DEBATE RPG</h1>
        <div className="flex items-center gap-3 font-hud text-[10px]">
          <span style={{ color: "var(--muted)" }}>
            api:{" "}
            <span style={{ color: health === "ok" ? "var(--win)" : "var(--warn)" }}>{health}</span>
          </span>
          {runId && (
            <span style={{ color: "var(--muted)" }} className="truncate max-w-[16rem]">
              topic: {topic}
            </span>
          )}
        </div>
      </header>

      {!runId ? (
        <main className="flex-1 grid place-items-center p-4">
          <div className="pixel-panel p-6 w-[30rem] max-w-[92vw] space-y-4 text-center">
            <div className="font-display text-lg" style={{ color: "var(--accent)" }}>
              DEBATE RPG
            </div>
            <p className="font-body text-sm" style={{ color: "var(--muted)" }}>
              Choose the topic every enemy in this run will debate.
            </p>
            <input
              className="pixel-field w-full text-sm"
              value={topicInput}
              onChange={(e) => setTopicInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") startRun();
              }}
            />
            <div className="flex gap-1.5 flex-wrap justify-center">
              {SUGGESTED.map((t) => (
                <button
                  key={t}
                  className="pixel-btn text-[9px] py-1"
                  onClick={() => setTopicInput(t)}
                >
                  {t}
                </button>
              ))}
            </div>
            <button className="pixel-btn pixel-btn--accent w-full" onClick={startRun}>
              Start Run
            </button>
          </div>
        </main>
      ) : (
        <>
          <nav
            className="flex gap-2 px-4 py-2"
            style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}
          >
            {(["overworld", "encounter", "party", "training"] as const).map((s) => (
              <button
                key={s}
                className={`pixel-btn text-[10px] ${screen === s ? "pixel-btn--accent" : ""}`}
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
    case "party":
      if (gambitMonster) {
        return (
          <div className="p-4 max-w-3xl mx-auto">
            <button
              className="pixel-btn text-[10px] mb-3"
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
