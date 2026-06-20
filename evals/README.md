# Evals — "Does the demo actually work?"

This directory holds the **executable demo eval**: a Playwright-driven harness
where an agent *plays the whole game* — new run → walk the overworld → trigger a
debate → run `/auto` → reach a win → capture the weakened wild → train it →
rematch — and **asserts the spine outcome at every beat**. It is the
single test that proves the demo is real, end to end.

- **Spec:** [`apps/web/e2e/eval-playthrough.spec.ts`](../apps/web/e2e/eval-playthrough.spec.ts)
- **Screenshots:** written to [`evals/shots/`](./shots) — one PNG per beat.

It runs against the **live stack** (Vite dev server + FastAPI + Postgres + Redis
+ model gateway). When the stack is **not** reachable, the suite *skips* (it does
not fail), so `playwright test --list` always passes on a host where the stack is
mid-edit or down.

---

## What it asserts (the demo spine)

| Beat | Action | Spine assertion |
|-----:|--------|-----------------|
| 0 | New run | Topic entry → **Start Run** → in-run nav (`overworld … demo`) renders |
| 1 | Walk | Phaser overworld canvas accepts movement; agent sweeps until a wild collision routes to the encounter (falls back to the Demo arc's embedded fight if the procedural map has no reachable enemy) |
| 2 | Debate + `/auto` | BattleDebateView renders; running `Auto (3)` lands a judge verdict — **the jumbo banner + verdict `why` headline is present** |
| 3 | Reach a win | Agent advances until **a side's HP reaches 0** (or the phase resolves to `won` / `capturable`, i.e. HP fell into the capture window) |
| 4 | Capture | On the Demo arc, capture the weakened wild — **capture succeeds in demo mode** (`CAPTURED`) |
| 5 | Train | Run GEPA on the captured monster — **a training-score delta is rendered** (before → after, or the honest `HELD ITS GROUND` Δ readout) |
| 6 | Rematch | Rematch with the trained version — **the `TRAINED VERSION` surface + a fresh battle view mount** |

Each beat writes a screenshot to `evals/shots/`:

```
beat0-new-run.png
beat1-overworld.png
beat1b-demo-arc-fight.png
beat2-verdict-why.png
beat3-win.png
beat4-capture-prompt.png
beat4-captured.png
beat5-train-prompt.png
beat5-training-delta.png
beat6-rematch-cta.png
beat6-rematch.png
```

---

## Prerequisites

1. **Install web deps + the Playwright browser** (once):

   ```bash
   cd apps/web
   pnpm install
   pnpm exec playwright install chromium
   ```

2. **Bring up the live stack.** The eval drives the UI through the real API, so
   you need the dev server *and* the backend running.

   - Backend + infra (Postgres / Redis / gateway), from the repo root:

     ```bash
     pnpm up          # docker compose -f infra/docker-compose.yml up -d --build
     ```

   - Web dev server (Vite proxies `/api` to the backend), from the repo root:

     ```bash
     pnpm dev:web     # serves http://localhost:5173
     ```

3. **Demo determinism (important for the capture beat).** The capture roll is
   probabilistic. For a demo that cannot fail on a 5% miss, run the API with the
   force-capture flag set (the capturable HP-window gate still applies — the
   wild must actually be weakened first):

   ```bash
   DEMO_FORCE_CAPTURE=1   # set in the API's environment (infra/.env or compose)
   ```

   Without it the capture beat can occasionally fail on an unlucky roll; the rest
   of the spine is unaffected.

---

## Running the eval

From `apps/web`, with the stack up at `http://localhost:5173`:

```bash
cd apps/web
pnpm e2e -- eval-playthrough.spec.ts
```

Or invoke Playwright directly (e.g. headed, to watch the agent play):

```bash
cd apps/web
pnpm exec playwright test e2e/eval-playthrough.spec.ts --headed
```

### Collect / list only (no running stack required)

This always passes on the host — it proves the spec compiles and is discoverable
even while the stack is mid-edit:

```bash
cd apps/web
pnpm exec playwright test --list e2e/eval-playthrough.spec.ts
```

### Pointing at a different host

Override the base URL (useful for a deployed preview):

```bash
EVAL_BASE_URL=https://my-preview.example.com \
  pnpm exec playwright test e2e/eval-playthrough.spec.ts
```

(`PLAYWRIGHT_BASE_URL` is also honored, for parity with the other e2e specs.)

---

## Interpreting results

- **Green:** the demo spine works end to end. Inspect `evals/shots/` for the
  per-beat screenshots — `beat2-verdict-why.png` (the "why" headline),
  `beat4-captured.png`, `beat5-training-delta.png`, and `beat6-rematch.png` are
  the money shots for a demo deck.
- **Skipped (whole suite):** the dev server was not reachable at the base URL.
  Bring the stack up (see Prerequisites) and re-run.
- **Failed at a beat:** the step name in the Playwright output names the broken
  beat (e.g. `beat 5 · run GEPA training and render a score delta`). The
  screenshot captured *up to* that beat plus Playwright's own failure trace
  (`playwright-report/`) localize the break. The capture beat failing on an
  occasional roll usually means `DEMO_FORCE_CAPTURE` is not set on the API.

---

## Timeouts

GEPA self-play and the local CPU debate model are slow. The eval uses generous
budgets (up to ~4 min for the win beat and ~4 min for training) and a per-test
cap that sums them. On faster gateways it finishes well under that.
