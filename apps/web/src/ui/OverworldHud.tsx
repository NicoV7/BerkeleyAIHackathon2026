/**
 * OverworldHud — DOM overlay chrome drawn on top of the Phaser overworld canvas:
 *   - Minimap (top-right) of the tile grid with player + enemy dots.
 *   - HP (red) and XP (blue) bars beneath the minimap, for the lead party monster.
 *   - WASD key overlay (bottom-left) that lights up as the player presses keys.
 *
 * Map + player data flow in from OverworldScene via the onMapLoaded/onPlayerMove
 * callbacks (see Overworld.tsx). Party stats come from GET /api/runs/{id}/party.
 */

import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import {
  activeQuestTargets,
  clampToRectEdge,
  edgeArrowToTarget,
  isOnScreen,
  nearestTarget,
  type QuestTarget,
  type RunQuest,
} from "./questPin";

export interface HudMap {
  width: number;
  height: number;
  tiles: number[][];
  enemies: { id: string; x: number; y: number }[];
  // Global tile origin of the loaded chunk window — lets the HUD project GLOBAL
  // quest target coords into this minimap's chunk-local space.
  originX: number;
  originY: number;
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
  const quests = useActiveQuestTargets(runId);

  return (
    <>
      <div
        className="pointer-events-none absolute top-3 left-3 z-10"
        style={{ width: "min(33vw, 360px)" }}
      >
        <StatBars runId={runId} />
      </div>
      <div className="pointer-events-none absolute right-3 top-3 z-10">
        <Minimap map={map} player={player} quests={quests} />
      </div>
      <QuestEdgeArrow map={map} player={player} quests={quests} />
      <WasdOverlay />
    </>
  );
}

/* ----------------------------- Quests ----------------------------- */

/**
 * Fetch GET /api/runs/{id}/quests and reduce to active quest TARGETS (global tile
 * coords). Best-effort: a failed fetch yields no targets (the HUD just omits the
 * pin/arrow), so a quest-less or offline run renders cleanly.
 */
function useActiveQuestTargets(runId: string): QuestTarget[] {
  const [targets, setTargets] = useState<QuestTarget[]>([]);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api
        .get<{ quests: RunQuest[] }>(`/api/runs/${runId}/quests`)
        .then((res) => {
          if (!cancelled) setTargets(activeQuestTargets(res.quests ?? []));
        })
        .catch(() => {
          if (!cancelled) setTargets([]);
        });
    };
    load();
    // Light polling so a quest accepted at an NPC shows up without a reload.
    const t = window.setInterval(load, 8000);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, [runId]);

  return targets;
}

/* ----------------------------- Minimap ----------------------------- */

const MINIMAP_PX = 180;

function Minimap({
  map,
  player,
  quests,
}: {
  map: HudMap | null;
  player: { x: number; y: number } | null;
  quests: QuestTarget[];
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

    // Quest pins (amber) for any active quest whose GLOBAL target lands inside
    // this chunk window. Drawn under the player dot so the player stays visible.
    for (const q of quests) {
      const lx = q.x - map.originX;
      const ly = q.y - map.originY;
      if (lx < 0 || ly < 0 || lx >= map.width || ly >= map.height) continue;
      drawPin(ctx, lx * cell + cell / 2, ly * cell + cell / 2, Math.max(3, cell + 2));
    }

    if (player) {
      ctx.fillStyle = "#5cc8ff";
      ctx.fillRect(player.x * cell - off, player.y * cell - off, dot, dot);
    }

    setSrc(canvas.toDataURL());
  }, [map, player, quests]);

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

/** Draw a teardrop-ish quest pin (amber) centred at (cx,cy) on the minimap. */
function drawPin(ctx: CanvasRenderingContext2D, cx: number, cy: number, r: number) {
  ctx.save();
  ctx.fillStyle = "#ffcf3f";
  ctx.strokeStyle = "#1a1410";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, r / 2, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  // Inner dot so it reads as a marker, not a blob.
  ctx.fillStyle = "#1a1410";
  ctx.beginPath();
  ctx.arc(cx, cy, Math.max(1, r / 6), 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

/* --------------------------- Quest edge arrow --------------------------- */

const ARROW_BOX = 320; // px box (centred on screen) the arrow is pinned to
const ARROW_PAD = 48;

/**
 * An on-screen arrow that points toward the NEAREST active quest target when it
 * is OFF the visible map window. Hidden when there are no active quests or the
 * target is already on screen (the minimap pin covers that case). Positioned by
 * clamping the player→target direction onto a centred box's edge.
 */
function QuestEdgeArrow({
  map,
  player,
  quests,
}: {
  map: HudMap | null;
  player: { x: number; y: number } | null;
  quests: QuestTarget[];
}) {
  const arrow = useMemo(() => {
    if (!map || !player || quests.length === 0) return null;
    // Player + targets back into GLOBAL tile space.
    const pgx = player.x + map.originX;
    const pgy = player.y + map.originY;
    const target = nearestTarget(pgx, pgy, quests);
    if (!target) return null;
    // Don't show the arrow if the target is already visible on the minimap window.
    if (
      isOnScreen(
        { originX: map.originX, originY: map.originY, width: map.width, height: map.height },
        target.x,
        target.y
      )
    ) {
      return null;
    }
    const dir = edgeArrowToTarget(pgx, pgy, target.x, target.y);
    if (!dir) return null;
    const pos = clampToRectEdge(dir.dx, dir.dy, ARROW_BOX, ARROW_BOX, ARROW_PAD);
    return { angleDeg: (dir.angle * 180) / Math.PI, pos, dist: dir.distanceTiles };
  }, [map, player, quests]);

  if (!arrow) return null;

  return (
    <div
      className="pointer-events-none absolute left-1/2 top-1/2 z-20"
      style={{
        transform: `translate(-50%, -50%) translate(${arrow.pos.x}px, ${arrow.pos.y}px)`,
      }}
    >
      <div
        className="flex flex-col items-center"
        style={{ transform: `rotate(${arrow.angleDeg}deg)` }}
      >
        {/* Triangle arrow pointing along +x (rotated to the target bearing). */}
        <div
          style={{
            width: 0,
            height: 0,
            borderTop: "11px solid transparent",
            borderBottom: "11px solid transparent",
            borderLeft: "18px solid var(--accent, #ffcf3f)",
            filter: "drop-shadow(1px 1px 0 #000)",
          }}
        />
      </div>
      <div
        className="font-hud text-[8px] mt-0.5 text-center"
        style={{ color: "var(--accent, #ffcf3f)", textShadow: "1px 1px 0 #000" }}
      >
        QUEST
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
