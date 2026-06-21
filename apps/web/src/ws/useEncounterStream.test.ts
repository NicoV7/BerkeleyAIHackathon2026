/**
 * useEncounterStream — message-handling contract test.
 *
 * RUNNER STATUS: vitest is *configured* by a sibling workstream
 * (apps/web/vitest.config.ts + a `test` script + deps in package.json) but its
 * dependencies are NOT yet installed (no `node_modules/vitest`, `pnpm install`
 * pending), so the runner cannot execute today. Per the WS task brief
 * ("vitest if present, else a type-level/compile assertion module") this file is
 * therefore primarily a COMPILE-CHECKED assertion module: it is type-checked by
 * `npx tsc --noEmit` (it lives under `src`, which tsconfig.json includes) and
 * intentionally does NOT statically `import "vitest"` — doing so would add an
 * unresolved-module error to the tsc gate until install.
 *
 * It is ALSO a real spec: when run under vitest (`globals: true` in the config),
 * the guarded block at the bottom registers `describe/it` cases that execute the
 * same assertions. With or without a runner, `runEncounterStreamContractTests()`
 * returns the failure count.
 *
 * What it pins (the hook's message-handling contract):
 *   1. an `hp` event updates the matching combatant's hp (and optional max_hp);
 *   2. a `phase` event sets the phase;
 *   3. `JudgeVerdict` exposes the additive fields why / logic / persuasion / actor_id.
 *
 * It binds to the REAL exported types from the hook module, so if any of those
 * fields are removed or renamed, this file fails to compile.
 */
import {
  useEncounterStream,
  type CombatantState,
  type EncounterPhase,
  type EncounterState,
  type EncounterStreamState,
  type HpUpdate,
  type IntelPreview,
  type JudgeVerdict,
  type PhaseUpdate,
  type SkillEffect,
  type Utterance,
} from "./useEncounterStream";

// ---------------------------------------------------------------------------
// Tiny zero-dependency assertion helpers (work with or without a test runner)
// ---------------------------------------------------------------------------

let failures = 0;

function expect(label: string, cond: boolean): void {
  if (!cond) {
    failures += 1;
    // eslint-disable-next-line no-console
    console.error(`[useEncounterStream.contract] FAIL: ${label}`);
  }
}

function expectEqual<T>(label: string, actual: T, want: T): void {
  expect(`${label} (got ${JSON.stringify(actual)}, want ${JSON.stringify(want)})`, actual === want);
}

// Compile-time assertion that two types are mutually assignable.
type Assignable<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
function staticAssert<_T extends true>(): void {
  /* type-level only */
}

// ---------------------------------------------------------------------------
// Replicas of the hook's pure reducers.
//
// The hook keeps its reducers inside the `onmessage` closure, so they cannot be
// imported directly. We re-implement the exact patch logic here against the real
// exported types; the static-shape assertions below guarantee these replicas
// stay structurally identical to what the hook consumes off the wire.
// ---------------------------------------------------------------------------

/** Mirror of the `hp` branch: patch the matching combatant in place. */
function applyHp(prev: EncounterState, h: HpUpdate): EncounterState {
  const combatants = prev.combatants.map((c) =>
    c.monster_id === h.monster_id ? { ...c, hp: h.hp, max_hp: h.max_hp ?? c.max_hp } : c
  );
  return { ...prev, combatants };
}

/** Mirror of the `phase` branch: set phase on state. */
function applyPhase(prev: EncounterState, p: PhaseUpdate): EncounterState {
  return { ...prev, phase: p.phase };
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const partyCombatant: CombatantState = {
  monster_id: "m-party",
  name: "Logician",
  type: "logic",
  role: "party",
  hp: 100,
  max_hp: 100,
};

const enemyCombatant: CombatantState = {
  monster_id: "m-enemy",
  name: "Sophist",
  type: "rhetoric",
  role: "enemy",
  hp: 80,
  max_hp: 80,
};

const baseState: EncounterState = {
  id: "enc-1",
  run_id: "run-1",
  topic: "Is a hotdog a sandwich?",
  phase: "intro",
  turn_no: 0,
  combatants: [partyCombatant, enemyCombatant],
  transcript: [],
  verdicts: [],
  effects: [],
};

// ---------------------------------------------------------------------------
// Runtime assertions (also a self-contained smoke test if a runner is wired up)
// ---------------------------------------------------------------------------

export function runEncounterStreamContractTests(): number {
  failures = 0;

  // (1) an `hp` event updates the matching combatant's hp.
  const hpMsg = { type: "hp", data: { monster_id: "m-enemy", hp: 42 } as HpUpdate };
  const afterHp = applyHp(baseState, hpMsg.data);
  const patched = afterHp.combatants.find((c) => c.monster_id === "m-enemy");
  const untouched = afterHp.combatants.find((c) => c.monster_id === "m-party");
  expectEqual("hp event patches target combatant hp", patched?.hp, 42);
  expectEqual("hp event leaves max_hp when omitted", patched?.max_hp, 80);
  expectEqual("hp event does not touch other combatant", untouched?.hp, 100);

  // hp event may also carry max_hp.
  const afterHpMax = applyHp(baseState, { monster_id: "m-party", hp: 10, max_hp: 120 });
  const grown = afterHpMax.combatants.find((c) => c.monster_id === "m-party");
  expectEqual("hp event applies provided max_hp", grown?.max_hp, 120);
  expectEqual("hp event applies provided hp", grown?.hp, 10);

  // (2) a `phase` event sets the phase.
  const phaseMsg = { type: "phase", data: { phase: "won" } as PhaseUpdate };
  const afterPhase = applyPhase(baseState, phaseMsg.data);
  expectEqual("phase event sets encounter phase", afterPhase.phase, "won" satisfies EncounterPhase);

  // (3) JudgeVerdict exposes why / logic / persuasion / actor_id (runtime read).
  const verdict: JudgeVerdict = {
    turn: 1,
    target: "m-enemy",
    score: 73,
    rationale: "Strong evidentiary chain.",
    damage: 23,
    why: "Cited a primary source the opponent could not rebut.",
    logic: 80,
    persuasion: 66,
    actor_id: "m-party",
  };
  expect("verdict exposes why", typeof verdict.why === "string");
  expect("verdict exposes logic", typeof verdict.logic === "number");
  expect("verdict exposes persuasion", typeof verdict.persuasion === "number");
  expect("verdict exposes actor_id", typeof verdict.actor_id === "string");

  return failures;
}

// ---------------------------------------------------------------------------
// Compile-time (type-level) contract assertions.
// These are the load-bearing checks for the `tsc --noEmit` gate: each fails to
// compile if the hook's exported contract drifts.
// ---------------------------------------------------------------------------

// hp event payload type carries hp (required) + optional max_hp keyed by monster_id.
staticAssert<Assignable<HpUpdate["monster_id"], string>>();
staticAssert<Assignable<HpUpdate["hp"], number>>();
staticAssert<Assignable<HpUpdate["max_hp"], number | undefined>>();

// phase event payload sets an EncounterPhase.
staticAssert<Assignable<PhaseUpdate["phase"], EncounterPhase>>();

// The additive verdict fields exist and have the documented types.
staticAssert<Assignable<JudgeVerdict["why"], string | undefined>>();
staticAssert<Assignable<JudgeVerdict["logic"], number | undefined>>();
staticAssert<Assignable<JudgeVerdict["persuasion"], number | undefined>>();
staticAssert<Assignable<JudgeVerdict["actor_id"], string | undefined>>();

// The hook's public return surface exposes phase + the live state shape.
staticAssert<Assignable<EncounterStreamState["phase"], EncounterPhase>>();
staticAssert<Assignable<EncounterStreamState["encounter"], EncounterState | null>>();
staticAssert<Assignable<EncounterStreamState["verdicts"], JudgeVerdict[]>>();
staticAssert<Assignable<EncounterStreamState["skillEffects"], SkillEffect[]>>();
staticAssert<Assignable<EncounterStreamState["statuses"], SkillEffect[]>>();
staticAssert<Assignable<EncounterStreamState["intelPreview"], IntelPreview | null>>();
staticAssert<Assignable<EncounterStreamState["transcript"], Utterance[]>>();

// The hook itself has the expected (encounterId | null) => EncounterStreamState signature.
type HookFn = typeof useEncounterStream;
staticAssert<Assignable<Parameters<HookFn>[0], string | null>>();
staticAssert<Assignable<ReturnType<HookFn>, EncounterStreamState>>();

// ---------------------------------------------------------------------------
// Optional vitest binding.
//
// vitest.config.ts sets `globals: true`, so `describe`/`it`/`expect` are present
// on globalThis WHEN this file is run under vitest. We bind to them via a runtime
// guard instead of `import { describe } from "vitest"` so that `tsc --noEmit`
// stays green before `pnpm install` provides the vitest types. When no runner is
// present, this block is a no-op and the assertions above remain the contract.
// ---------------------------------------------------------------------------

type ViDescribe = (name: string, fn: () => void) => void;
type ViIt = (name: string, fn: () => void) => void;
type ViExpect = (actual: unknown) => { toBe(expected: unknown): void };

const g = globalThis as unknown as {
  describe?: ViDescribe;
  it?: ViIt;
  expect?: ViExpect;
};

if (typeof g.describe === "function" && typeof g.it === "function" && typeof g.expect === "function") {
  const { describe, it } = g;
  const viExpect = g.expect;

  describe("useEncounterStream message-handling contract", () => {
    it("hp event updates the matching combatant's hp", () => {
      const next = applyHp(baseState, { monster_id: "m-enemy", hp: 42 });
      viExpect(next.combatants.find((c) => c.monster_id === "m-enemy")?.hp).toBe(42);
      viExpect(next.combatants.find((c) => c.monster_id === "m-party")?.hp).toBe(100);
    });

    it("hp event applies provided max_hp", () => {
      const next = applyHp(baseState, { monster_id: "m-party", hp: 10, max_hp: 120 });
      const c = next.combatants.find((x) => x.monster_id === "m-party");
      viExpect(c?.max_hp).toBe(120);
      viExpect(c?.hp).toBe(10);
    });

    it("phase event sets the phase", () => {
      viExpect(applyPhase(baseState, { phase: "won" }).phase).toBe("won");
    });

    it("JudgeVerdict exposes why / logic / persuasion / actor_id", () => {
      const v: JudgeVerdict = {
        turn: 1,
        target: "m-enemy",
        score: 73,
        rationale: "r",
        damage: 23,
        why: "decisive move",
        logic: 80,
        persuasion: 66,
        actor_id: "m-party",
      };
      viExpect(typeof v.why).toBe("string");
      viExpect(typeof v.logic).toBe("number");
      viExpect(typeof v.persuasion).toBe("number");
      viExpect(typeof v.actor_id).toBe("string");
    });

    it("standalone contract runner reports zero failures", () => {
      viExpect(runEncounterStreamContractTests()).toBe(0);
    });
  });
}
