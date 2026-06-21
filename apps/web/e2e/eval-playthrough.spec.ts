/**
 * eval-playthrough.spec.ts — T4 eval: "does the demo actually work?"
 *
 * This is the executable, agent-plays-the-game eval. A single Playwright test
 * drives a FULL demo playthrough end-to-end and ASSERTS the spine outcome at
 * each beat, capturing a screenshot per beat into evals/shots/.
 *
 * The demo spine (and what each beat must prove):
 *
 *   BEAT 0  new run        name entry → "Start Run" → in-run nav appears.
 *   BEAT 1  walk           overworld Phaser canvas accepts movement; the agent
 *                          sweeps the map until it collides with a wild enemy
 *                          and the app routes to the encounter screen. (If the
 *                          procedural map yields no reachable enemy, the eval
 *                          falls back to the Demo arc's embedded fight so the
 *                          spine still runs — the fight beat is asserted either
 *                          way.)
 *   BEAT 2  debate + /auto the BattleDebateView renders; the agent runs the
 *                          debate via "Auto (3)" / "Next Round" until a judge
 *                          verdict lands. ASSERT: the jumbo verdict banner with
 *                          its "why" headline is present (verdict 'why' present).
 *   BEAT 3  reach a win    the agent keeps advancing until a combatant's HP
 *                          reaches 0 OR the encounter phase resolves
 *                          (won / capturable). ASSERT: HP reaches 0 for a side
 *                          (or the phase reaches a terminal/capturable state).
 *   BEAT 4  capture        on the Demo arc, the agent captures the weakened
 *                          wild. ASSERT: capture succeeds in demo mode
 *                          ("CAPTURED").
 *   BEAT 5  train          the agent runs GEPA training on the captured monster.
 *                          ASSERT: a training-score delta is rendered (the
 *                          before → after "training score" reveal, or the honest
 *                          "HELD ITS GROUND" Δ readout — both are valid rendered
 *                          deltas).
 *   BEAT 6  rematch        the agent rematches with the trained version.
 *                          ASSERT: the "TRAINED VERSION" rematch surface renders
 *                          and a fresh battle view mounts.
 *
 * RESILIENCE CONTRACT
 * -------------------
 * - This is a LIVE-STACK eval: it needs the Vite dev server + FastAPI + DB +
 *   model gateway up at http://localhost:5173. For demo determinism the API
 *   should run with DEMO_FORCE_CAPTURE truthy (see evals/README.md) so the
 *   capture beat cannot fail on a probabilistic roll.
 * - A SEPARATE implementation fleet edits the source concurrently, so selectors
 *   are role/text based with regex fallbacks rather than brittle CSS/test-ids.
 * - When the dev server is NOT reachable on the host (e.g. during collection
 *   while the stack is mid-edit), the whole suite SKIPS at runtime so that
 *   `playwright test --list` always passes on the host. The eval is meant to run
 *   green at integration time once the stack is up.
 *
 * Run: see evals/README.md.
 */
import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

// This package is ESM ("type": "module"), so __dirname is not defined; derive it.
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const BASE_URL =
  process.env.EVAL_BASE_URL ??
  process.env.PLAYWRIGHT_BASE_URL ??
  "http://localhost:5173";

// Where per-beat screenshots land. Resolved relative to this spec so it works
// regardless of the cwd Playwright is launched from.
const SHOTS_DIR = path.resolve(__dirname, "../../../evals/shots");

const PLAYER_NAME = "Ada";

// --- Generous budgets: local CPU model + procedural map + GEPA self-play. ---
const NAV_TIMEOUT = 30_000;
const ENCOUNTER_TIMEOUT = 120_000;
const VERDICT_TIMEOUT = 180_000;
const WIN_TIMEOUT = 240_000;
const TRAIN_TIMEOUT = 240_000;
const MOVE_SETTLE_MS = 220; // > scene moveDelay (150ms) so each keypress lands.
const MAX_MOVE_STEPS = 320; // hard cap on the overworld wander loop.
const MAX_DEBATE_NUDGES = 12; // how many times we re-press Auto/Next while waiting.

/** True when the live dev server answers on BASE_URL. Skips the suite if not. */
async function liveStackReachable(): Promise<boolean> {
  try {
    const res = await fetch(BASE_URL, { method: "GET" });
    return res.ok || res.status < 500;
  } catch {
    return false;
  }
}

/** Save a numbered, named beat screenshot into evals/shots/. */
async function shot(page: Page, beat: string): Promise<void> {
  await page
    .screenshot({ path: path.join(SHOTS_DIR, `${beat}.png`), fullPage: false })
    .catch(() => undefined);
}

/** Parse the first "n/m HP" pair out of a combatant card's text. */
function parseHp(text: string): { hp: number; max: number } | null {
  const m = text.match(/(\d+)\s*\/\s*(\d+)\s*HP/i);
  return m ? { hp: Number(m[1]), max: Number(m[2]) } : null;
}

/** Click an in-run nav tab by its (lowercase) label. */
async function openTab(page: Page, name: RegExp): Promise<boolean> {
  const tab = page.getByRole("button", { name }).first();
  if (await tab.isVisible().catch(() => false)) {
    await tab.click();
    return true;
  }
  return false;
}

/**
 * Walk the overworld via keyboard until the app routes to the encounter screen.
 * Sweeps in repeating right/down/left/up runs to maximize the chance of
 * colliding with a procedurally-placed enemy. Returns true if an encounter
 * fired (battle controls appeared), false if the wander loop exhausted.
 */
async function walkUntilEncounter(page: Page): Promise<boolean> {
  const canvas = page.locator("canvas");
  if (!(await canvas.isVisible().catch(() => false))) return false;
  // The Phaser canvas must have focus for keyboard input to reach the scene.
  await canvas.click({ position: { x: 10, y: 10 } }).catch(() => undefined);

  const nextRound = page.getByRole("button", { name: /next round/i });
  const autoBtn = page.getByRole("button", { name: /auto/i });

  const directions = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"] as const;
  const runLength = 6; // tiles per directional run before turning.

  for (let step = 0; step < MAX_MOVE_STEPS; step++) {
    const dir = directions[Math.floor(step / runLength) % directions.length];
    await page.keyboard.press(dir);
    await page.waitForTimeout(MOVE_SETTLE_MS);
    if ((await nextRound.count()) > 0 || (await autoBtn.count()) > 0) {
      return true;
    }
  }
  return false;
}

/**
 * Drive the embedded BattleDebateView toward a resolved/capturable phase.
 * Repeatedly presses "Auto (3)" (or "Next Round") while the battle is still in
 * progress, and resolves once any of:
 *   - the phase indicator reads "won" / "capturable", or
 *   - a combatant's HP reaches 0, or
 *   - a "Capture" action button appears (only rendered when capturable).
 * Returns the observed terminal signal for assertions.
 */
async function advanceBattleToResolution(page: Page): Promise<{
  hpReachedZero: boolean;
  capturable: boolean;
  won: boolean;
}> {
  const autoBtn = page.getByRole("button", { name: /^auto/i }).first();
  const nextRound = page.getByRole("button", { name: /next round/i }).first();
  const captureBtn = page.getByRole("button", { name: /^capture$/i }).first();
  // Phase chip text in the battle header is one of intro/debating/capturable/won/lost.
  const wonOrCapturablePhase = page.getByText(/\b(won|capturable)\b/i).first();

  let hpReachedZero = false;
  let capturable = false;
  let won = false;

  for (let nudge = 0; nudge < MAX_DEBATE_NUDGES; nudge++) {
    // Press Auto if it is enabled; otherwise fall back to Next Round.
    if (await autoBtn.isEnabled().catch(() => false)) {
      await autoBtn.click().catch(() => undefined);
    } else if (await nextRound.isEnabled().catch(() => false)) {
      await nextRound.click().catch(() => undefined);
    }

    // Wait for SOME terminal signal, polling the cheap observable signals.
    const settled = await page
      .waitForFunction(
        () => {
          const body = document.body.innerText;
          // A side at 0 HP, or a terminal phase, or the capture affordance.
          if (/\b0\s*\/\s*\d+\s*HP/i.test(body)) return "hp0";
          if (/\bcapturable\b/i.test(body)) return "capturable";
          if (/\bwon\b/i.test(body)) return "won";
          return false;
        },
        { timeout: WIN_TIMEOUT / MAX_DEBATE_NUDGES, polling: 750 },
      )
      .then((h) => h.jsonValue() as Promise<string | boolean>)
      .catch(() => false);

    if (settled === "hp0") hpReachedZero = true;
    if (settled === "capturable") capturable = true;
    if (settled === "won") won = true;

    // Confirm against the dedicated locators too (more robust than innerText).
    capturable =
      capturable ||
      (await captureBtn.isVisible().catch(() => false)) ||
      (await wonOrCapturablePhase.isVisible().catch(() => false));

    if (hpReachedZero || capturable || won) break;
  }

  return { hpReachedZero, capturable, won };
}

test.describe("T4 eval — full demo playthrough (live stack)", () => {
  // The arc is one continuous narrative; run its steps in order, one worker.
  test.describe.configure({ mode: "serial" });

  test.beforeAll(async () => {
    const up = await liveStackReachable();
    test.skip(!up, `Live dev server not reachable at ${BASE_URL}; skipping eval.`);
  });

  test.beforeEach(async ({ page }) => {
    page.setDefaultTimeout(NAV_TIMEOUT);
  });

  test("agent plays the full demo: run → debate → win → capture → train → rematch", async ({
    page,
  }) => {
    test.setTimeout(WIN_TIMEOUT + TRAIN_TIMEOUT + 120_000);

    // ====================================================================
    // BEAT 0 — NEW RUN
    // ====================================================================
    await test.step("beat 0 · start a new run from the name screen", async () => {
      // Arrange: load the app on the name-entry screen.
      await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
      const nameInput = page.getByRole("textbox", { name: /player name/i });
      await expect(nameInput).toBeVisible({ timeout: NAV_TIMEOUT });
      await nameInput.fill(PLAYER_NAME);

      const startRun = page.getByRole("button", { name: /start run/i });
      await expect(startRun).toBeEnabled();

      // Act: start the run → in-run navigation (overworld/.../demo) renders.
      await startRun.click();

      // Assert: we left the name screen — the in-run nav tabs are present.
      await expect(
        page.getByRole("button", { name: /^overworld$/i }),
      ).toBeVisible({ timeout: NAV_TIMEOUT });
      await expect(page.getByRole("button", { name: /^demo$/i })).toBeVisible({
        timeout: NAV_TIMEOUT,
      });
      await shot(page, "beat0-new-run");
    });

    // ====================================================================
    // BEAT 1 — WALK (overworld → encounter); collision triggers a debate.
    // ====================================================================
    let encounterFromOverworld = false;
    await test.step("beat 1 · walk the overworld until a wild encounter fires", async () => {
      await openTab(page, /^overworld$/i);
      await expect(page.locator("canvas")).toBeVisible({ timeout: NAV_TIMEOUT });
      await shot(page, "beat1-overworld");

      encounterFromOverworld = await walkUntilEncounter(page);
      // We do NOT hard-fail if the procedural map has no reachable enemy: the
      // Demo arc embeds the same fight, so the spine still runs. We only record
      // the path taken; the fight itself is asserted in beat 2.
    });

    // ====================================================================
    // Enter the Demo arc — the single continuous surface that carries the
    // capture → train → rematch climax (the embedded fight is the same
    // BattleDebateView the overworld routes into).
    // ====================================================================
    await test.step("open the Demo arc surface", async () => {
      const opened = await openTab(page, /^demo$/i);
      expect(opened, "the Demo nav tab should be present in-run").toBe(true);
      // The beat rail (Fight → Capture → Train → Rematch → Improved) renders.
      await expect(
        page.getByText(/\bfight\b/i).first(),
      ).toBeVisible({ timeout: NAV_TIMEOUT });
      await shot(page, "beat1b-demo-arc-fight");
    });

    // ====================================================================
    // BEAT 2 — DEBATE + /auto → judge verdict with a 'why' headline.
    // ====================================================================
    await test.step("beat 2 · run the debate via /auto and land a verdict with 'why'", async () => {
      // The Demo arc's fight beat embeds BattleDebateView with Auto/Next Round.
      const autoBtn = page.getByRole("button", { name: /^auto/i }).first();
      const nextRound = page.getByRole("button", { name: /next round/i }).first();
      await expect(autoBtn.or(nextRound)).toBeVisible({
        timeout: ENCOUNTER_TIMEOUT,
      });

      // Transcript panel chrome confirms the live battle view is mounted.
      await expect(page.getByText(/transcript/i).first()).toBeVisible({
        timeout: ENCOUNTER_TIMEOUT,
      });

      // Capture a starting HP reading for the BEAT 3 win assertion.
      const hpCard = page.getByText(/\d+\s*\/\s*\d+\s*HP/i).first();
      await expect(hpCard).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });

      // Act: kick off the debate (prefer Auto for a faster verdict).
      if (await autoBtn.isEnabled().catch(() => false)) {
        await autoBtn.click();
      } else {
        await expect(nextRound).toBeEnabled({ timeout: ENCOUNTER_TIMEOUT });
        await nextRound.click();
      }

      // Assert: the jumbo verdict banner appears (ARGUMENT WON / FELL SHORT)...
      const jumboBanner = page.getByText(
        /ARGUMENT WON BECAUSE|ARGUMENT FELL SHORT/i,
      );
      await expect(jumboBanner.first()).toBeVisible({ timeout: VERDICT_TIMEOUT });

      // ...AND a verdict 'why' headline is rendered. The banner quotes the
      // verdict's `why` text inside curly quotes; assert a non-empty quoted line
      // is present beneath the headline.
      const whyQuote = page.locator("text=/“.+”/").first();
      await expect(whyQuote).toBeVisible({ timeout: VERDICT_TIMEOUT });
      const whyText = (await whyQuote.textContent())?.trim() ?? "";
      expect(
        whyText.replace(/[“”"]/g, "").trim().length,
        "verdict 'why' headline should be non-empty",
      ).toBeGreaterThan(0);

      // A judge verdict badge should also be present in the verdicts panel.
      await expect(page.getByText(/judge/i).first()).toBeVisible({
        timeout: VERDICT_TIMEOUT,
      });
      await shot(page, "beat2-verdict-why");
    });

    // ====================================================================
    // BEAT 3 — REACH A WIN: a side's HP hits 0 (or the phase resolves).
    // ====================================================================
    let battleResolved = { hpReachedZero: false, capturable: false, won: false };
    await test.step("beat 3 · advance until a side's HP reaches 0 / the debate resolves", async () => {
      battleResolved = await advanceBattleToResolution(page);

      // The spine outcome is "HP reaches 0 for a side"; we accept the equivalent
      // terminal phase signals (won / capturable) as the same beat, since a
      // capturable wild is one whose HP dropped into the capture window.
      expect(
        battleResolved.hpReachedZero ||
          battleResolved.capturable ||
          battleResolved.won,
        "a side's HP should reach 0 OR the encounter should reach a won/capturable phase",
      ).toBe(true);

      // When HP did reach 0, prove it explicitly against a combatant card.
      if (battleResolved.hpReachedZero) {
        const zeroHp = page.getByText(/\b0\s*\/\s*\d+\s*HP/i).first();
        await expect(zeroHp).toBeVisible({ timeout: WIN_TIMEOUT });
        const parsed = parseHp((await zeroHp.textContent()) ?? "");
        expect(parsed?.hp).toBe(0);
      }
      await shot(page, "beat3-win");
    });

    // ====================================================================
    // BEAT 4 — CAPTURE the weakened wild (demo mode forces success).
    // ====================================================================
    await test.step("beat 4 · capture the weakened wild (succeeds in demo mode)", async () => {
      // Advance the Demo arc from the fight beat to the capture beat. The CTA
      // reads "Go to capture →" when capturable, "Skip to capture →" otherwise.
      const toCapture = page
        .getByRole("button", { name: /go to capture|skip to capture/i })
        .first();
      await expect(toCapture).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });
      await toCapture.click();

      // The capture beat shows the wild card + a "Capture" button.
      const captureBtn = page
        .getByRole("button", { name: /^capture$/i })
        .first();
      await expect(captureBtn).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });
      await shot(page, "beat4-capture-prompt");

      // Act: throw the ball.
      await captureBtn.click();

      // Assert: the capture succeeds — the beat flips to its "CAPTURED" state
      // and surfaces the "Train it →" continue affordance. (Demo determinism via
      // DEMO_FORCE_CAPTURE; see evals/README.md.)
      await expect(page.getByText(/^captured$/i).first()).toBeVisible({
        timeout: ENCOUNTER_TIMEOUT,
      });
      await expect(
        page.getByRole("button", { name: /train it/i }).first(),
      ).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });
      await shot(page, "beat4-captured");
    });

    // ====================================================================
    // BEAT 5 — TRAIN (GEPA) → a training-score delta is rendered.
    // ====================================================================
    await test.step("beat 5 · run GEPA training and render a score delta", async () => {
      // Continue from the capture beat into the train beat.
      await page.getByRole("button", { name: /train it/i }).first().click();

      const runTraining = page
        .getByRole("button", { name: /run training/i })
        .first();
      await expect(runTraining).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });
      await shot(page, "beat5-train-prompt");

      // Act: kick off GEPA self-play (blocks on the gateway; long timeout).
      await runTraining.click();

      // Assert: the before → after training-score reveal renders. The improved
      // branch shows a "training score" eyebrow with before/after numbers and a
      // "+N.N training score" delta; the honest branch shows "HELD ITS GROUND"
      // with a "Δ <number>" readout. Either is a valid rendered training delta.
      const trainingScore = page.getByText(/training score/i).first();
      const heldGround = page.getByText(/held its ground/i).first();
      await expect(trainingScore.or(heldGround)).toBeVisible({
        timeout: TRAIN_TIMEOUT,
      });

      if (await heldGround.isVisible().catch(() => false)) {
        // Honest non-positive delta: assert the Δ readout carries a number.
        const deltaReadout = page.getByText(/Δ\s*[-+]?\d/i).first();
        await expect(deltaReadout).toBeVisible({ timeout: TRAIN_TIMEOUT });
      } else {
        // Improved: assert both before/after endpoints and the signed delta.
        await expect(page.getByText(/^before$/i).first()).toBeVisible({
          timeout: TRAIN_TIMEOUT,
        });
        await expect(page.getByText(/^after$/i).first()).toBeVisible({
          timeout: TRAIN_TIMEOUT,
        });
        const signedDelta = page.getByText(/\+\s*\d+(\.\d+)?\s*training score/i);
        await expect(signedDelta.first()).toBeVisible({ timeout: TRAIN_TIMEOUT });
      }
      await shot(page, "beat5-training-delta");
    });

    // ====================================================================
    // BEAT 6 — REMATCH with the trained version.
    // ====================================================================
    await test.step("beat 6 · rematch with the trained version", async () => {
      const rematch = page
        .getByRole("button", { name: /rematch with the trained version/i })
        .first();
      await expect(rematch).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });
      await shot(page, "beat6-rematch-cta");

      // Act: start the rematch (creates a fresh encounter for the trained party).
      await rematch.click();

      // Assert: the rematch surface renders the "TRAINED VERSION" banner and a
      // fresh battle view mounts (Auto/Next Round controls return).
      await expect(page.getByText(/trained version/i).first()).toBeVisible({
        timeout: ENCOUNTER_TIMEOUT,
      });
      const battleControls = page
        .getByRole("button", { name: /^auto|next round/i })
        .first();
      await expect(battleControls).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });
      await shot(page, "beat6-rematch");
    });
  });
});
