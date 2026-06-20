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
}

export function parseSkill(s: unknown): ParsedSkill {
  if (typeof s === "string") {
    return { id: s, name: s, type: "", power: 1, description: "" };
  }
  if (s && typeof s === "object") {
    const o = s as Record<string, unknown>;
    const name = String(o.name ?? o.id ?? "Skill");
    return {
      id: String(o.id ?? o.name ?? name),
      name,
      type: String(o.type ?? "").toUpperCase(),
      power: Number(o.power ?? 1) || 1,
      description: String(o.description ?? ""),
    };
  }
  return { id: "Skill", name: "Skill", type: "", power: 1, description: "" };
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
