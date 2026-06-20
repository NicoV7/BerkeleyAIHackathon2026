/**
 * Overworld.tsx — React wrapper that mounts the Phaser overworld canvas.
 *
 * Exported as the default export so App.tsx (Wave 2 wiring) can do:
 *   import Overworld from "@/ui/Overworld"
 *
 * The component:
 * - Creates a Phaser.Game instance on mount, destroys on unmount.
 * - Passes runId + encounter callback into OverworldScene via scene `init` data.
 * - On encounter collision: calls POST /api/encounters (WS-B), then setEncounter.
 */

import Phaser from "phaser";
import { useEffect, useRef } from "react";
import { buildEncounterBridge } from "../game/EncounterTrigger";
import { OverworldScene, TILE_SIZE } from "../game/OverworldScene";
import { useGame } from "../state/store";

const MAP_WIDTH = 20;
const MAP_HEIGHT = 15;
const CANVAS_W = Math.min(MAP_WIDTH * TILE_SIZE, window.innerWidth - 16);
const CANVAS_H = Math.min(MAP_HEIGHT * TILE_SIZE, window.innerHeight - 120);

export default function Overworld() {
  const { runId, setEncounter } = useGame();
  const containerRef = useRef<HTMLDivElement>(null);
  const gameRef = useRef<Phaser.Game | null>(null);

  useEffect(() => {
    if (!containerRef.current || !runId) return;

    const bridge = buildEncounterBridge(runId, setEncounter);

    const config: Phaser.Types.Core.GameConfig = {
      type: Phaser.AUTO,
      width: CANVAS_W,
      height: CANVAS_H,
      backgroundColor: "#0e1018",
      parent: containerRef.current,
      scene: [OverworldScene],
      render: { pixelArt: true, antialias: false, roundPixels: true },
      scale: {
        mode: Phaser.Scale.FIT,
        autoCenter: Phaser.Scale.CENTER_BOTH,
      },
      audio: { noAudio: true },
    };

    const game = new Phaser.Game(config);
    gameRef.current = game;

    // Start scene with run config after Phaser is ready
    game.events.once("ready", () => {
      game.scene.start("OverworldScene", {
        runId,
        onEncounter: (wildId: string) => {
          void bridge.onCollision(wildId);
        },
      });
    });

    return () => {
      game.destroy(true);
      gameRef.current = null;
    };
  }, [runId, setEncounter]);

  if (!runId) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="font-body text-sm" style={{ color: "var(--muted)" }}>
          No active run — start a run first.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center gap-2 p-2">
      <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
        Arrow keys / WASD to move · Walk into a red enemy to battle
      </div>
      <div
        ref={containerRef}
        style={{ width: CANVAS_W, height: CANVAS_H, borderColor: "rgba(232,230,216,0.18)" }}
        className="overflow-hidden border-[3px]"
      />
    </div>
  );
}
