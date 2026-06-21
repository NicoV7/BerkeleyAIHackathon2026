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

export default function Overworld() {
  const { runId, setEncounter, playerName } = useGame();
  const { transition } = useIrisTransition();
  const rootRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const gameRef = useRef<Phaser.Game | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [hudMap, setHudMap] = useState<HudMap | null>(null);
  const [playerPos, setPlayerPos] = useState<{ x: number; y: number } | null>(null);
  const [activeNpc, setActiveNpc] = useState<NPCAnchorView | null>(null);

  useLayoutEffect(() => {
    const measure = () => setSize({ w: window.innerWidth, h: window.innerHeight });
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
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
        onEncounter: (wildId: string) => {
          void bridge.onCollision(wildId);
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

  // Keep the in-scene floating name tag in sync if the player's name changes
  // mid-run (the scene is only started once, so push updates imperatively).
  // Guard on the scene being registered first: getScene() can throw while the
  // Phaser game is still booting, so we check the manager's key map directly.
  useEffect(() => {
    const manager = gameRef.current?.scene;
    if (!manager || !manager.keys["OverworldScene"]) return;
    const scene = manager.getScene("OverworldScene") as OverworldScene | undefined;
    scene?.setPlayerName?.(playerName);
  }, [playerName]);

  if (!runId) {
    return (
      <div ref={rootRef} className="flex items-center justify-center h-full">
        <p className="font-body text-sm" style={{ color: "var(--muted)" }}>
          No active run — start a run first.
        </p>
      </div>
    );
  }

  return (
    <div
      ref={rootRef}
      className="relative w-full overflow-hidden"
      style={{ height: size.h || "100vh" }}
    >
      <div ref={containerRef} className="absolute inset-0" />
      <div
        className="pointer-events-none absolute left-1/2 top-3 z-10 -translate-x-1/2 font-hud text-[10px]"
        style={{ color: "var(--muted)" }}
      >
        Arrow keys / WASD to move · Walk into a red enemy to battle
      </div>
      <OverworldHud map={hudMap} player={playerPos} runId={runId} />
      <NPCDialogue runId={runId} npc={activeNpc} onClose={() => setActiveNpc(null)} />
    </div>
  );
}
