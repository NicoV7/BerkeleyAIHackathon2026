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
// THEME topics: the player picks a THEME at run start; each battle then draws a
// random topic WITHIN that theme (resolved server-side at encounter creation).
// Mirrors apps/api/app/debate/topics.py TOPICS_BY_THEME (examples shown to set
// expectations — the server is the source of truth for the actual draw).
const THEMES: { name: string; examples: string[] }[] = [
  {
    name: "Ethics",
    examples: ["Money can buy happiness.", "Free will is an illusion.", "Humans are inherently selfish."],
  },
  {
    name: "Technology",
    examples: ["AI should be open-sourced.", "Self-driving cars are safer.", "Privacy is dead and that's okay."],
  },
  {
    name: "Society",
    examples: ["Social media does more harm than good.", "A four-day work week.", "Tipping should be abolished."],
  },
  {
    name: "Science",
    examples: ["Nuclear energy fights climate change.", "We'll colonize Mars in 50 years.", "Aliens have visited Earth."],
  },
  {
    name: "Culture",
    examples: ["Pineapple belongs on pizza.", "Cats beat dogs.", "A hot dog is a sandwich."],
  },
];

export default function App() {
  const { runId, screen, topic, theme: runTheme, battleLocked, setRun, setScreen } = useGame();
  const [health, setHealth] = useState<string>("…");
  const [themeInput, setThemeInput] = useState(THEMES[0].name);

  useEffect(() => {
    api
      .health()
      .then((h) => setHealth(h.status))
      .catch(() => setHealth("down"));
  }, []);

  async function startRun() {
    try {
      const run = await api.post<{ id: string; debate_topic: string; theme?: string }>(
        "/api/runs",
        // topic kept for the NOT-NULL column; server labels the run by theme and
        // draws a random topic within the theme per battle.
        { topic: themeInput, theme: themeInput },
      );
      setRun(run.id, run.debate_topic, run.theme ?? themeInput);
    } catch {
      // /api/runs lands in WS-A; until then just enter the overworld locally.
      setRun("local-dev", themeInput, themeInput);
    }
  }

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
              theme: {runTheme || topic}
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
              Pick a THEME. Each battle draws a random topic from it.
            </p>
            <div className="grid grid-cols-1 gap-1.5 text-left">
              {THEMES.map((th) => {
                const selected = th.name === themeInput;
                return (
                  <button
                    key={th.name}
                    className={`pixel-btn w-full text-left px-3 py-2 ${
                      selected ? "pixel-btn--accent" : ""
                    }`}
                    onClick={() => setThemeInput(th.name)}
                    aria-pressed={selected}
                  >
                    <div className="font-display text-[11px]">{th.name}</div>
                    <div
                      className="font-body text-[8px] mt-0.5 leading-snug"
                      style={{ color: "var(--muted)" }}
                    >
                      e.g. {th.examples.join("  ·  ")}
                    </div>
                  </button>
                );
              })}
            </div>
            <button className="pixel-btn pixel-btn--accent w-full" onClick={startRun}>
              Start Run
            </button>
          </div>
        </main>
      ) : (
        <>
          {/* Battle isolation: while a battle is active (battleLocked), the
              global nav is replaced by a "locked" banner so the only way out is
              the in-battle Flee button (or a natural win/lose). */}
          {battleLocked ? (
            <div
              className="flex items-center gap-2 px-4 py-2 font-hud text-[10px]"
              style={{ borderBottom: "2px solid rgba(232,230,216,0.12)", color: "var(--warn)" }}
            >
              <span>⚔️ In battle</span>
              <span style={{ color: "var(--muted)" }}>
                — navigation locked. Flee to return to the overworld.
              </span>
            </div>
          ) : (
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
          )}
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
