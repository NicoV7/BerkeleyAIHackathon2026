import { useEffect, useState } from "react";
import { useGame } from "./state/store";
import { api } from "./api/client";
import Overworld from "./ui/Overworld";
import { BattleDebateView } from "./ui/BattleDebateView";
import PartyScreen from "./ui/PartyScreen";
import { GambitEditor } from "./ui/GambitEditor";
import TrainingScreen from "./ui/TrainingScreen";
import GachaScreen from "./ui/GachaScreen";
import StartMenu from "./ui/StartMenu";
import { IrisTransitionProvider, useIrisTransition } from "./ui/fx/IrisWipe";
import { AdventureMenu } from "./ui/shell/AdventureMenu";
import { OverlayHost } from "./ui/shell/OverlayHost";

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
  const { runId, screen, topic, theme: runTheme, playerName, battleLocked, setScreen } = useGame();
  const { transition } = useIrisTransition();
  const [health, setHealth] = useState<string>("…");
  // Gacha gate (Wave A): when a run is loaded with an empty party, the player
  // is funneled through the gacha pull cinematic before reaching the overworld.
  // `null` = unknown (still checking), `true` = show gacha, `false` = ok.
  const [needsGacha, setNeedsGacha] = useState<boolean | null>(null);

  useEffect(() => {
    api
      .health()
      .then((h) => setHealth(h.status))
      .catch(() => setHealth("down"));
  }, []);

  // Whenever the active run changes, ask the backend whether the party is
  // empty and gate on it. Backend: GET /api/runs/{id} returns a `party` array.
  useEffect(() => {
    let cancelled = false;
    if (!runId) {
      setNeedsGacha(null);
      return;
    }
    setNeedsGacha(null);
    (async () => {
      try {
        const r = await api.get<{ party?: unknown[] }>(`/api/runs/${runId}`);
        if (cancelled) return;
        setNeedsGacha(((r?.party ?? []) as unknown[]).length === 0);
      } catch {
        if (!cancelled) setNeedsGacha(false); // fail-open so we don't block the player
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);

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
              <span style={{ color: health === "ok" ? "var(--win)" : "var(--warn)" }}>{health}</span>
            </span>
            <span style={{ color: "var(--muted)" }} className="truncate max-w-[16rem]">
              player: {playerName}
            </span>
            <span style={{ color: "var(--muted)" }} className="truncate max-w-[16rem]">
              theme: {runTheme || topic}
            </span>
          </div>
        </header>
      )}

      {!runId ? (
        <StartMenu />
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
          ) : needsGacha ? (
            <div
              className="flex items-center gap-2 px-4 py-2 font-hud text-[10px]"
              style={{ borderBottom: "2px solid rgba(232,230,216,0.12)", color: "var(--accent)" }}
            >
              <span>🎰 Summon required</span>
              <span style={{ color: "var(--muted)" }}>
                — pull your first persona to enter the world.
              </span>
            </div>
          ) : (
            <nav
              className="flex items-center gap-2 px-4 py-2"
              style={{ borderBottom: "2px solid rgba(232,230,216,0.12)" }}
            >
              {/* TODO(WS-6): this tab strip is the screen-restructure surface.
                  WS-6 owns removing the "encounter"/"training" tabs (encounter
                  becomes battle-only; training moves elsewhere). See
                  UI_CONTRACT.md §Planned tab removal for the setScreen audit. */}
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
              {/* Adventure menu: persistent entry into Inventory/Quests/Map. */}
              <AdventureMenu className="ml-auto" />
            </nav>
          )}
          <main className="flex-1 overflow-auto">
            {needsGacha ? (
              <GachaScreen
                onReady={() => {
                  setNeedsGacha(false);
                  setScreen("overworld");
                }}
              />
            ) : (
              <ScreenPanel screen={screen} />
            )}
          </main>
          {/* Overlay surfaces (Inventory/Quests/Map) float above everything but
              the iris transition. Suppressed during battle + gacha gate because
              setEncounter/needsGacha already clear/guard the overlay. */}
          {!battleLocked && !needsGacha ? <OverlayHost /> : null}
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
