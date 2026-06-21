/**
 * itemMeta.ts — presentation helpers for economy items (DATA + PURE fns ONLY).
 *
 * The backend economy catalog (app/economy/catalog.py) is the source of truth
 * for item keys, names, effects, and prices. This module only adds front-end
 * flavor — a glyph and a one-line "what does it do here" blurb keyed off the
 * item `kind` — so Inventory / Shop / Camp read consistently. No React, no API.
 */

/** Item kinds as returned by the economy API (`InventoryItem.kind` / `ShopItem.kind`). */
export type ItemKind =
  | "potion_hp"
  | "potion_mp"
  | "camp_token"
  | "training_atk"
  | "training_def"
  | "training_mp"
  | string;

/** A leading glyph for an item kind (one glyph max, per the button grammar). */
export function itemGlyph(kind: ItemKind): string {
  switch (kind) {
    case "potion_hp":
      return "❤️";
    case "potion_mp":
      return "🔷";
    case "camp_token":
      return "🏕";
    case "training_atk":
      return "⚔️";
    case "training_def":
      return "🛡";
    case "training_mp":
      return "✨";
    default:
      return "📦";
  }
}

/** A short, in-world description of what consuming the item does. */
export function itemBlurb(kind: ItemKind, effect: Record<string, unknown> = {}): string {
  const n = (k: string) => Number(effect[k] ?? 0);
  switch (kind) {
    case "potion_hp":
      return `Restores ${n("hp")} HP to your lead Speaker in battle.`;
    case "potion_mp":
      return `Restores ${n("mp")} MP to your lead Speaker in battle.`;
    case "camp_token":
      return "Lets you make camp once — rest and train your party.";
    case "training_atk":
      return `Permanently raises ATK by ${n("atk")}.`;
    case "training_def":
      return `Permanently raises DEF by ${n("def")}.`;
    case "training_mp":
      return `Permanently raises Max MP by ${n("mp")}.`;
    default:
      return "A curious item.";
  }
}

/** True for items that train (permanently raise a stat) — used by the Camp screen. */
export function isTrainingItem(kind: ItemKind): boolean {
  return kind === "training_atk" || kind === "training_def" || kind === "training_mp";
}

/** True for items that are usable directly from the Inventory overlay. */
export function isUsableInInventory(kind: ItemKind): boolean {
  // Potions heal the lead combatant (best-effort even with no live battle), and
  // camp tokens are banked/consumed by the camp flow. Training items are also
  // consumable but are routed through Camp's "train" action for clearer UX.
  return kind === "potion_hp" || kind === "potion_mp" || kind === "camp_token";
}
