/**
 * Scoreboard.test.tsx — unit tests for the per-side debate scoreboard.
 *
 * Scoreboard.tsx is a STABLE file (see header comment in source). These tests
 * pin the load-bearing scoring semantics:
 *   - per-side average is the mean of verdict scores attributed to that side
 *   - the trend arrow is derived from (avg - 50), NOT from the raw sign of the
 *     score (scores are always unsigned 0-100, so sign would always be +)
 *   - exactly-average (50) renders the neutral glyph, not a winning arrow
 *
 * Verdicts attribute to a side via verdict.actor_id (falling back to
 * verdict.target), resolved against the combatant roster's role field.
 *
 * Owned by T1 frontend unit wave. Does NOT import BattleDebateView (in flux).
 */
import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { Scoreboard } from "./Scoreboard";
import type {
  CombatantState,
  JudgeVerdict,
} from "../ws/useEncounterStream";

// ---------------------------------------------------------------------------
// Builders — keep test bodies focused on the Arrange data that matters.
// ---------------------------------------------------------------------------

function combatant(
  monster_id: string,
  role: "party" | "enemy"
): CombatantState {
  return {
    monster_id,
    name: monster_id,
    type: "logic",
    role,
    hp: 100,
    max_hp: 100,
  };
}

function verdict(
  partial: Partial<JudgeVerdict> & { score: number }
): JudgeVerdict {
  return {
    turn: partial.turn ?? 1,
    target: partial.target ?? "p1",
    score: partial.score,
    rationale: partial.rationale ?? "because",
    damage: partial.damage ?? 0,
    actor_id: partial.actor_id,
  };
}

/** The two side cards, ordered [party, enemy] as the component renders them. */
function sideCards(): { party: HTMLElement; enemy: HTMLElement } {
  // Each card exposes an aria-label "<Side label> trend <glyph>" on its arrow;
  // we locate cards by their distinctive labels instead.
  const partyLabel = screen.getByText("Your Side");
  const enemyLabel = screen.getByText("Opponent");
  // Walk up to the card container (the element that holds both the label and
  // the score). closest on the flex card wrapper.
  const party = partyLabel.closest("div.flex-1") as HTMLElement;
  const enemy = enemyLabel.closest("div.flex-1") as HTMLElement;
  return { party, enemy };
}

describe("Scoreboard per-side average", () => {
  it("renders the mean of verdict scores attributed to each side", () => {
    // Arrange: party p1 gets 80 & 60 (mean 70); enemy e1 gets 40 (mean 40).
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [
      verdict({ actor_id: "p1", score: 80 }),
      verdict({ actor_id: "p1", score: 60 }),
      verdict({ actor_id: "e1", score: 40 }),
    ];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);
    const { party, enemy } = sideCards();

    // Assert: rounded means displayed, with the /100 suffix alongside.
    expect(within(party).getByText("70")).toBeInTheDocument();
    expect(within(enemy).getByText("40")).toBeInTheDocument();
  });

  it("attributes a verdict via target when actor_id is absent", () => {
    // Arrange: no actor_id — the side is resolved from verdict.target.
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [verdict({ target: "e1", score: 90 })];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);
    const { party, enemy } = sideCards();

    // Assert: enemy shows 90, party has no data (em-dash placeholder).
    expect(within(enemy).getByText("90")).toBeInTheDocument();
    expect(within(party).getByText("—")).toBeInTheDocument();
  });

  it("shows a dash and zero count for a side with no verdicts", () => {
    // Arrange: only the party side is judged.
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [verdict({ actor_id: "p1", score: 55 })];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);
    const { enemy } = sideCards();

    // Assert: placeholder value and singular/plural-correct empty count.
    expect(within(enemy).getByText("—")).toBeInTheDocument();
    expect(within(enemy).getByText("0 verdicts")).toBeInTheDocument();
  });

  it("ignores verdicts whose actor maps to no known combatant role", () => {
    // Arrange: a verdict pointing at an id that isn't on the roster.
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [
      verdict({ actor_id: "p1", score: 70 }),
      verdict({ actor_id: "ghost", score: 100 }),
    ];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);
    const { party } = sideCards();

    // Assert: the orphan verdict did not pollute the party mean (still 70).
    expect(within(party).getByText("70")).toBeInTheDocument();
    expect(within(party).getByText("1 verdict")).toBeInTheDocument();
  });
});

describe("Scoreboard trend arrow from (avg - 50)", () => {
  it("shows the up arrow when average is above 50", () => {
    // Arrange: party mean 80 -> delta +30 -> winning.
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [verdict({ actor_id: "p1", score: 80 })];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);

    // Assert
    expect(
      screen.getByLabelText("Your Side trend ▲")
    ).toBeInTheDocument();
  });

  it("shows the down arrow when average is below 50 (score still positive)", () => {
    // Arrange: party mean 20 -> delta -30. The raw score sign is +; the arrow
    // must come from (avg - 50), so a positive score below 50 trends DOWN.
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [verdict({ actor_id: "p1", score: 20 })];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);

    // Assert
    expect(
      screen.getByLabelText("Your Side trend ▼")
    ).toBeInTheDocument();
  });

  it("shows the neutral glyph at exactly the average score of 50", () => {
    // Arrange: party mean exactly 50 -> delta 0 -> neutral, not winning.
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [verdict({ actor_id: "p1", score: 50 })];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);

    // Assert: neutral bar glyph, and explicitly NOT an up/down arrow.
    expect(
      screen.getByLabelText("Your Side trend ▬")
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Your Side trend ▲")).toBeNull();
    expect(screen.queryByLabelText("Your Side trend ▼")).toBeNull();
  });

  it("shows the neutral glyph for a side with no verdicts (null average)", () => {
    // Arrange: enemy has no verdicts -> avg null -> neutral.
    const combatants = [combatant("p1", "party"), combatant("e1", "enemy")];
    const verdicts = [verdict({ actor_id: "p1", score: 90 })];

    // Act
    render(<Scoreboard verdicts={verdicts} combatants={combatants} />);

    // Assert
    expect(screen.getByLabelText("Opponent trend ▬")).toBeInTheDocument();
  });
});
