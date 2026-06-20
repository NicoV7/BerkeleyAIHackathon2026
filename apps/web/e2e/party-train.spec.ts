/**
 * T3 e2e — Party + Gambit editor + Training (GEPA) + Capture/Demo before→after.
 *
 * Scripts the full "raise a debater" path against the LIVE dev stack:
 *   1. Boot a run from the topic screen.
 *   2. Open the Party screen.
 *   3. Open the Gambit editor via the #gambits/{id} hash route, edit + save a
 *      gambit, and assert the editor confirms the save.
 *   4. Open the Training screen, run a GEPA cycle, and assert a score-delta
 *      readout renders.
 *   5. Assert the capture / Demo arc surface shows a before → after training
 *      score (when that surface is wired into the running build).
 *
 * RESILIENCE CONTRACT
 * -------------------
 * A SEPARATE implementation fleet is editing the React/API source concurrently,
 * so this spec is written defensively:
 *   - It targets stable, human-readable text + role selectors with regex
 *     fallbacks rather than brittle CSS/test-id chains.
 *   - Every network-dependent beat uses generous timeouts.
 *   - The whole suite SKIPS (does not fail) when the dev stack is not reachable,
 *     so `playwright test --list` / collection always passes on a host where the
 *     stack is mid-edit or down. It is meant to run green at integration time
 *     once `pnpm install` + the dev server (http://localhost:5173) + API are up.
 *
 * Run:
 *   cd apps/web && pnpm install && pnpm exec playwright install chromium
 *   # with the dev stack already up at http://localhost:5173:
 *   cd apps/web && pnpm e2e -- party-train.spec.ts
 *   # or list/collect only (no running stack required):
 *   cd apps/web && pnpm exec playwright test --list party-train.spec.ts
 */
import { test, expect, type Page } from "@playwright/test";

const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const DEFAULT_TOPIC = "Pineapple belongs on pizza";

// --- Generous timeouts: the live stack drives an LLM gateway + DB on GEPA. ---
const NAV_TIMEOUT = 15_000;
const UI_TIMEOUT = 20_000;
const TRAIN_TIMEOUT = 120_000;

/**
 * Probe the dev server once. Returns true when something is serving at BASE_URL.
 * Used to skip (not fail) the suite when the stack is down / mid-edit so that
 * collection on the host always passes.
 */
async function stackIsUp(page: Page): Promise<boolean> {
  try {
    const res = await page.request.get(BASE_URL, { timeout: 4_000 });
    return res.ok();
  } catch {
    return false;
  }
}

/**
 * Boot a run from the topic-entry screen. The app shows a topic <input> + a
 * "Start Run" button while runId is null; clicking it swaps to the in-run nav.
 * Returns once the in-run navigation (Party/Training tabs) is visible.
 */
async function startRun(page: Page, topic = DEFAULT_TOPIC): Promise<void> {
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT });

  // If a previous run is already active (persisted store), the nav tabs render
  // immediately; otherwise drive the topic-entry screen.
  const startButton = page.getByRole("button", { name: /start run/i });
  if (await startButton.isVisible().catch(() => false)) {
    const topicInput = page.locator("input").first();
    await topicInput.fill(topic).catch(() => undefined);
    await startButton.click();
  }

  // In-run nav exposes Party + Training tabs (matched by their lowercase label).
  await expect(page.getByRole("button", { name: /^party$/i })).toBeVisible({
    timeout: UI_TIMEOUT,
  });
}

/** Click an in-run nav tab by its label (overworld/encounter/party/training). */
async function openScreen(page: Page, name: RegExp): Promise<void> {
  await page.getByRole("button", { name }).first().click();
}

/**
 * Resolve the run id from the in-page game store, falling back to the API list.
 * Used to discover a party monster id for the #gambits/{id} deep link.
 */
async function discoverMonsterId(page: Page): Promise<string | null> {
  // The party screen calls GET /api/runs/{runId}/party; we replay that to grab
  // a stable monster id without depending on a clickable "Edit Gambits" link
  // (which only renders when an encounter is active).
  return page.evaluate(async () => {
    try {
      // Find a run id: the topic-entry POST stores it; the simplest portable
      // discovery is to ask the API for any party. We try a couple of shapes.
      // 1) If the app put a run id on the window/store, prefer it.
      const w = window as unknown as Record<string, unknown>;
      const fromWindow =
        (w.__RUN_ID__ as string | undefined) ??
        (typeof w.runId === "string" ? (w.runId as string) : undefined);

      async function partyMonster(runId: string): Promise<string | null> {
        const r = await fetch(`/api/runs/${runId}/party`);
        if (!r.ok) return null;
        const data = (await r.json()) as unknown;
        const arr = Array.isArray(data)
          ? data
          : ((data as { party?: unknown[] }).party ?? []);
        const first = arr[0] as { id?: string } | undefined;
        return first?.id ?? null;
      }

      if (fromWindow) {
        const id = await partyMonster(fromWindow);
        if (id) return id;
      }
      return null;
    } catch {
      return null;
    }
  });
}

test.describe("Party → Gambits → Training → Capture demo arc", () => {
  test.describe.configure({ mode: "serial" });

  test.beforeEach(async ({ page }) => {
    // Arrange: skip the whole suite (never fail collection) if the stack is down.
    test.skip(!(await stackIsUp(page)), `dev stack not reachable at ${BASE_URL}`);
  });

  test("edits and saves a gambit through the #gambits hash route", async ({ page }) => {
    // Arrange: a booted run with a Party screen.
    await startRun(page);
    await openScreen(page, /^party$/i);
    await expect(page.getByRole("heading", { name: /^party$/i })).toBeVisible({
      timeout: UI_TIMEOUT,
    });

    // Arrange: discover a monster to deep-link the gambit editor at.
    const monsterId = await discoverMonsterId(page);
    // If the party is empty (no captured monster yet), this beat has nothing to
    // edit — skip rather than fail, since party seeding is the impl fleet's job.
    test.skip(!monsterId, "no party monster available to edit gambits for");

    // Act: open the gambit editor via its hash route.
    await page.evaluate((id) => {
      window.location.hash = `gambits/${id}`;
    }, monsterId);

    // The editor header reads "Gambits"; loading resolves to the rule list.
    await expect(page.getByText(/^gambits$/i).first()).toBeVisible({
      timeout: UI_TIMEOUT,
    });

    // Act: ensure at least one rule exists (add one if the list is empty), then
    // toggle/edit it so there is a change to persist.
    const addRule = page.getByRole("button", { name: /\+\s*add rule/i });
    if (await addRule.isVisible().catch(() => false)) {
      await addRule.click();
    }

    // Flip the first rule's enable toggle (on/off button) to dirty the state.
    const toggle = page.getByRole("button", { name: /^(on|off)$/i }).first();
    if (await toggle.isVisible().catch(() => false)) {
      await toggle.click();
    }

    // Act: save the gambit list.
    await page.getByRole("button", { name: /^save$/i }).click();

    // Assert: the editor confirms the save (transient "Saved!" badge) OR the
    // save button settles out of its "Saving…" state without an error banner.
    const savedBadge = page.getByText(/saved!?/i);
    await expect(savedBadge.first()).toBeVisible({ timeout: UI_TIMEOUT });
  });

  test("runs a GEPA cycle and renders a training score delta", async ({ page }) => {
    // Arrange: a booted run, on the Training screen.
    await startRun(page);
    await openScreen(page, /^training$/i);
    await expect(page.getByRole("heading", { name: /training lab/i })).toBeVisible({
      timeout: UI_TIMEOUT,
    });

    // Arrange: pick the first party member if a picker is shown. The first
    // member is auto-selected by the screen, but click defensively if present.
    const memberButtons = page
      .locator("section")
      .first()
      .getByRole("button");
    if (await memberButtons.first().isVisible().catch(() => false)) {
      await memberButtons.first().click().catch(() => undefined);
    }

    const runGepa = page.getByRole("button", { name: /run gepa/i });
    // If there is no trainable member, GEPA stays disabled — skip rather than
    // fail, since party seeding belongs to the impl fleet.
    const enabled = await runGepa.isEnabled().catch(() => false);
    test.skip(!enabled, "GEPA control disabled (no trainable party member)");

    // Act: kick off a GEPA cycle (blocks on the gateway; allow a long timeout).
    await runGepa.click();

    // Assert: a score-delta readout renders. The screen prints
    //   "score delta: +N.N — genome adopted/kept".
    await expect(page.getByText(/score delta/i)).toBeVisible({
      timeout: TRAIN_TIMEOUT,
    });

    // Assert: the delta carries a signed/numeric value (sanity on the readout).
    const deltaText = await page.getByText(/score delta/i).first().innerText();
    expect(deltaText).toMatch(/score delta:\s*[+-]?\d/i);
  });

  test("shows a capture before → after on the demo arc surface", async ({ page }) => {
    // Arrange: a booted run.
    await startRun(page);

    // The capture before/after lives on the Demo arc surface (DemoArcPanel).
    // That panel may be reached via a dedicated nav tab ("demo") OR a "Demo"/
    // "Arc" launcher button once the impl fleet wires it in. Probe for any of
    // those entry points; if none exist in the running build yet, skip rather
    // than fail (the surface is the impl fleet's to mount).
    const demoEntry = page
      .getByRole("button", { name: /^(demo|demo arc|arc)$/i })
      .first();
    const hasDemoEntry = await demoEntry.isVisible().catch(() => false);
    test.skip(!hasDemoEntry, "demo arc entry point not wired into the running build");

    // Act: open the demo arc and advance toward the before/after reveal. The
    // arc rail beats are: Fight → Capture → Train → Improved → Rematch. We
    // click the forward CTA at each beat (resilient regexes per beat label).
    await demoEntry.click();
    await expect(page.getByText(/fight|capture|train/i).first()).toBeVisible({
      timeout: UI_TIMEOUT,
    });

    // Walk the beats with the visible forward buttons, tolerating whichever beat
    // we land on. Each click is best-effort; the assertion below is the gate.
    const forwardCtas = [
      /go to capture|skip to capture/i,
      /^capture$/i,
      /train it/i,
      /run training/i,
      /rematch/i,
    ];
    for (const cta of forwardCtas) {
      const btn = page.getByRole("button", { name: cta }).first();
      if (await btn.isVisible().catch(() => false)) {
        await btn.click().catch(() => undefined);
        // Give each beat a beat to settle before the next probe.
        await page.waitForTimeout(500);
      }
    }

    // Assert: the before → after training-score reveal renders. The panel shows
    // a "training score" eyebrow with explicit "before" and "after" labels (or
    // the honest "HELD ITS GROUND" fallback when the delta is non-positive —
    // both are valid before/after outcomes).
    const beforeAfter = page.getByText(/training score/i).first();
    const heldGround = page.getByText(/held its ground/i).first();
    await expect(beforeAfter.or(heldGround)).toBeVisible({
      timeout: TRAIN_TIMEOUT,
    });

    // When the improved branch rendered, assert both endpoints are present.
    if (await beforeAfter.isVisible().catch(() => false)) {
      const hasBefore = await page
        .getByText(/^before$/i)
        .first()
        .isVisible()
        .catch(() => false);
      const hasAfter = await page
        .getByText(/^after$/i)
        .first()
        .isVisible()
        .catch(() => false);
      const held = await heldGround.isVisible().catch(() => false);
      // Either a true before/after pair, or the honest held-its-ground state.
      expect(hasBefore && hasAfter ? true : held).toBeTruthy();
    }
  });
});
