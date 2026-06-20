/**
 * ReasoningTrend — hand-rolled pixel SVG line chart (WS-G §4).
 *
 * Plots a reasoning score (0–100) per round for up to two series:
 *   - You          (--party)  the player's per-round judge score, live in battle
 *   - Trained agent(--accent) an agent's per-round score after a training cycle
 *
 * No chart dependency — a small SVG keeps the retro pixel aesthetic (hard
 * segments, no anti-aliased curves). Used in BattleDebateView and TrainingScreen.
 */

export interface TrendSeries {
  label: string;
  color: string; // CSS color (e.g. "var(--party)")
  points: number[]; // scores 0–100, one per round
}

export function ReasoningTrend({
  series,
  height = 140,
  title = "Reasoning",
}: {
  series: TrendSeries[];
  height?: number;
  title?: string;
}) {
  const W = 320;
  const H = height;
  const padL = 26;
  const padB = 16;
  const padT = 10;
  const padR = 8;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const maxLen = Math.max(1, ...series.map((s) => s.points.length));
  // X positions: even across the number of rounds (min 2 anchors so a single
  // point still renders as a dot at the left).
  const stepsX = Math.max(1, maxLen - 1);

  const x = (i: number) => padL + (stepsX === 0 ? 0 : (i / stepsX) * plotW);
  const y = (v: number) => padT + plotH - (Math.max(0, Math.min(100, v)) / 100) * plotH;

  const gridScores = [0, 25, 50, 75, 100];

  return (
    <div className="pixel-inset p-2">
      <div className="font-hud text-[10px] mb-1" style={{ color: "var(--muted)" }}>
        {title}
      </div>
      <svg
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        shapeRendering="crispEdges"
        style={{ display: "block" }}
      >
        {/* gridlines + y labels */}
        {gridScores.map((g) => (
          <g key={g}>
            <line
              x1={padL}
              y1={y(g)}
              x2={W - padR}
              y2={y(g)}
              stroke="rgba(232,230,216,0.10)"
              strokeWidth={1}
            />
            <text
              x={padL - 4}
              y={y(g) + 3}
              textAnchor="end"
              fontSize={7}
              fontFamily="var(--font-hud)"
              fill="var(--muted)"
            >
              {g}
            </text>
          </g>
        ))}

        {/* series */}
        {series.map((s) => {
          if (s.points.length === 0) return null;
          const pts = s.points.map((v, i) => `${x(i)},${y(v)}`).join(" ");
          return (
            <g key={s.label}>
              <polyline
                points={pts}
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeLinejoin="miter"
                strokeLinecap="square"
              />
              {s.points.map((v, i) => (
                <rect
                  key={i}
                  x={x(i) - 2}
                  y={y(v) - 2}
                  width={4}
                  height={4}
                  fill={s.color}
                />
              ))}
            </g>
          );
        })}
      </svg>

      {/* legend */}
      <div className="flex gap-3 mt-1 flex-wrap">
        {series.map((s) => (
          <div key={s.label} className="flex items-center gap-1">
            <span
              style={{ background: s.color, width: 8, height: 8, display: "inline-block" }}
            />
            <span className="font-hud text-[9px]" style={{ color: "var(--muted)" }}>
              {s.label}
              {s.points.length > 0 && (
                <span style={{ color: s.color }}> {Math.round(s.points[s.points.length - 1])}</span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default ReasoningTrend;
