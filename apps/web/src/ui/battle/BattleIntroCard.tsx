import { useEffect, useRef, useState } from "react";
import type { JSX } from "react";

type Phase = "theme" | "choose" | "countdown" | "go" | "done";
type Stance = "for" | "against";

/**
 * Dramatic pre-fight intro card for the Debate RPG battle screen.
 *
 * Phase timeline:
 *   theme      → auto-advances to "choose" after 5s
 *   choose     → waits for the user to pick FOR / AGAINST
 *   countdown  → 5s, big gold numeral 5→4→3→2→1 (one tick / second)
 *   go         → "READY!" for 0.5s, then "DEBATE!!" for 0.9s (1.4s total)
 *   done       → fires onComplete(stance) exactly once, renders null
 */
export function BattleIntroCard({
  topic,
  onComplete,
}: {
  topic: string | null;
  onComplete: (stance: "for" | "against") => void;
}): JSX.Element | null {
  const [phase, setPhase] = useState<Phase>("theme");
  const [stance, setStance] = useState<Stance | null>(null);
  const [count, setCount] = useState(5);
  const [goStep, setGoStep] = useState<0 | 1>(0); // 0 = READY!, 1 = DEBATE!!

  const numeralRef = useRef<HTMLDivElement>(null);
  const firedRef = useRef(false);

  // ── theme → choose (auto-advance) ───────────────────────────────────────
  useEffect(() => {
    if (phase !== "theme") return;
    const id = setTimeout(() => setPhase("choose"), 5000);
    return () => clearTimeout(id);
  }, [phase]);

  // ── countdown 5→1, then go ──────────────────────────────────────────────
  useEffect(() => {
    if (phase !== "countdown") return;
    setCount(5);
    const id = setInterval(() => {
      setCount((c) => {
        if (c <= 1) {
          setPhase("go");
          return c;
        }
        return c - 1;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [phase]);

  // Replay the pop animation on each countdown tick.
  useEffect(() => {
    if (phase !== "countdown") return;
    const el = numeralRef.current;
    if (!el) return;
    el.classList.remove("score-punch");
    void el.offsetWidth; // force reflow so the animation restarts
    el.classList.add("score-punch");
  }, [count, phase]);

  // ── go: READY! (0.5s) → DEBATE!! (0.9s) → done ──────────────────────────
  useEffect(() => {
    if (phase !== "go") return;
    setGoStep(0);
    const toDebate = setTimeout(() => setGoStep(1), 500);
    const toDone = setTimeout(() => setPhase("done"), 1400);
    return () => {
      clearTimeout(toDebate);
      clearTimeout(toDone);
    };
  }, [phase]);

  // ── done: fire onComplete exactly once ──────────────────────────────────
  useEffect(() => {
    if (phase !== "done" || firedRef.current) return;
    firedRef.current = true;
    onComplete(stance ?? "for");
  }, [phase, stance, onComplete]);

  if (phase === "done") return null;

  const choose = (s: Stance) => {
    setStance(s);
    setPhase("countdown");
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        display: "grid",
        placeItems: "center",
        background: "rgba(8,9,14,0.92)",
        padding: 16,
      }}
    >
      <div
        className="pixel-panel intro-card"
        style={{
          background: "var(--panel2)",
          padding: "40px 36px",
          maxWidth: 720,
          width: "100%",
          textAlign: "center",
          display: "grid",
          placeItems: "center",
          gap: 24,
          minHeight: 280,
        }}
      >
        {phase === "theme" && (
          <>
            <div
              className="font-hud"
              style={{ color: "var(--accent)", fontSize: 12 }}
            >
              Theme
            </div>
            <div
              className="font-display"
              style={{
                color: "var(--ink)",
                fontSize: 22,
                lineHeight: 1.5,
                maxWidth: 640,
                wordBreak: "break-word",
              }}
            >
              {(topic ?? "DEBATE").toUpperCase()}
            </div>
          </>
        )}

        {phase === "choose" && (
          <>
            <div
              className="font-hud"
              style={{ color: "var(--muted)", fontSize: 13 }}
            >
              Choose your position
            </div>
            <div
              style={{
                display: "flex",
                gap: 20,
                flexWrap: "wrap",
                justifyContent: "center",
                width: "100%",
              }}
            >
              <button
                type="button"
                onClick={() => choose("for")}
                className="font-display"
                style={{
                  flex: "1 1 200px",
                  minWidth: 180,
                  padding: "26px 18px",
                  fontSize: 20,
                  cursor: "pointer",
                  color: "var(--party)",
                  background: "transparent",
                  border: "3px solid var(--party)",
                  boxShadow: "4px 4px 0 #000",
                  transition: "background 0.1s ease, color 0.1s ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--party)";
                  e.currentTarget.style.color = "#001722";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "var(--party)";
                }}
              >
                FOR
              </button>
              <button
                type="button"
                onClick={() => choose("against")}
                className="font-display"
                style={{
                  flex: "1 1 200px",
                  minWidth: 180,
                  padding: "26px 18px",
                  fontSize: 20,
                  cursor: "pointer",
                  color: "var(--enemy)",
                  background: "transparent",
                  border: "3px solid var(--enemy)",
                  boxShadow: "4px 4px 0 #000",
                  transition: "background 0.1s ease, color 0.1s ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--enemy)";
                  e.currentTarget.style.color = "#2a0006";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "var(--enemy)";
                }}
              >
                AGAINST
              </button>
            </div>
          </>
        )}

        {phase === "countdown" && (
          <>
            <div
              className="font-hud"
              style={{ color: "var(--muted)", fontSize: 13 }}
            >
              Your position:{" "}
              <span
                style={{
                  color: stance === "for" ? "var(--party)" : "var(--enemy)",
                }}
              >
                {stance === "for" ? "FOR" : "AGAINST"}
              </span>
            </div>
            <div
              ref={numeralRef}
              className="font-display score-punch"
              style={{
                color: "var(--accent)",
                fontSize: 96,
                lineHeight: 1,
                textShadow: "4px 4px 0 #000",
              }}
            >
              {count}
            </div>
          </>
        )}

        {phase === "go" && (
          <div
            key={goStep}
            className="font-display intro-go"
            style={{
              color: "var(--accent)",
              fontSize: goStep === 0 ? 40 : 64,
              lineHeight: 1,
              textShadow: "4px 4px 0 #000",
            }}
          >
            {goStep === 0 ? "READY!" : "DEBATE!!"}
          </div>
        )}
      </div>
    </div>
  );
}

export default BattleIntroCard;
