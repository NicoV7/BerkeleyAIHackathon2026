// Shared catalogs mirrored from apps/api/app/db/models.py.
// Keep in sync by hand (small + stable). Generated request/response *types*
// live in types.gen.ts (from the API's OpenAPI schema).

export const DEBATE_TYPES = [
  "LOGOS", // logic / data
  "PATHOS", // emotion / story
  "ETHOS", // credibility / authority
  "CHAOS", // disruption / reframing
  "SOCRATIC", // questioning
  "RHETORIC", // style / framing
] as const;
export type DebateType = (typeof DEBATE_TYPES)[number];

export const EVENT_TYPES = ["BATTLE", "PLAYER", "CHARACTER"] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export const MONSTER_OWNERS = ["player", "wild", "enemy"] as const;
export type MonsterOwner = (typeof MONSTER_OWNERS)[number];

// Type-effectiveness chart (attacker -> defender -> multiplier).
// Rock-paper-scissors style; tuned later in Wave 2 balancing.
export const TYPE_CHART: Record<DebateType, Partial<Record<DebateType, number>>> = {
  LOGOS: { PATHOS: 1.5, ETHOS: 0.75, CHAOS: 0.75 },
  PATHOS: { ETHOS: 1.5, LOGOS: 0.75, SOCRATIC: 0.75 },
  ETHOS: { CHAOS: 1.5, PATHOS: 0.75, RHETORIC: 0.75 },
  CHAOS: { LOGOS: 1.5, RHETORIC: 1.5, ETHOS: 0.75 },
  SOCRATIC: { RHETORIC: 1.5, PATHOS: 1.5, LOGOS: 0.75 },
  RHETORIC: { SOCRATIC: 0.75, LOGOS: 1.5, CHAOS: 0.75 },
};

export function typeMultiplier(attacker: DebateType, defender: DebateType): number {
  return TYPE_CHART[attacker]?.[defender] ?? 1.0;
}
