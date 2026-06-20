/**
 * Scoreboard — per-side debate scoring summary.
 *
 * Verdict scores are UNSIGNED 0-100 (50 = average). A side is "winning" when
 * its average score is ABOVE 50, so the trend arrow is derived from
 * (score - 50), NOT from the raw sign of the score (which is always positive).
 *
 * Consumes verdict + combatant state passed as props from BattleDebateView.
 */
import { CombatantState, JudgeVerdict } from "../ws/useEncounterStream";

interface SideStats {
  role: "party" | "enemy";
  label: string;
  /** Mean of verdict scores attributed to this side (0-100), or null if none. */
  avg: number | null;
  count: number;
}

/** ▲ above average, ▼ below, ▬ exactly average — derived from (avg - 50). */
function trendArrow(avg: number | null): { glyph: string; cls: string } {
  if (avg == null) return { glyph: "▬", cls: "text-white/30" };
  const delta = avg - 50;
  if (delta > 0.5) return { glyph: "▲", cls: "text-green-400" };
  if (delta < -0.5) return { glyph: "▼", cls: "text-red-400" };
  return { glyph: "▬", cls: "text-white/40" };
}

function computeSides(
  verdicts: JudgeVerdict[],
  combatants: CombatantState[]
): SideStats[] {
  // Map combatant id -> role so verdicts (keyed by actor_id/target) attribute
  // to the right side.
  const roleOf = new Map<string, "party" | "enemy">();
  for (const c of combatants) roleOf.set(c.monster_id, c.role);

  const acc: Record<"party" | "enemy", { sum: number; n: number }> = {
    party: { sum: 0, n: 0 },
    enemy: { sum: 0, n: 0 },
  };

  for (const v of verdicts) {
    const key = v.actor_id ?? v.target;
    const role = roleOf.get(key);
    if (role !== "party" && role !== "enemy") continue;
    acc[role].sum += v.score;
    acc[role].n += 1;
  }

  return (["party", "enemy"] as const).map((role) => ({
    role,
    label: role === "party" ? "Your Side" : "Opponent",
    avg: acc[role].n > 0 ? acc[role].sum / acc[role].n : null,
    count: acc[role].n,
  }));
}

export function Scoreboard({
  verdicts,
  combatants,
}: {
  verdicts: JudgeVerdict[];
  combatants: CombatantState[];
}) {
  const sides = computeSides(verdicts, combatants);

  return (
    <div className="flex items-stretch gap-3">
      {sides.map((s) => {
        const arrow = trendArrow(s.avg);
        const accent =
          s.role === "party"
            ? "border-indigo-500/50 bg-indigo-900/20"
            : "border-rose-500/50 bg-rose-900/20";
        const labelColor = s.role === "party" ? "text-indigo-300" : "text-rose-300";
        return (
          <div
            key={s.role}
            className={`flex-1 rounded-lg border ${accent} px-3 py-2 flex items-center justify-between`}
          >
            <div>
              <div className={`text-[10px] font-bold uppercase tracking-widest ${labelColor}`}>
                {s.label}
              </div>
              <div className="text-2xl font-black tabular-nums">
                {s.avg == null ? "—" : Math.round(s.avg)}
                <span className="text-xs font-normal text-white/40">/100</span>
              </div>
              <div className="text-[10px] text-white/40">
                {s.count} verdict{s.count === 1 ? "" : "s"}
              </div>
            </div>
            <div
              className={`text-3xl leading-none font-black ${arrow.cls}`}
              title={s.avg == null ? "no data" : `${(s.avg - 50).toFixed(0)} vs average`}
              aria-label={`${s.label} trend ${arrow.glyph}`}
            >
              {arrow.glyph}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default Scoreboard;
