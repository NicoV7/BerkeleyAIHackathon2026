/**
 * overworld-to-debate.spec.ts — full happy-path e2e for the debate spectacle.
 *
 * Flow under test (runs against the LIVE stack at http://localhost:5173):
 *   1. Load the app, type a player name, click "Start Game".
 *   2. Land in the overworld (Phaser canvas) and walk with arrow keys until
 *      walking into a red enemy fires an encounter (POST /api/runs/{id}/move
 *      returns an encounter_id -> store.setEncounter -> screen "encounter").
 *   3. Assert the BattleDebateView renders.
 *   4. Click "Auto (3)" (falls back to "Next Round") to advance the debate.
 *   5. Assert a judge verdict arrives AND the jumbo "ARGUMENT WON BECAUSE"
 *      (or "ARGUMENT FELL SHORT") banner appears, and that combatant HP
 *      changes from its starting value.
 *
 * Selectors are role/text based and deliberately resilient: the implementation
 * fleet is editing the source concurrently, so we avoid brittle CSS hooks.
 *
 * The overworld is procedural (random enemy placement) and the debate is driven
 * by a local model, so every wait uses generous timeouts and the map walk is a
 * bounded keyboard sweep rather than a fixed path.
 *
 * This is a LIVE-STACK test. When the dev server / API is not reachable on the
 * host (e.g. during collection on a machine where the stack is mid-edit), the
 * whole suite is skipped at runtime so `playwright test --list` always passes.
 */
import { expect, test, type Page } from "@playwright/test";

const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:5173";

// Generous budgets — local CPU model + procedural map wandering.
const NAV_TIMEOUT = 30_000;
const ENCOUNTER_TIMEOUT = 120_000;
const VERDICT_TIMEOUT = 180_000;
const MOVE_SETTLE_MS = 220; // > scene moveDelay (150ms) so each keypress lands.
const MAX_MOVE_STEPS = 400; // hard cap on the wander loop.

const PLAYER_NAME = "Ada";

/** True when the live dev server answers on BASE_URL. Used to skip on hosts. */
async function liveStackReachable(): Promise<boolean> {
  try {
    const res = await fetch(BASE_URL, { method: "GET" });
    return res.ok || res.status < 500;
  } catch {
    return false;
  }
}

/**
 * Walk the overworld via keyboard until the app routes to the encounter screen.
 * Sweeps in repeating right/down/left/up runs to maximize the chance of
 * colliding with a procedurally-placed enemy. Resolves once the BattleDebateView
 * is visible (detected by the action bar's "Next Round" / "Auto" controls).
 */
async function walkUntilEncounter(page: Page): Promise<void> {
  // The Phaser canvas must have focus for keyboard input to reach the scene.
  const canvas = page.locator("canvas");
  await expect(canvas).toBeVisible({ timeout: NAV_TIMEOUT });
  await canvas.click({ position: { x: 10, y: 10 } });

  // Battle view is "live" once an action control shows up.
  const nextRound = page.getByRole("button", { name: /next round/i });
  const autoBtn = page.getByRole("button", { name: /auto/i });

  const directions = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"] as const;
  const runLength = 6; // tiles per directional run before turning.

  for (let step = 0; step < MAX_MOVE_STEPS; step++) {
    const dir = directions[Math.floor(step / runLength) % directions.length];
    await page.keyboard.press(dir);
    await page.waitForTimeout(MOVE_SETTLE_MS);

    if ((await nextRound.count()) > 0 || (await autoBtn.count()) > 0) {
      return;
    }
  }

  throw new Error(
    `No encounter triggered after ${MAX_MOVE_STEPS} moves — overworld may have no reachable enemies.`
  );
}

/** Parse the first integer "n/m HP" pair out of the combatant cards' text. */
function parseFirstHp(text: string): number | null {
  const m = text.match(/(\d+)\s*\/\s*(\d+)\s*HP/i);
  return m ? Number(m[1]) : null;
}

test.describe("overworld → debate spectacle (live stack)", () => {
  test.beforeAll(async () => {
    const up = await liveStackReachable();
    test.skip(!up, `Live dev server not reachable at ${BASE_URL}; skipping e2e.`);
  });

  test.beforeEach(async ({ page }) => {
    page.setDefaultTimeout(NAV_TIMEOUT);
  });

  test("start a run, walk to an encounter, and win an argument with HP change", async ({
    page,
  }) => {
    // --- Arrange: load the app on the name-entry screen. ---
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

    const nameInput = page.getByRole("textbox", { name: /player name/i });
    await expect(nameInput).toBeVisible({ timeout: NAV_TIMEOUT });
    await nameInput.fill(PLAYER_NAME);

    const startRun = page.getByRole("button", { name: /start game/i });
    await expect(startRun).toBeEnabled();

    // --- Act: start the run → overworld. ---
    await startRun.click();

    // Overworld nav tabs confirm we've left the name screen.
    await expect(
      page.getByRole("button", { name: /^overworld$/i })
    ).toBeVisible({ timeout: NAV_TIMEOUT });

    // Walk until an encounter fires (may take a while on a procedural map).
    await test.step("walk the overworld until an encounter triggers", async () => {
      await walkUntilEncounter(page);
    });

    // --- Assert: the BattleDebateView is rendered. ---
    const nextRound = page.getByRole("button", { name: /next round/i });
    const autoBtn = page.getByRole("button", { name: /auto/i });
    await expect(nextRound.or(autoBtn).first()).toBeVisible({
      timeout: ENCOUNTER_TIMEOUT,
    });

    // The transcript panel header is part of the battle view chrome.
    await expect(page.getByText(/transcript/i).first()).toBeVisible({
      timeout: ENCOUNTER_TIMEOUT,
    });

    // Capture a starting HP value (combatant cards render "n/m HP").
    const hpCard = page.getByText(/\d+\s*\/\s*\d+\s*HP/i).first();
    await expect(hpCard).toBeVisible({ timeout: ENCOUNTER_TIMEOUT });
    const startingHpText = (await hpCard.textContent()) ?? "";
    const startingHp = parseFirstHp(startingHpText);

    // --- Act: advance the debate (prefer Auto for a faster verdict). ---
    await test.step("advance the debate", async () => {
      if ((await autoBtn.count()) > 0 && (await autoBtn.first().isEnabled())) {
        await autoBtn.first().click();
      } else {
        await expect(nextRound.first()).toBeEnabled({ timeout: ENCOUNTER_TIMEOUT });
        await nextRound.first().click();
      }
    });

    // --- Assert: the jumbo verdict banner appears. ---
    const jumboBanner = page.getByText(
      /ARGUMENT WON BECAUSE|ARGUMENT FELL SHORT/i
    );
    await expect(jumboBanner.first()).toBeVisible({ timeout: VERDICT_TIMEOUT });

    // A judge verdict badge (e.g. "[Judge T1]") should also be present.
    await expect(page.getByText(/judge/i).first()).toBeVisible({
      timeout: VERDICT_TIMEOUT,
    });

    // --- Assert: HP changed from its starting value after the round(s). ---
    await test.step("HP changes after the verdict lands", async () => {
      await expect
        .poll(
          async () => {
            const txt = (await hpCard.textContent()) ?? "";
            return parseFirstHp(txt);
          },
          {
            timeout: VERDICT_TIMEOUT,
            message: "expected a combatant's HP to change after the round",
          }
        )
        .not.toBe(startingHp);
    });
  });
});
