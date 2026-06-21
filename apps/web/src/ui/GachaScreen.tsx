/**
 * GachaScreen — Wave A persona-pull cinematic.
 *
 * Flow:
 *   1. Slot-machine spin animation (~1.5s).
 *   2. POST /api/runs/{id}/gacha/pull -> the rolled persona's name, domain,
 *      type, and seed tagline.
 *   3. "Hydrating from Wikipedia..." spinner. Polls GET /api/monsters/{id}
 *      every 500ms (max 10s) until wiki_hydrated=true, then enables the
 *      "Ready to battle" CTA.
 *
 * Visual baseline mirrors PartyScreen.tsx: pixel-panel, font-display, the
 * --accent CSS variable for the highlight color. Lives behind App.tsx's
 * needs-gacha gate (runs whose party is empty).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useGame } from "../state/store";
import { api } from "../api/client";

// ---- Types (mirror MonsterSummary / GachaPullResult in app/schemas.py) ----

interface MonsterSummary {
  id: string;
  name: string;
  type: string;
  owner: string;
  level: number;
  xp: number;
  max_hp: number;
  evolution_stage: number;
  skills: unknown[];
  atk?: number;
  def?: number;
  mp?: number;
  max_mp?: number;
  domain?: string;
  wiki_hydrated?: boolean;
}

interface GachaPullResult {
  monster: MonsterSummary;
  persona_key: string;
  persona_tier: string;
}

type Stage = "idle" | "spinning" | "revealed" | "hydrating" | "ready" | "error";

const SPIN_MS = 1500;
const POLL_INTERVAL_MS = 500;
const POLL_TIMEOUT_MS = 10_000;

// Marquee of placeholder names the slot machine rolls through during the spin.
const _MARQUEE = [
  "Socrates",
  "Linus Torvalds",
  "Marie Curie",
  "Steve Jobs",
  "Nietzsche",
  "Ada Lovelace",
  "Shakespeare",
  "Gandhi",
  "Einstein",
  "MLK",
];

function _tierStyle(tier: string): { color: string; label: string } {
  switch (tier) {
    case "legendary":
      return { color: "#f6c64a", label: "LEGENDARY" };
    case "rare":
      return { color: "#7ad7ff", label: "RARE" };
    default:
      return { color: "var(--muted)", label: "COMMON" };
  }
}

interface GachaScreenProps {
  /** Optional summon item to consume on the pull (post-battle drop UX). */
  summonItemId?: string | null;
  /** Fires after the player presses "Ready to battle". Lets the parent
   *  hand off to the overworld / encounter screen. */
  onReady?: (monster: MonsterSummary) => void;
}

export default function GachaScreen({ summonItemId, onReady }: GachaScreenProps = {}) {
  const { runId, setScreen } = useGame();
  const [stage, setStage] = useState<Stage>("idle");
  const [error, setError] = useState<string | null>(null);
  const [pull, setPull] = useState<GachaPullResult | null>(null);
  const [monster, setMonster] = useState<MonsterSummary | null>(null);
  const [marqueeIdx, setMarqueeIdx] = useState(0);
  const pollTimerRef = useRef<number | null>(null);
  const pollStartRef = useRef<number>(0);

  const clearPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // Slot-machine marquee while spinning.
  useEffect(() => {
    if (stage !== "spinning") return;
    const id = window.setInterval(() => {
      setMarqueeIdx((i) => (i + 1) % _MARQUEE.length);
    }, 90);
    return () => window.clearInterval(id);
  }, [stage]);

  // Cleanup any in-flight poll when we unmount.
  useEffect(() => () => clearPolling(), [clearPolling]);

  const startPull = useCallback(async () => {
    if (!runId) return;
    setError(null);
    setStage("spinning");
    setPull(null);
    setMonster(null);

    // Kick the spin animation + the request in parallel; reveal after the
    // animation finishes so the cinematic always lands the same way.
    const spinPromise = new Promise<void>((resolve) =>
      window.setTimeout(resolve, SPIN_MS),
    );
    const pullPromise = api.post<GachaPullResult>(
      `/api/runs/${runId}/gacha/pull`,
      summonItemId ? { summon_item_id: summonItemId } : {},
    );

    try {
      const [, result] = await Promise.all([spinPromise, pullPromise]);
      setPull(result);
      setMonster(result.monster);
      // If the server already finished hydration in the spin window (rare —
      // hydration is background; usually we land on "hydrating"), short-circuit.
      if (result.monster.wiki_hydrated) {
        setStage("ready");
      } else {
        setStage("hydrating");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Pull failed");
      setStage("error");
    }
  }, [runId, summonItemId]);

  // Hydration poll. Bounded by POLL_TIMEOUT_MS — after 10s we surface "Ready"
  // anyway so the player is never blocked (the seed tagline is still useful).
  useEffect(() => {
    if (stage !== "hydrating" || !pull) return;
    pollStartRef.current = Date.now();

    const tick = async () => {
      if (!pull) return;
      const elapsed = Date.now() - pollStartRef.current;
      try {
        const m = await api.get<MonsterSummary>(`/api/monsters/${pull.monster.id}`);
        setMonster(m);
        if (m.wiki_hydrated) {
          setStage("ready");
          return;
        }
      } catch {
        // Soft-fail — keep polling until the timeout.
      }
      if (elapsed >= POLL_TIMEOUT_MS) {
        setStage("ready"); // Never block the player; tagline is still set.
        return;
      }
      pollTimerRef.current = window.setTimeout(tick, POLL_INTERVAL_MS);
    };

    pollTimerRef.current = window.setTimeout(tick, POLL_INTERVAL_MS);
    return clearPolling;
  }, [stage, pull, clearPolling]);

  const handleReady = () => {
    if (!monster) return;
    if (onReady) onReady(monster);
    else setScreen("overworld");
  };

  if (!runId) {
    return (
      <div className="text-center py-8 font-body" style={{ color: "var(--muted)" }}>
        No active run. Start a run first.
      </div>
    );
  }

  return (
    <div className="w-full max-w-xl mx-auto px-4 py-8 space-y-4">
      <div className="pixel-panel p-6 space-y-5 text-center">
        <div className="font-display text-base" style={{ color: "var(--accent)" }}>
          GACHA SUMMON
        </div>

        {stage === "idle" && (
          <>
            <p className="font-body text-sm" style={{ color: "var(--muted)" }}>
              {summonItemId
                ? "A summon item glows in your inventory. Consume it to pull a stronger persona."
                : "Pull your first persona to start your party. Common 70 / Rare 25 / Legendary 5."}
            </p>
            <button
              className="pixel-btn pixel-btn--accent w-full"
              onClick={startPull}
              autoFocus
            >
              Pull
            </button>
          </>
        )}

        {stage === "spinning" && (
          <div className="space-y-4">
            <div
              className="font-display text-lg py-6 px-3"
              style={{
                border: "2px solid var(--accent)",
                background: "rgba(232,230,216,0.05)",
                letterSpacing: "0.1em",
              }}
              data-testid="gacha-spinner"
            >
              {_MARQUEE[marqueeIdx]}
            </div>
            <div className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
              rolling…
            </div>
          </div>
        )}

        {(stage === "hydrating" || stage === "ready") && pull && monster && (
          <div className="space-y-3">
            <div
              className="font-display text-xl py-5"
              style={{
                border: "2px solid var(--accent)",
                background: "rgba(232,230,216,0.05)",
              }}
            >
              {monster.name}
            </div>
            <div className="flex justify-center gap-2 flex-wrap">
              <span
                className="font-hud text-[10px] px-2 py-0.5"
                style={{
                  background: _tierStyle(pull.persona_tier).color,
                  color: "#000",
                }}
              >
                {_tierStyle(pull.persona_tier).label}
              </span>
              <span
                className="font-hud text-[10px] px-2 py-0.5"
                style={{ border: "1px solid var(--accent)", color: "var(--accent)" }}
              >
                {monster.domain || "GENERAL"}
              </span>
              <span
                className="font-hud text-[10px] px-2 py-0.5"
                style={{ border: "1px solid var(--muted)", color: "var(--muted)" }}
              >
                {monster.type}
              </span>
            </div>
            <p
              className="font-body text-sm italic"
              style={{ color: "var(--muted)" }}
            >
              “{(monster as any).persona?.voice ||
                (monster as any).persona?.tagline ||
                "…"}”
            </p>

            {stage === "hydrating" ? (
              <div
                className="font-hud text-[10px] flex items-center justify-center gap-2"
                style={{ color: "var(--muted)" }}
                data-testid="gacha-hydrating"
              >
                <span
                  className="inline-block"
                  style={{
                    width: 10,
                    height: 10,
                    border: "2px solid var(--accent)",
                    borderTopColor: "transparent",
                    borderRadius: "50%",
                    animation: "gacha-spin 0.9s linear infinite",
                  }}
                />
                Hydrating from Wikipedia…
              </div>
            ) : (
              <button
                className="pixel-btn pixel-btn--accent w-full"
                onClick={handleReady}
                autoFocus
                data-testid="gacha-ready"
              >
                Ready to battle
              </button>
            )}
          </div>
        )}

        {stage === "error" && (
          <div className="space-y-3">
            <div
              className="pixel-panel p-3 font-body text-sm"
              style={{ borderColor: "var(--danger)", color: "var(--danger)" }}
            >
              {error || "Pull failed."}
            </div>
            <button className="pixel-btn w-full" onClick={startPull}>
              Try again
            </button>
          </div>
        )}
      </div>

      {/* Inline keyframes for the spinner — keeps the component self-contained. */}
      <style>{`
        @keyframes gacha-spin {
          0%   { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
