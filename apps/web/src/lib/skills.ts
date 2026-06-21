/**
 * Shared skill parsing + elemental type colors (WS-G §5.2).
 *
 * Skills come back from the API as objects like
 *   {"name":"Emotional Appeal","type":"PATHOS","power":1.2,"description":"..."}
 * but older/seed data may store them as bare strings. `parseSkill` normalizes
 * both shapes so every screen (PartyScreen chips, BattleDebateView skill
 * buttons, etc.) renders them the same way.
 */

export interface ParsedSkill {
  /** Stable identifier sent to the backend as skill_id (== the skill name). */
  id: string;
  name: string;
  type: string; // DebateType, e.g. "PATHOS"; "" if unknown
  power: number; // damage multiplier; 1.0 default
  description: string;
  /** MP cost shipped by the backend skill catalog. */
  mp_cost: number;
  effect_kind:
    | "agent_argument"
    | "prompt_augment"
    | "defense"
    | "status"
    | "intel_preview"
    | "judge_sway";
  target: string;
  duration_turns: number;
  requires_prompt: boolean;
  rarity: string;
  modifiers: Record<string, unknown>;
}

function parseBool(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const v = value.toLowerCase();
    if (["true", "1", "yes"].includes(v)) return true;
    if (["false", "0", "no"].includes(v)) return false;
  }
  return fallback;
}

export function parseSkill(s: unknown): ParsedSkill {
  if (typeof s === "string") {
    return {
      id: s,
      name: s,
      type: "",
      power: 1,
      description: "",
      mp_cost: 0,
      effect_kind: "agent_argument",
      target: "enemy",
      duration_turns: 0,
      requires_prompt: false,
      rarity: "common",
      modifiers: {},
    };
  }
  if (s && typeof s === "object") {
    const o = s as Record<string, unknown>;
    const name = String(o.name ?? o.id ?? "Skill");
    const explicit = Number(o.mp_cost ?? o.cost ?? Number.NaN);
    const mp_cost = Number.isFinite(explicit) ? explicit : 0;
    const effect = String(o.effect_kind ?? "agent_argument");
    const effect_kind = (
      [
        "agent_argument",
        "prompt_augment",
        "defense",
        "status",
        "intel_preview",
        "judge_sway",
      ] as const
    ).includes(effect as ParsedSkill["effect_kind"])
      ? (effect as ParsedSkill["effect_kind"])
      : "agent_argument";
    const duration = Number(o.duration_turns ?? 0);
    const mods = o.modifiers && typeof o.modifiers === "object" && !Array.isArray(o.modifiers)
      ? (o.modifiers as Record<string, unknown>)
      : {};
    return {
      id: String(o.id ?? o.name ?? name),
      name,
      type: String(o.type ?? "").toUpperCase(),
      power: Number(o.power ?? 1) || 1,
      description: String(o.description ?? ""),
      mp_cost,
      effect_kind,
      target: String(o.target ?? "enemy"),
      duration_turns: Number.isFinite(duration) ? duration : 0,
      requires_prompt: parseBool(o.requires_prompt, effect_kind !== "agent_argument"),
      rarity: String(o.rarity ?? "common"),
      modifiers: mods,
    };
  }
  return {
    id: "Skill",
    name: "Skill",
    type: "",
    power: 1,
    description: "",
    mp_cost: 0,
    effect_kind: "agent_argument",
    target: "enemy",
    duration_turns: 0,
    requires_prompt: false,
    rarity: "common",
    modifiers: {},
  };
}

export function parseSkills(arr: unknown): ParsedSkill[] {
  if (!Array.isArray(arr)) return [];
  return arr.map(parseSkill);
}

/** Elemental (debate-type) -> CSS var color. Mirrors index.css + enums.ts. */
export const TYPE_COLOR: Record<string, string> = {
  LOGOS: "var(--logos)",
  PATHOS: "var(--pathos)",
  ETHOS: "var(--ethos)",
  CHAOS: "var(--chaos)",
  SOCRATIC: "var(--socratic)",
  RHETORIC: "var(--rhetoric)",
};

export function typeColor(type: string | undefined | null): string {
  return TYPE_COLOR[(type ?? "").toUpperCase()] ?? "var(--muted)";
}

/**
 * One-line "what it does + when to use" copy per debate type, used as a tooltip
 * fallback when a skill ships no `description` from the API. Keyed by DebateType.
 */
export const TYPE_BLURB: Record<string, string> = {
  LOGOS: "Logic & data. Best vs emotional (PATHOS) opponents.",
  PATHOS: "Emotion & story. Best vs credibility (ETHOS) opponents.",
  ETHOS: "Credibility & authority. Best vs disruptive (CHAOS) opponents.",
  CHAOS: "Disruption & reframing. Best vs logic (LOGOS) or style (RHETORIC).",
  SOCRATIC: "Questioning. Best vs style (RHETORIC) or emotion (PATHOS).",
  RHETORIC: "Style & framing. Best vs logic (LOGOS) opponents.",
};

/** Human-readable description for a skill chip's tooltip. */
export function effectLabel(effect: ParsedSkill["effect_kind"]): string {
  switch (effect) {
    case "agent_argument":
      return "Agent attack";
    case "prompt_augment":
      return "Prompt boost";
    case "defense":
      return "Defense";
    case "status":
      return "Status";
    case "intel_preview":
      return "Preview";
    case "judge_sway":
      return "Judge sway";
    default:
      return "Skill";
  }
}

/** Human-readable description for a skill chip's tooltip. */
export function skillTooltip(skill: ParsedSkill): string {
  const parts: string[] = [];
  if (skill.description) parts.push(skill.description);
  else if (TYPE_BLURB[skill.type]) parts.push(TYPE_BLURB[skill.type]);
  const dmg = skill.power >= 1 ? `+${Math.round((skill.power - 1) * 100)}% damage` : `${Math.round((1 - skill.power) * 100)}% less damage`;
  parts.push(`Type: ${skill.type || "—"} · Power ×${skill.power} (${dmg}).`);
  parts.push(`${effectLabel(skill.effect_kind)} · Target: ${skill.target || "—"}.`);
  if (skill.duration_turns > 0) parts.push(`Lasts ${skill.duration_turns} turn.`);
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// Type effectiveness (mirrors packages/shared/enums.ts TYPE_CHART).
// Kept local so the web bundle has no runtime dependency on the shared module's
// non-type exports; values must stay in sync with enums.ts.
// ---------------------------------------------------------------------------

const LOCAL_TYPE_CHART: Record<string, Record<string, number>> = {
  LOGOS: { PATHOS: 1.5, ETHOS: 0.75, CHAOS: 0.75 },
  PATHOS: { ETHOS: 1.5, LOGOS: 0.75, SOCRATIC: 0.75 },
  ETHOS: { CHAOS: 1.5, PATHOS: 0.75, RHETORIC: 0.75 },
  CHAOS: { LOGOS: 1.5, RHETORIC: 1.5, ETHOS: 0.75 },
  SOCRATIC: { RHETORIC: 1.5, PATHOS: 1.5, LOGOS: 0.75 },
  RHETORIC: { SOCRATIC: 0.75, LOGOS: 1.5, CHAOS: 0.75 },
};

/** Effectiveness multiplier of an attacking type vs a defending type. */
export function typeEffectiveness(
  attacker: string | null | undefined,
  defender: string | null | undefined
): number {
  const a = (attacker ?? "").toUpperCase();
  const d = (defender ?? "").toUpperCase();
  if (!a || !d) return 1;
  return LOCAL_TYPE_CHART[a]?.[d] ?? 1;
}

export interface EffectivenessInfo {
  multiplier: number;
  label: string; // "Super effective!" | "Not very effective" | ""
  color: string; // CSS var
}

/** Label + color for a skill's type vs the lead enemy's type. */
export function effectivenessInfo(
  attacker: string | null | undefined,
  defender: string | null | undefined
): EffectivenessInfo {
  const multiplier = typeEffectiveness(attacker, defender);
  if (multiplier > 1) return { multiplier, label: "Super effective!", color: "var(--win)" };
  if (multiplier < 1) return { multiplier, label: "Not very effective", color: "var(--danger)" };
  return { multiplier, label: "", color: "var(--muted)" };
}
