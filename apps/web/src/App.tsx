import { useEffect, useState } from "react";
import { useGame } from "./state/store";
import { api } from "./api/client";
import Overworld from "./ui/Overworld";
import { BattleDebateView } from "./ui/BattleDebateView";
import PartyScreen from "./ui/PartyScreen";
import { GambitEditor } from "./ui/GambitEditor";
import TrainingScreen from "./ui/TrainingScreen";
import StartMenu from "./ui/StartMenu";
import { IrisTransitionProvider, useIrisTransition } from "./ui/fx/IrisWipe";

// Wave 2: real screens wired in.
//   overworld -> WS-A (Phaser canvas)
//   encounter -> WS-B/WS-C (BattleDebateView)
//   party     -> WS-E (PartyScreen) + WS-C GambitEditor via #gambits/{id} hash
//   training  -> WS-F (TrainingScreen)
export default function App() {
  return (
    <IrisTransitionProvider>
      <AppShell />
    </IrisTransitionProvider>
  );
}

function AppShell() {
  const { runId, screen, playerName, setScreen } = useGame();
  const { transition } = useIrisTransition();
  const [health, setHealth] = useState<string>("…");

  useEffect(() => {
    api
      .health()
      .then((h) => setHealth(h.status))
      .catch(() => setHealth("down"));
  }, []);

  return (
    <div className="min-h-screen flex flex-col">
      {runId && (
        <header
          className="flex items-center justify-between px-4 py-2"
          style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}
        >
          <h1 className="font-display text-sm">⚔️ DEBATE RPG</h1>
          <div className="flex items-center gap-3 font-hud text-[10px]">
            <span style={{ color: "var(--muted)" }}>
              api:{" "}
              <span style={{ color: health === "ok" ? "var(--win)" : "var(--warn)" }}>
                {health}
              </span>
            </span>
            <span style={{ color: "var(--muted)" }} className="truncate max-w-[16rem]">
              player: {playerName}
            </span>
          </div>
        </header>
      )}

      {!runId ? (
        <StartMenu />
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
                onClick={() => {
                  if (screen !== s) transition(() => setScreen(s));
                }}
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
