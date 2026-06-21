/**
 * Overworld.tsx — React wrapper that mounts the Phaser overworld canvas.
 *
 * Exported as the default export so App.tsx (Wave 2 wiring) can do:
 *   import Overworld from "@/ui/Overworld"
 *
 * The component:
 * - Sizes the Phaser canvas to the viewport and resizes in place.
 * - Passes runId, encounter, HUD, and NPC callbacks into OverworldScene.
 * - On encounter collision: calls POST /api/encounters (WS-B), then setEncounter.
 */

import Phaser from "phaser";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { buildEncounterBridge } from "../game/EncounterTrigger";
import NPCDialogue from "../game/NPCDialogue";
import type { NPCAnchorView } from "../game/NPCBehavior";
import { OverworldScene } from "../game/OverworldScene";
import { useGame } from "../state/store";
import OverworldHud, { type HudMap } from "./OverworldHud";
import { useIrisTransition } from "./fx/IrisWipe";

// Cast helper so we can call public methods on the typed scene.
function getOverworldScene(game: Phaser.Game | null): OverworldScene | null {
  return (game?.scene?.getScene?.("OverworldScene") as OverworldScene) ?? null;
}

export default function Overworld() {
  const { runId, playerName, activeEncounterId, setEncounter } = useGame();
  const { transition } = useIrisTransition();
  const rootRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const gameRef = useRef<Phaser.Game | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [hudMap, setHudMap] = useState<HudMap | null>(null);
  const [playerPos, setPlayerPos] = useState<{ x: number; y: number } | null>(null);
  const [activeNpc, setActiveNpc] = useState<NPCAnchorView | null>(null);

  useLayoutEffect(() => {
    const measure = () => {
      const rect = rootRef.current?.getBoundingClientRect();
      const width = rect?.width ?? window.innerWidth;
      const height = rect?.height ?? window.innerHeight;
      setSize({ w: Math.floor(width), h: Math.floor(height) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    if (rootRef.current) observer.observe(rootRef.current);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  useEffect(() => {
    setActiveNpc(null);
    setHudMap(null);
    setPlayerPos(null);
  }, [runId]);

  useEffect(() => {
    if (!containerRef.current || !runId || size.w === 0 || size.h === 0) return;

    const bridge = buildEncounterBridge(runId, (id) => {
      transition(() => setEncounter(id));
    });

    const config: Phaser.Types.Core.GameConfig = {
      type: Phaser.AUTO,
      backgroundColor: "#0e1018",
      parent: containerRef.current,
      scene: [OverworldScene],
      render: { pixelArt: true, antialias: false, roundPixels: true },
      scale: { mode: Phaser.Scale.RESIZE, width: size.w, height: size.h },
      audio: { noAudio: true },
    };

    const game = new Phaser.Game(config);
    gameRef.current = game;
    let cancelled = false;

    const startScene = () => {
      if (cancelled || !gameRef.current) return;
      gameRef.current.scene.start("OverworldScene", {
        runId,
        playerName,
        onEncounter: (wildId?: string | null, locationTile?: number | null) => {
          void bridge.onCollision(wildId, locationTile);
        },
        onNpcTalk: (npc: NPCAnchorView) => {
          setActiveNpc(npc);
        },
        onMapLoaded: (m: HudMap) => setHudMap(m),
        onPlayerMove: (x: number, y: number) => setPlayerPos({ x, y }),
      });
    };
    if (game.isBooted) startScene();
    else game.events.once("ready", startScene);

    return () => {
      cancelled = true;
      game.destroy(true);
      gameRef.current = null;
    };
    // Resize changes are handled below without tearing down the Phaser game.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, setEncounter, size.w === 0 || size.h === 0]);

  useEffect(() => {
    if (gameRef.current && size.w > 0 && size.h > 0) {
      gameRef.current.scale.resize(size.w, size.h);
    }
  }, [size]);

  // Pause the Phaser scene during battle; reset encounter latch flags on return.
  const wasInEncounterRef = useRef(false);
  useEffect(() => {
    const game = gameRef.current;
    const scene = getOverworldScene(game);
    if (!scene || !game) return;
    if (activeEncounterId) {
      wasInEncounterRef.current = true;
      scene.scene.pause();
      // Disable Phaser's GLOBAL KeyboardManager (not the per-scene plugin). The
      // manager is what calls preventDefault() on the WASD/arrow captures at the
      // window level; its onKeyDown short-circuits on this `enabled` flag BEFORE
      // preventDefault, so toggling it here lets WASD/Space reach the battle
      // textarea. The scene-plugin `enabled` flag does NOT gate that path.
      if (game.input?.keyboard) game.input.keyboard.enabled = false;
    } else if (wasInEncounterRef.current) {
      wasInEncounterRef.current = false;
      if (game.input?.keyboard) game.input.keyboard.enabled = true;
      scene.scene.resume();
      // Reset the encounterFired/encounterPending latches so update() runs again.
      scene.resetAfterBattle();
    }
  }, [activeEncounterId]);

  if (!runId) {
    return (
      <div ref={rootRef} className="flex items-center justify-center w-full h-full">
        <p className="font-body text-sm" style={{ color: "var(--muted)" }}>
          No active run — start a run first.
        </p>
      </div>
    );
  }

  return (
    <div
      ref={rootRef}
      className="relative w-full h-full overflow-hidden"
    >
      <div ref={containerRef} className="absolute inset-0" />
      <div
        className="pointer-events-none absolute left-1/2 top-3 z-10 -translate-x-1/2 font-hud text-[10px]"
        style={{ color: "var(--muted)" }}
      >
        Arrow keys / WASD to move · Walk into a red enemy to battle
      </div>
      <OverworldHud map={hudMap} player={playerPos} runId={runId} />
      <NPCDialogue
        runId={runId}
        npc={activeNpc}
        onClose={() => setActiveNpc(null)}
        onQuestSettled={() => {
          const scene = gameRef.current?.scene.getScene("OverworldScene");
          void (scene as OverworldScene | undefined)?.refreshQuestOffers();
        }}
      />
    </div>
  );
}
