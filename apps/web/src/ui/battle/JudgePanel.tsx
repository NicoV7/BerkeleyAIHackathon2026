import { forwardRef, useEffect, useRef, useState } from "react";
import type { JudgeVerdict } from "../../ws/useEncounterStream";
import { ReasoningTrend, type TrendSeries } from "../ReasoningTrend";

interface EstimateEntry {
  turn: number;
  actor_id: string;
  score: number;
}

export const JudgePanel = forwardRef<
  HTMLDivElement,
  {
    lastVerdict: JudgeVerdict | null;
    pendingEstimates: EstimateEntry[];
    youSeries: TrendSeries;
    recentVerdicts: JudgeVerdict[];
  }
>(function JudgePanel({ lastVerdict, pendingEstimates, youSeries, recentVerdicts }, ref) {
  const scoreEl = useRef<HTMLSpanElement>(null);
  const prevTurn = useRef(-1);

  // Re-trigger score-punch animation on each new verdict.
  useEffect(() => {
    if (!lastVerdict || lastVerdict.turn === prevTurn.current) return;
    prevTurn.current = lastVerdict.turn;
    const el = scoreEl.current;
    if (!el) return;
    el.classList.remove("score-punch");
    void el.offsetWidth; // force reflow
    el.classList.add("score-punch");
  }, [lastVerdict]);

  const [showRationale, setShowRationale] = useState(false);
  useEffect(() => {
    if (!lastVerdict) return;
    setShowRationale(false);
    const id = setTimeout(() => setShowRationale(true), 200);
    return () => clearTimeout(id);
  }, [lastVerdict?.turn]);

  const pendingScore = pendingEstimates[0];

  // Last ~5 verdicts, most recent first.
  const history = recentVerdicts.slice(-5).reverse();

  return (
    <div
      ref={ref}
      className="pixel-panel flex flex-col gap-2 h-full overflow-hidden"
      style={{
        borderColor: "var(--accent)",
        background: "rgba(14,16,24,0.96)",
        padding: "10px 12px",
      }}
    >
      {/* Label */}
      <div className="font-hud text-[9px] uppercase tracking-wider shrink-0" style={{ color: "var(--accent)" }}>
        Judge ★
      </div>

      {/* Big score number */}
      <div className="flex items-baseline gap-2 shrink-0">
        <span
          ref={scoreEl}
          className="font-display score-punch"
          style={{ fontSize: 60, lineHeight: 1, color: "var(--accent)" }}
        >
          {pendingScore
            ? `~${Math.round(pendingScore.score)}`
            : lastVerdict
              ? Math.round(lastVerdict.score)
              : "—"}
        </span>
        {pendingScore && (
          <span className="font-hud text-[10px] caret-blink" style={{ color: "var(--muted)" }}>
            est…
          </span>
        )}
        {lastVerdict && !pendingScore && (
          <span className="font-hud text-[10px]" style={{ color: "var(--muted)" }}>
            T{lastVerdict.turn}
          </span>
        )}
      </div>

      {/* Rationale (fades in after punch) */}
      {lastVerdict && showRationale && (
        <p
          className="font-body text-[12px] leading-snug shrink-0"
          style={{ color: "var(--muted)", maxHeight: 72, overflow: "hidden" }}
        >
          {(lastVerdict.why ?? lastVerdict.rationale ?? "").slice(0, 160)}
        </p>
      )}

      {/* Reasoning trend sparkline */}
      <div className="mt-1 shrink-0">
        <ReasoningTrend series={[youSeries]} height={110} title="Trend" />
      </div>

      {/* Recent verdict history (fills remaining height, scrolls) */}
      <div className="font-hud text-[9px] uppercase tracking-wider shrink-0 mt-1" style={{ color: "var(--muted)" }}>
        Recent
      </div>
      <div className="flex-1 overflow-y-auto flex flex-col gap-1 min-h-0">
        {history.length === 0 && (
          <p className="font-body text-[11px] italic" style={{ color: "var(--muted)" }}>
            No verdicts yet…
          </p>
        )}
        {history.map((v, i) => (
          <div
            key={`${v.turn}-${v.target}-${i}`}
            className="flex items-center gap-2 text-[11px] border-b border-white/5 pb-0.5"
          >
            <span className="font-hud" style={{ color: "var(--muted)" }}>
              T{v.turn}
            </span>
            <span className="font-display" style={{ color: "var(--accent)" }}>
              {Math.round(v.score)}
            </span>
            {v.damage > 0 && (
              <span className="ml-auto font-hud" style={{ color: "var(--danger)" }}>
                -{v.damage} HP
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
});
