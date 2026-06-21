/**
 * Overworld.tsx — React wrapper that mounts the Phaser overworld canvas.
 *
 * Exported as the default export so App.tsx (Wave 2 wiring) can do:
 *   import Overworld from "@/ui/Overworld"
 *
 * The component:
 * - Measures the available area (its <main> parent) and sizes the canvas to it,
 *   so the world fills the screen and the camera scrolls instead of letterboxing.
 *   A percentage `h-full` does NOT resolve against a flex-grown parent, so we
 *   measure with a ResizeObserver and set an explicit pixel height instead.
 * - Creates a Phaser.Game once a real size is known; resizes it on layout change.
 * - On encounter collision: calls POST /api/encounters (WS-B), then setEncounter.
 */

import Phaser from "phaser";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { buildEncounterBridge } from "../game/EncounterTrigger";
import { OverworldScene } from "../game/OverworldScene";
import OverworldHud, { type HudMap } from "./OverworldHud";
import { useGame } from "../state/store";

export default function Overworld() {
  const { runId, setEncounter } = useGame();
  const rootRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const gameRef = useRef<Phaser.Game | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [hudMap, setHudMap] = useState<HudMap | null>(null);
  const [playerPos, setPlayerPos] = useState<{ x: number; y: number } | null>(null);

  // Size the canvas to the viewport. Using window dimensions directly is more
  // robust than observing parentElement, which breaks if the component moves in
  // the DOM and doesn't work when ancestor elements lack explicit heights.
  useLayoutEffect(() => {
    const measure = () =>
      setSize({ w: window.innerWidth, h: window.innerHeight });
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  // Create the Phaser game once we have a real size and an active run.
  useEffect(() => {
    if (!containerRef.current || !runId || size.w === 0 || size.h === 0) return;

    const bridge = buildEncounterBridge(runId, setEncounter);

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

    // OverworldScene fetches its own map in create(); we just start it once the
    // game has booted. The cancelled guard keeps unmount/HMR from emitting into
    // a torn-down game.
    const startScene = () => {
      if (cancelled || !gameRef.current) return;
      gameRef.current.scene.start("OverworldScene", {
        runId,
        onEncounter: (wildId: string) => {
          void bridge.onCollision(wildId);
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
    // Intentionally NOT keyed on size — we resize in place (below) rather than
    // tearing down the game on every layout tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, setEncounter, size.w === 0 || size.h === 0]);

  // Resize the live game when the available area changes.
  useEffect(() => {
    if (gameRef.current && size.w > 0 && size.h > 0) {
      gameRef.current.scale.resize(size.w, size.h);
    }
  }, [size]);

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
    </div>
  );
}
