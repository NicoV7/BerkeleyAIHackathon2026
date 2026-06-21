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

// Gacha wave: persona expertise domain. Drives the topic-match damage
// multiplier in `app.debate.topics.domain_match_mult` (Wave 0). GENERAL is the
// neutral default that always multiplies by 1.0.
export const MONSTER_DOMAINS = [
  "ENGINEERING",
  "PHILOSOPHY",
  "SCIENCE",
  "BUSINESS",
  "ETHICS",
  "ART",
  "GENERAL",
] as const;
export type MonsterDomain = (typeof MONSTER_DOMAINS)[number];

// Mirrors `app.debate.topics.domain_match_mult` exactly — keep in sync by hand.
//
//   * GENERAL on either side -> 1.0 (no nudge)
//   * matching domains       -> 1.2 (party-composition reward)
//   * mismatched domains     -> 0.9 (off-domain penalty)
export function domainMatchMult(monsterDomain: MonsterDomain, topicDomain: MonsterDomain): number {
  if (monsterDomain === "GENERAL" || topicDomain === "GENERAL") return 1.0;
  return monsterDomain === topicDomain ? 1.2 : 0.9;
}

// Summon item rarity — mirrors `app.db.models.SummonItemTier`.
export const SUMMON_ITEM_TIERS = ["common", "rare", "legendary"] as const;
export type SummonItemTier = (typeof SUMMON_ITEM_TIERS)[number];

// Economy item categories (WS-1) — mirrors `app.db.models.ItemKind`.
//   * potion_hp / potion_mp  — consumable, restores HP/MP
//   * camp_token             — consumed by the camp/rest flow
//   * training_atk/def/mp    — permanent stat-up applied to the lead party member
export const ITEM_KINDS = [
  "potion_hp",
  "potion_mp",
  "camp_token",
  "training_atk",
  "training_def",
  "training_mp",
] as const;
export type ItemKind = (typeof ITEM_KINDS)[number];

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
