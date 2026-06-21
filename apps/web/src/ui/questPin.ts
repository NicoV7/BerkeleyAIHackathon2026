/**
 * questPin — pure helpers for the minimap quest pin + on-screen edge arrow
 * (WS-7 render side). Kept free of React/DOM so the direction math is unit-
 * testable in isolation.
 *
 * The /quests endpoint returns each quest's `target` as a POSITIONAL poi key
 * (`kind:x:y`, e.g. "den:13:9"); there is no separate `target_xy`. We parse the
 * coordinates out of the key (and accept an explicit `target_xy` if a future
 * backend adds one). Coordinates are GLOBAL world tiles.
 */

export interface RunQuest {
  quest_id?: string;
  objective?: string;
  /** Positional poi key `kind:x:y`, or any string whose last two `:`-parts are coords. */
  target?: string;
  /** Optional explicit coordinates (preferred if present). */
  target_xy?: { x: number; y: number } | null;
  status?: string;
}

export interface QuestTarget {
  quest_id: string;
  x: number; // global tile x
  y: number; // global tile y
}

/** A quest is "active" when accepted and not yet completed. */
export function isActiveQuest(q: RunQuest): boolean {
  return q.status !== "completed";
}

/**
 * Parse global target tile coords for a quest. Prefers an explicit `target_xy`,
 * else extracts the trailing `:x:y` from the `kind:x:y` poi key. Returns null if
 * neither yields finite integer coordinates.
 */
export function questTargetXY(q: RunQuest): { x: number; y: number } | null {
  if (
    q.target_xy &&
    Number.isFinite(q.target_xy.x) &&
    Number.isFinite(q.target_xy.y)
  ) {
    return { x: Math.trunc(q.target_xy.x), y: Math.trunc(q.target_xy.y) };
  }
  if (typeof q.target === "string") {
    const parts = q.target.split(":");
    if (parts.length >= 2) {
      const x = Number(parts[parts.length - 2]);
      const y = Number(parts[parts.length - 1]);
      if (Number.isInteger(x) && Number.isInteger(y)) return { x, y };
    }
  }
  return null;
}

/** Resolve the active quests with parseable global target coords. */
export function activeQuestTargets(quests: RunQuest[]): QuestTarget[] {
  const out: QuestTarget[] = [];
  for (const q of quests) {
    if (!isActiveQuest(q)) continue;
    const xy = questTargetXY(q);
    if (xy) out.push({ quest_id: q.quest_id ?? `${xy.x}:${xy.y}`, x: xy.x, y: xy.y });
  }
  return out;
}

export interface ViewportRect {
  /** Top-left GLOBAL tile of the visible area. */
  originX: number;
  originY: number;
  /** Visible size in tiles. */
  width: number;
  height: number;
}

/** True if a GLOBAL tile lies inside the visible viewport rect (inclusive). */
export function isOnScreen(rect: ViewportRect, gx: number, gy: number): boolean {
  return (
    gx >= rect.originX &&
    gy >= rect.originY &&
    gx < rect.originX + rect.width &&
    gy < rect.originY + rect.height
  );
}

export interface EdgeArrow {
  /** Unit direction from the player toward the target (screen space: +y = down). */
  dx: number;
  dy: number;
  /** Angle in radians (atan2(dy,dx)); 0 = pointing right, +PI/2 = down. */
  angle: number;
  /** Manhattan-ish distance in tiles to the target (for optional labelling). */
  distanceTiles: number;
}

/**
 * Direction from the player to a target in GLOBAL tile space, as a normalized
 * vector + angle, for drawing an on-screen arrow that points toward an off-screen
 * quest. Returns null when the target coincides with the player (no direction).
 */
export function edgeArrowToTarget(
  playerGX: number,
  playerGY: number,
  targetGX: number,
  targetGY: number
): EdgeArrow | null {
  const vx = targetGX - playerGX;
  const vy = targetGY - playerGY;
  const len = Math.hypot(vx, vy);
  if (len < 1e-6) return null;
  return {
    dx: vx / len,
    dy: vy / len,
    angle: Math.atan2(vy, vx),
    distanceTiles: Math.round(len),
  };
}

/**
 * Pick the NEAREST active quest target to the player (by squared tile distance),
 * or null if there are none. Used to drive the single on-screen edge arrow.
 */
export function nearestTarget(
  playerGX: number,
  playerGY: number,
  targets: QuestTarget[]
): QuestTarget | null {
  let best: QuestTarget | null = null;
  let bestD = Infinity;
  for (const t of targets) {
    const d = (t.x - playerGX) ** 2 + (t.y - playerGY) ** 2;
    if (d < bestD) {
      bestD = d;
      best = t;
    }
  }
  return best;
}

/**
 * Clamp a unit direction onto a centred rectangle's border, returning the offset
 * (in px, relative to centre) where an edge arrow should sit. Keeps the arrow
 * pinned to the edge of a `width`x`height` box (with `pad` inset) pointing the
 * way of (dx,dy). Pure — used to position the arrow over the viewport.
 */
export function clampToRectEdge(
  dx: number,
  dy: number,
  width: number,
  height: number,
  pad: number
): { x: number; y: number } {
  const halfW = width / 2 - pad;
  const halfH = height / 2 - pad;
  if (dx === 0 && dy === 0) return { x: 0, y: 0 };
  // Scale the direction so it just touches the nearest rect edge.
  const scaleX = dx !== 0 ? halfW / Math.abs(dx) : Infinity;
  const scaleY = dy !== 0 ? halfH / Math.abs(dy) : Infinity;
  const scale = Math.min(scaleX, scaleY);
  return { x: dx * scale, y: dy * scale };
}
