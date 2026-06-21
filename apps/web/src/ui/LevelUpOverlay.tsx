/**
 * LevelUpOverlay — 3s cinematic that fires when a party monster levels up.
 *
 * Listens for the global `encounter:level-up` CustomEvent dispatched by
 * `useEncounterStream` whenever the WS receives a `{type: "LevelUp", ...}`
 * message from the encounter finalize. Self-contained: mount once near the
 * top of the battle scene and forget about it — no props required.
 *
 * Visual contract:
 *   - Pinned full-screen overlay with a centered headline.
 *   - "{monster.name} LEVEL {new_level}" if the monster name is known via the
 *     game store; otherwise just "LEVEL UP   →   LEVEL {new_level}".
 *   - For each of stat_gains.atk / def / mp / hp the overlay flies a "+N STAT"
 *     chip up from below with ~200ms stagger.
 *   - Auto-dismisses after 3 seconds. Subsequent events queue (one-at-a-time
 *     playback) so a multi-monster finalize never drops a cinematic.
 *
 * The overlay never blocks input (pointer-events: none); a missing stat key
 * (zero gain) renders no chip rather than "+0".
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  LEVEL_UP_EVENT,
  type LevelUpEvent,
} from "../ws/useEncounterStream";

/**
 * Minimal combatant shape used to resolve a monster's display name. Matches
 * the public fields of `CombatantState` so the parent can pass its existing
 * roster straight through without re-shaping.
 */
export interface LevelUpCombatant {
  monster_id: string;
  name: string;
}

export interface LevelUpOverlayProps {
  /**
   * Optional combatant roster used to render the monster's name in the
   * headline. When omitted (or the monster_id is not found) the overlay
   * gracefully falls back to "MONSTER LEVEL N".
   */
  combatants?: ReadonlyArray<LevelUpCombatant>;
}

const OVERLAY_DURATION_MS = 3000;
const CHIP_STAGGER_MS = 200;

type StatKey = "atk" | "def" | "mp" | "hp";

interface StatChip {
  key: StatKey;
  label: string;
  amount: number;
  color: string;
}

/** Map a LevelUpEvent's stat_gains into the ordered chip list (zeroes dropped). */
function chipsFor(ev: LevelUpEvent): StatChip[] {
  // Order matches the design doc: ATK -> DEF -> MP -> HP. Each chip animates
  // ~CHIP_STAGGER_MS after its predecessor so the eye reads them in sequence.
  const defs: { key: StatKey; label: string; color: string }[] = [
    { key: "atk", label: "ATK", color: "var(--danger, #ff5252)" },
    { key: "def", label: "DEF", color: "var(--accent, #6ab7ff)" },
    { key: "mp", label: "MP", color: "var(--warn, #ffd166)" },
    { key: "hp", label: "HP", color: "var(--win, #6bd968)" },
  ];
  return defs
    .map((d) => ({ ...d, amount: ev.stat_gains?.[d.key] ?? 0 }))
    .filter((c) => c.amount > 0);
}

export default function LevelUpOverlay({
  combatants,
}: LevelUpOverlayProps = {}) {
  // Queue of pending level-up events: a multi-monster finalize emits one
  // event per monster, and we play them one cinematic at a time.
  const [queue, setQueue] = useState<LevelUpEvent[]>([]);
  // The event currently animating (null while idle). Splitting current out
  // of the queue lets a new event arrive mid-cinematic without restarting.
  const [current, setCurrent] = useState<LevelUpEvent | null>(null);
  // Ref the dismiss timer so we never double-fire on rapid arrivals.
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Resolve the monster's display name from the optional combatant roster
  // so the headline reads "Socrates LEVEL 3" rather than a raw id. Memoize
  // off the resolved-name lookup to avoid re-scanning every render.
  const nameById = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of combatants ?? []) m.set(c.monster_id, c.name);
    return m;
  }, [combatants]);
  const combatantName = current ? nameById.get(current.monster_id) ?? null : null;

  // Subscribe to the global level-up event ONCE. The hook re-dispatches the
  // server's `{type: "LevelUp"}` WS message as a CustomEvent so this overlay
  // can mount anywhere without prop-drilling.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<LevelUpEvent>;
      if (!ce?.detail) return;
      setQueue((prev) => [...prev, ce.detail]);
    };
    window.addEventListener(LEVEL_UP_EVENT, handler as EventListener);
    return () =>
      window.removeEventListener(LEVEL_UP_EVENT, handler as EventListener);
  }, []);

  // Pump the queue: when idle and an event is queued, start the cinematic.
  useEffect(() => {
    if (current || queue.length === 0) return;
    const [next, ...rest] = queue;
    setCurrent(next);
    setQueue(rest);
    dismissTimer.current = setTimeout(() => {
      setCurrent(null);
      dismissTimer.current = null;
    }, OVERLAY_DURATION_MS);
    return () => {
      if (dismissTimer.current) {
        clearTimeout(dismissTimer.current);
        dismissTimer.current = null;
      }
    };
  }, [queue, current]);

  if (!current) return null;

  const chips = chipsFor(current);
  const headlineName = combatantName ?? "MONSTER";

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "fixed",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "1rem",
        // Sit above HP bars / battle UI but below modals.
        zIndex: 80,
        pointerEvents: "none",
        // Subtle vignette so the overlay reads in any lighting.
        background:
          "radial-gradient(ellipse at center, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0) 70%)",
        animation: "levelup-fade 3000ms ease-out forwards",
      }}
    >
      {/* Inline keyframes — keeps this overlay portable without touching
          the global stylesheet. Defined once per mount. */}
      <style>{`
        @keyframes levelup-fade {
          0% { opacity: 0; }
          10% { opacity: 1; }
          85% { opacity: 1; }
          100% { opacity: 0; }
        }
        @keyframes levelup-headline-in {
          0% { transform: scale(0.5) translateY(20px); opacity: 0; }
          40% { transform: scale(1.15) translateY(0); opacity: 1; }
          70% { transform: scale(1) translateY(0); opacity: 1; }
          100% { transform: scale(1) translateY(0); opacity: 1; }
        }
        @keyframes levelup-chip-fly {
          0% { transform: translateY(40px); opacity: 0; }
          25% { transform: translateY(-4px); opacity: 1; }
          80% { transform: translateY(-12px); opacity: 1; }
          100% { transform: translateY(-32px); opacity: 0; }
        }
      `}</style>

      <div
        className="font-hud"
        style={{
          fontSize: "2.25rem",
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          textShadow: "0 4px 12px rgba(0,0,0,0.8), 0 0 18px rgba(255,210,90,0.45)",
          color: "var(--accent, #ffd24a)",
          animation: "levelup-headline-in 700ms cubic-bezier(0.2, 0.9, 0.3, 1.4) forwards",
          textAlign: "center",
        }}
      >
        {headlineName} <span style={{ opacity: 0.85 }}>LEVEL</span>{" "}
        <span style={{ color: "var(--win, #6bd968)" }}>{current.new_level}</span>
      </div>

      <div
        style={{
          display: "flex",
          gap: "0.75rem",
          flexWrap: "wrap",
          justifyContent: "center",
          maxWidth: "min(90vw, 32rem)",
        }}
      >
        {chips.map((chip, i) => (
          <div
            key={chip.key}
            className="font-hud"
            style={{
              padding: "0.4rem 0.9rem",
              border: `2px solid ${chip.color}`,
              background: "rgba(0,0,0,0.6)",
              color: chip.color,
              fontSize: "1.1rem",
              letterSpacing: "0.05em",
              borderRadius: "2px",
              textShadow: `0 0 6px ${chip.color}`,
              // Stagger each chip ~200ms after the previous one.
              animation: `levelup-chip-fly 1800ms ease-out ${
                400 + i * CHIP_STAGGER_MS
              }ms forwards`,
              opacity: 0,
            }}
          >
            +{chip.amount} {chip.label}
          </div>
        ))}
      </div>
    </div>
  );
}
