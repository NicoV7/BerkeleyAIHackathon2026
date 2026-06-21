/**
 * introScript.ts — deterministic first-NPC dialogue script (DATA + TYPES ONLY).
 *
 * This is the scripted intro the player sees at the very start of a fresh run:
 * the first NPC greets them, explains that "reasoning is the weapon," offers the
 * first quest, and — on acceptance — triggers the first gacha pull so the player
 * leaves the conversation with their first agent.
 *
 * WS-0-UI owns this as a CONTENT contract. WS-3 (dialogue/NPC) WIRES it:
 *   - render `INTRO_SCRIPT.lines` in order through the dialogue surface,
 *   - at the choice line, render `choices` via <ListMenu>,
 *   - on the choice whose `effect` is "accept_quest_and_pull", dispatch the
 *     first-quest acceptance + the gacha pull (App.tsx already has the gacha
 *     gate; see UI_CONTRACT.md §Intro flow).
 *
 * Nothing here imports React or calls APIs — it is pure, testable data.
 */

/** What activating a choice should cause the wiring layer to do. */
export type IntroChoiceEffect =
  /** Accept the first quest AND trigger the first gacha pull, then close. */
  | "accept_quest_and_pull"
  /** Re-show the explanation lines (player asked "what do you mean?"). */
  | "repeat_explanation"
  /** Dismiss the dialogue without accepting (player can return later). */
  | "decline";

export interface IntroLine {
  /** Stable id for keying + analytics. */
  id: string;
  /** Who is speaking. "npc" = the guide; "narration" = scene voice. */
  speaker: "npc" | "narration";
  /** The line of text. */
  text: string;
}

export interface IntroChoice {
  id: string;
  /** Label shown in the choice list. */
  label: string;
  /** What the wiring layer should do when this choice is activated. */
  effect: IntroChoiceEffect;
}

export interface IntroScript {
  /** The NPC the wiring layer should attach this script to. */
  npcId: string;
  /** Display name for the speaker label. */
  npcName: string;
  npcArchetype: "quest_giver";
  /** Ordered lines, shown one at a time (advance on click / Enter). */
  lines: IntroLine[];
  /** Prompt shown above the choice list at the end of the lines. */
  choicePrompt: string;
  /** The branching choice that gates quest acceptance + the first pull. */
  choices: IntroChoice[];
}

/** Identifier the NPC layer + quest layer agree on for the very first quest. */
export const FIRST_QUEST_ID = "q_first_summon";

/** The deterministic opening conversation. */
export const INTRO_SCRIPT: IntroScript = {
  npcId: "elder_mara",
  npcName: "Elder Mara",
  npcArchetype: "quest_giver",
  lines: [
    {
      id: "intro-1",
      speaker: "narration",
      text: "A figure in a frayed scholar's cloak waits at the crossroads, as if she'd expected you.",
    },
    {
      id: "intro-2",
      speaker: "npc",
      text: "So. Another wanderer who thinks the loudest voice wins. You'll learn quickly that it doesn't.",
    },
    {
      id: "intro-3",
      speaker: "npc",
      text: "In these lands the weapon is reasoning. A sharp argument cuts deeper than any blade — and you walk in unarmed.",
    },
    {
      id: "intro-4",
      speaker: "npc",
      text: "But you needn't fight alone. Speakers — agents of pure rhetoric — can be summoned to argue at your side. You have none yet.",
    },
    {
      id: "intro-5",
      speaker: "npc",
      text: "Take this charm. Summon your first Speaker, and I'll mark your map with a debate worth winning.",
    },
  ],
  choicePrompt: "Will you accept the charm?",
  choices: [
    {
      id: "choice-accept",
      label: "Take the charm and summon a Speaker.",
      effect: "accept_quest_and_pull",
    },
    {
      id: "choice-explain",
      label: '"Reasoning is the weapon?" Explain.',
      effect: "repeat_explanation",
    },
    {
      id: "choice-decline",
      label: "Not yet. (Walk away.)",
      effect: "decline",
    },
  ],
};

/**
 * Copy for the empty Party state before the first summon. Surfaces (PartyScreen,
 * Adventure menu) should render this verbatim so the new-player funnel is
 * consistent. `{npc}` is substituted with INTRO_SCRIPT.npcName by the renderer.
 */
export const EMPTY_PARTY_COPY = {
  title: "No Speakers yet",
  message: `Recruit your first agent — talk to ${INTRO_SCRIPT.npcName} at the crossroads.`,
  /** Short CTA label if the surface wants a button. */
  cta: "Find the crossroads",
} as const;

export type EmptyPartyCopy = typeof EMPTY_PARTY_COPY;

/**
 * Greeting + the diegetic action a merchant/innkeeper NPC offers in the dialogue
 * menu (WS-3, #13). NPCDialogue renders the greeting as the line and the action
 * label as a ListMenu choice that opens the matching diegetic surface (Shop /
 * Camp). These are pure data so the dialogue menu stays declarative.
 */
export const MERCHANT_DIALOGUE = {
  greeting: "Goods for the road, traveler? Coin talks louder than any rebuttal here.",
  actionLabel: "Browse the wares.",
} as const;

export const INNKEEPER_DIALOGUE = {
  greeting: "Weary? Pitch a tent, rest your party, and sharpen your arguments.",
  actionLabel: "Make camp.",
} as const;
