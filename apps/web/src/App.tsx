import { useEffect, useState } from "react";
import { useGame, type Screen } from "./state/store";
import { api } from "./api/client";
import Overworld from "./ui/Overworld";
import { BattleDebateView } from "./ui/BattleDebateView";
import PartyScreen from "./ui/PartyScreen";
import { GambitEditor } from "./ui/GambitEditor";
import TrainingScreen from "./ui/TrainingScreen";
import GachaScreen from "./ui/GachaScreen";
import StartMenu from "./ui/StartMenu";
import { Overlay } from "./ui/Overlay";
import { BattleOverlay } from "./ui/BattleOverlay";
import { IrisTransitionProvider } from "./ui/fx/IrisWipe";

export default function App() {
  return (
    <IrisTransitionProvider>
      <AppShell />
    </IrisTransitionProvider>
  );
}

function AppShell() {
  const { runId, screen, activeEncounterId, setScreen } = useGame();
  const [needsGacha, setNeedsGacha] = useState<boolean | null>(null);
  const [gambitMonster, setGambitMonster] = useState<string | null>(null);

  // Gacha gate: run with empty party → funnel through pull cinematic first.
  useEffect(() => {
    let cancelled = false;
    if (!runId) { setNeedsGacha(null); return; }
    setNeedsGacha(null);
    (async () => {
      try {
        const r = await api.get<{ party?: unknown[] }>(`/api/runs/${runId}`);
        if (cancelled) return;
        setNeedsGacha(((r?.party ?? []) as unknown[]).length === 0);
      } catch {
        if (!cancelled) setNeedsGacha(false);
      }
    })();
    return () => { cancelled = true; };
  }, [runId]);

  // Gambit sub-screen: PartyScreen signals via URL hash.
  useEffect(() => {
    const sync = () => {
      const m = window.location.hash.match(/^#?gambits\/(.+)$/);
      setGambitMonster(m ? m[1] : null);
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  // No run yet — show start menu full-screen.
  if (!runId) return <StartMenu />;

  const inBattle = screen === "encounter" && !!activeEncounterId;

  return (
    <div style={{ position: "fixed", inset: 0, overflow: "hidden" }}>
      {/* BASE LAYER — Overworld never unmounts once a run is active */}
      <Overworld />

      {/* Gacha gate — opaque, blocks world until first pull completes */}
      {needsGacha && (
        <BattleOverlay>
          <GachaScreen onReady={() => { setNeedsGacha(false); setScreen("overworld"); }} />
        </BattleOverlay>
      )}

      {/* ENCOUNTER — opaque overlay; entered/exited via iris wipe */}
      {inBattle && (
        <BattleOverlay>
          <BattleDebateView />
        </BattleOverlay>
      )}

      {/* PARTY — dimmed scrim over the living world */}
      {screen === "party" && !needsGacha && !inBattle && (
        <Overlay label="Party" onClose={() => setScreen("overworld")}>
          {gambitMonster ? (
            <div>
              <button
                className="pixel-btn text-[10px] mb-3"
                onClick={() => { window.location.hash = ""; }}
              >
                ← back to party
              </button>
              <GambitEditor monsterId={gambitMonster} />
            </div>
          ) : (
            <PartyScreen />
          )}
        </Overlay>
      )}

      {/* TRAINING — dimmed scrim over the living world */}
      {screen === "training" && !needsGacha && !inBattle && (
        <Overlay label="Training Lab" onClose={() => setScreen("overworld")}>
          <TrainingScreen />
        </Overlay>
      )}

      {/* HUD NAV — floating buttons, hidden during battle and gacha */}
      {!inBattle && !needsGacha && (
        <HudNav screen={screen} onSetScreen={setScreen} />
      )}
    </div>
  );
}

function HudNav({
  screen,
  onSetScreen,
}: {
  screen: string;
  onSetScreen: (s: Screen) => void;
}) {
  return (
    <div className="pointer-events-auto fixed bottom-4 right-4 z-10 flex gap-2">
      {(["party", "training"] as const).map((s) => (
        <button
          key={s}
          className={`pixel-btn text-[10px] ${screen === s ? "pixel-btn--accent" : ""}`}
          onClick={() => onSetScreen(screen === s ? "overworld" : s)}
        >
          {s}
        </button>
      ))}
    </div>
  );
}
