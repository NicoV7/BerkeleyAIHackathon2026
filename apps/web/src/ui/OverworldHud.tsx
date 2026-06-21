/**
 * OverworldHud — DOM overlay chrome drawn on top of the Phaser overworld canvas:
 *   - Minimap (top-right) of the tile grid with player + enemy dots.
 *   - HP (red) and XP (blue) bars beneath the minimap, for the lead party monster.
 *   - WASD key overlay (bottom-left) that lights up as the player presses keys.
 *
 * Map + player data flow in from OverworldScene via the onMapLoaded/onPlayerMove
 * callbacks (see Overworld.tsx). Party stats come from GET /api/runs/{id}/party.
 */

import { useEffect, useState } from "react";
import { api } from "../api/client";

export interface HudMap {
  width: number;
  height: number;
  tiles: number[][];
  enemies: { id: string; x: number; y: number }[];
}

interface PartyMonster {
  id: string;
  name: string;
  level: number;
  xp: number;
  max_hp: number;
}

export default function OverworldHud({
  map,
  player,
  runId,
}: {
  map: HudMap | null;
  player: { x: number; y: number } | null;
  runId: string;
}) {
  return (
    <>
      <div
        className="pointer-events-none absolute top-3 left-3 z-10"
        style={{ width: "min(33vw, 360px)" }}
      >
        <StatBars runId={runId} />
      </div>
      <div className="pointer-events-none absolute right-3 top-3 z-10">
        <Minimap map={map} player={player} />
      </div>
      <WasdOverlay />
    </>
  );
}

/* ----------------------------- Minimap ----------------------------- */

const MINIMAP_PX = 180;

function Minimap({
  map,
  player,
}: {
  map: HudMap | null;
  player: { x: number; y: number } | null;
}) {
  // Draw to an OFFSCREEN canvas and show the result as an <img>. A live 2D
  // canvas in the DOM corrupts Phaser's WebGL canvas (GPU compositing quirk),
  // so we never mount one.
  const [src, setSrc] = useState<string>("");

  useEffect(() => {
    if (!map) return;
    const cell = Math.max(1, Math.floor(MINIMAP_PX / Math.max(map.width, map.height)));
    const canvas = document.createElement("canvas");
    canvas.width = map.width * cell;
    canvas.height = map.height * cell;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Terrain: walkable green, blocked brown.
    for (let y = 0; y < map.height; y++) {
      for (let x = 0; x < map.width; x++) {
        ctx.fillStyle = map.tiles[y][x] === 1 ? "#6b4a2b" : "#4f7a36";
        ctx.fillRect(x * cell, y * cell, cell, cell);
      }
    }

    // Enemies (rose) then player (cyan) on top.
    const dot = Math.max(2, cell + 1);
    const off = (dot - cell) / 2;
    ctx.fillStyle = "#ff5d6c";
    for (const e of map.enemies) ctx.fillRect(e.x * cell - off, e.y * cell - off, dot, dot);
    if (player) {
      ctx.fillStyle = "#5cc8ff";
      ctx.fillRect(player.x * cell - off, player.y * cell - off, dot, dot);
    }

    setSrc(canvas.toDataURL());
  }, [map, player]);

  return (
    <div className="pixel-panel p-2" style={{ width: MINIMAP_PX + 16 }}>
      <div className="font-hud text-[8px] mb-1.5 px-1" style={{ color: "var(--muted)" }}>
        MAP
      </div>
      <div
        className="block"
        style={{ width: MINIMAP_PX, height: MINIMAP_PX, background: "#2a2118" }}
      >
        {src ? (
          <img
            src={src}
            alt="minimap"
            style={{
              width: MINIMAP_PX,
              height: MINIMAP_PX,
              imageRendering: "pixelated",
              display: "block",
            }}
          />
        ) : null}
      </div>
    </div>
  );
}

/* ----------------------------- Stat bars ----------------------------- */

function StatBars({ runId }: { runId: string }) {
  const [mon, setMon] = useState<PartyMonster | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<PartyMonster[]>(`/api/runs/${runId}/party`)
      .then((party) => {
        if (!cancelled) setMon(party[0] ?? null);
      })
      .catch(() => {
        /* HUD is best-effort; leave bars empty if the fetch fails */
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // Overworld has no current-HP concept, so the lead monster shows full health.
  const xpNeeded = mon ? 100 * mon.level : 100;
  const xpPct = mon ? Math.min(100, Math.round((mon.xp / xpNeeded) * 100)) : 0;

  return (
    <div className="flex flex-col gap-2" style={{ filter: "drop-shadow(0 2px 8px rgba(0,0,0,0.8))" }}>
      <Bar
        label="HP"
        pct={100}
        color="var(--enemy)"
        text={mon ? `${mon.max_hp}/${mon.max_hp}` : ""}
      />
      <Bar
        label="XP"
        pct={xpPct}
        color="var(--party)"
        text={mon ? `${mon.xp}/${xpNeeded}` : ""}
      />
    </div>
  );
}

function Bar({
  label,
  pct,
  color,
  text,
}: {
  label: string;
  pct: number;
  color: string;
  text: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="font-hud text-[11px] w-[22px] shrink-0" style={{ color: "rgba(232,230,216,0.7)", textShadow: "1px 1px 0 #000" }}>
        {label}
      </span>
      <div
        className="pixel-inset relative h-5 flex-1 overflow-hidden"
        style={{ border: "2px solid rgba(0,0,0,0.6)" }}
      >
        <div className="h-full" style={{ width: `${pct}%`, background: color }} />
        <span
          className="font-hud text-[10px] absolute inset-0 flex items-center justify-center"
          style={{ color: "var(--ink)", textShadow: "1px 1px 0 #000" }}
        >
          {text}
        </span>
      </div>
    </div>
  );
}

/* ----------------------------- WASD overlay ----------------------------- */

const KEY_MAP: Record<string, "up" | "down" | "left" | "right"> = {
  w: "up",
  arrowup: "up",
  s: "down",
  arrowdown: "down",
  a: "left",
  arrowleft: "left",
  d: "right",
  arrowright: "right",
};

function WasdOverlay() {
  const [held, setHeld] = useState<Record<string, boolean>>({});

  useEffect(() => {
    const set = (dir: string, on: boolean) =>
      setHeld((h) => (h[dir] === on ? h : { ...h, [dir]: on }));
    const down = (e: KeyboardEvent) => {
      const d = KEY_MAP[e.key.toLowerCase()];
      if (d) set(d, true);
    };
    const up = (e: KeyboardEvent) => {
      const d = KEY_MAP[e.key.toLowerCase()];
      if (d) set(d, false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, []);

  return (
    <div className="pointer-events-none fixed bottom-4 left-4 z-20 flex flex-col items-center gap-1.5">
      <Key label="W" active={!!held.up} />
      <div className="flex gap-1.5">
        <Key label="A" active={!!held.left} />
        <Key label="S" active={!!held.down} />
        <Key label="D" active={!!held.right} />
      </div>
    </div>
  );
}

function Key({ label, active }: { label: string; active: boolean }) {
  return (
    <div
      className="font-hud flex h-8 w-8 items-center justify-center text-[11px]"
      style={{
        border: "2px solid rgba(232,230,216,0.3)",
        background: active ? "var(--accent)" : "rgba(26,29,46,0.85)",
        color: active ? "#0e1018" : "var(--ink)",
        boxShadow: active ? "0 0 0 2px var(--accent)" : "2px 2px 0 #000",
        transition: "background 60ms, color 60ms",
      }}
    >
      {label}
    </div>
  );
}
