<!-- /autoplan restore point: /Users/nicov/.gstack/projects/NicoV7-BerkeleyAIHackathon2026/feat-redisvl-memory-hotcache-autoplan-restore-20260620-140810.md -->
# Implementation Plan — Live Debate Spectacle (Token Streaming + Scoreboard)

> Seeded from the approved office-hours design doc
> `~/.gstack/projects/NicoV7-BerkeleyAIHackathon2026/nicov-feat-redisvl-memory-hotcache-design-20260620-135854.md`
> (Approach B — real token streaming end-to-end). This plan adds the orchestration
> structure: disjoint workstreams runnable as parallel subagents in waves.

## Context
The AI Debate RPG is a complete vertical slice. The hackathon demo spine is the **live
AI-vs-AI debate spectacle**. Today the debate engine emits each combatant's argument as one
whole paragraph over the WebSocket and scores with a small local model — in a noisy room that
reads as "a wall of text appeared after a pause," not "two AIs are DEBATING." This plan makes
the existing engine *legible as a fight*: words stream token-by-token the instant a model
thinks (killing dead-air latency), HP bars drain live, and a verdict pops on each line.

Constraint: **additive, not a rewrite**. REST `/turn` + `/auto` deltas keep working (game
playable if WS drops). Local-first (Ollama gemma3 on CPU) — per-utterance latency is the
dominant demo risk. Reliability on the demo laptop beats feature count.

## SPINE (re-spined at CEO premise gate — Approach D)
**"AI Debate Arena: a legible fight that ends in a quotable verdict, then you capture the
winner and train it to argue better."** The novel, defensible, already-built loop
(collide → out-argue → **capture → train → measurable improvement**) LEADS the demo. The
judge explaining *why* an argument won makes the AI feel intelligent. Token streaming is
**supporting polish (Wave 2)**, not the thesis. Typewriter-from-whole-utterance stays as
insurance.

## Goal / Definition of Done
- **Judge legibility:** every round, the judge emits a one-line "decisive move / why it won"
  plus 2 named dimensions (Logic, Persuasion) — surfaced over WS and shown per line.
- **Capture/train climax:** the demo can capture a weakened wild debater and run a train →
  re-battle beat that visibly shows the agent improving (before/after score).
- **Fight feels alive:** HP bars animate; the judge's "why" is the hero element; (Wave 2) text
  streams token-by-token with a whole-utterance fallback.
- **Reliability:** REST `/turn`+`/auto` still return full deltas; opening round pre-cached so
  the money shot has ~0 time-to-first-token; finished debate does not reconnect.

> **STATUS: APPROVED via /autoplan** (CEO+Design+Eng, dual voices, all consensus).
> Taste calls locked: **T-D1 = audio sting INTO Wave 1**; **T-E1 = GEPA offline pre-train +
> replay** (live-capped behind a flag for the "it's real" tell).

## PRE-WAVE ASSIGNMENT (CEO, do first — validates contested premise P1)
Run ~5 real debates on the demo laptop and watch cold. If arguments are dull/incoherent on
gemma3-CPU, move the **judge-model upgrade** into Wave 1 (pin `judge` alias to a stronger
model) and/or curate cached "greatest-hits" debates. Don't invest in presentation until the
content clears the bar.

## Orchestration Model (waves of parallel subagents)
Disjoint file ownership per workstream so subagents never conflict. The orchestrator (me)
seeds briefs, dispatches each wave, integrates, and verifies before the next wave.

### Wave 1 — Legible Fight + Capture/Train Climax (3 parallel subagents) ← the demo
- **WS-1 Judge explanation (backend)** — OWNS `apps/api/app/debate/judge.py` + the verdict
  emission path it feeds.
  - `score_round` returns, per utterance: flat `score`, a one-line `why` (the decisive move),
    and 2 sub-scores `logic`/`persuasion`. JSON + json_repair + heuristic ladder already exists;
    extend the rubric/schema minimally. Keep round-level judging (latency).
  - Surface `why` + sub-scores in the existing `verdict` WS event (additive fields). Heuristic
    fallback fills `why` with a templated sentence so the demo never shows a blank explanation.
- **WS-2 Fight legibility (frontend)** — OWNS `apps/web/src/ui/BattleDebateView.tsx`,
  new `apps/web/src/ui/Scoreboard.tsx`.
  - The judge's one-line **"why it won"** is the hero element per line; show Logic/Persuasion
    dims + damage + 💥; animated HP bars (CSS transition on `hp`). Scoreboard: avg/side + ▲/▼
    arrow derived client-side from verdict sign.
- **WS-3 Capture→train climax (frontend flow)** — OWNS a new
  `apps/web/src/ui/DemoArcPanel.tsx` + light additive hooks into existing `PartyScreen.tsx` /
  `TrainingScreen.tsx` (read-only reuse; no overlapping edits with WS-2).
  - Make the capture moment cinematic and surface a **train → re-battle → before/after score**
    beat using the already-built `party/capture.py` + `training/gepa.py`. Show genome_version
    bump + score delta (the "it got better" proof).

### Wave 2 — Streaming Polish + Dead-Air Kill (after Wave 1 integrates & verifies)
- **Token streaming** (the original Approach B work, now polish) — OWNS
  `apps/api/app/debate/orchestrator.py`, `apps/api/app/routers/debate.py`,
  `apps/web/src/ws/useEncounterStream.ts`:
  - Add `_stream_utterance(...)` async-gen: reuse `_build_actor_messages(...)`; iterate
    `gateway.stream()`; sanitize each token via `_sanitize`; accumulate; **first-token
    timeout** (~15–20s). `run_round_stream` emits additive `token` `{turn,actor_id,text}` then
    the canonical `utterance`. Leave `_generate_utterance` + headless `_run_self_play_async`
    untouched. Frontend `token` handler buffers by `(turn,actor_id)`, reconciles on
    `(turn,actor_id,ts)`, empty-buffer-on-utterance → whole-utterance fallback; track `phase`
    ref to stop reconnect on won/lost.
- **Pre-cache opening round** so the demo's first exchange has ~0 time-to-first-token; live
  generation for the "look, it's real" reveal after.
- Token coalescing (~3–5/msg); pre-warm models; demo-reliability checklist.

### Wave 3 — Stretch (only if Wave 1+2 solid)
- Broadcast mode (Approach C): one-line color-commentary between rounds; reaction meter.
- Audio: a single synthesized commentator sting on KO/capture (head-turn in a noisy room).
- Tests: judge schema/fallback, streaming token order/fallback/timeout, capture/train delta.

## Open Questions (carried from design)
- Token granularity: per-token vs coalesced (recommend coalesced).
- Per-line verdict timing: retroactive pop once round verdict arrives (recommended).
- Does `/auto` stream too? Recommend WS path streams both; confirm demo drives WS not REST.
- First-token timeout tuning on CPU gemma3 (~15–20s start).

## Verification
- `pnpm up` → start a debate via WS `/stream`; observe tokens flowing < ~1s, HP animating,
  verdict+damage popping, scoreboard updating.
- Kill token emission (feature-flag) → confirm whole-utterance fallback still renders.
- Confirm REST `/turn` + `/auto` unchanged (game still playable without WS).
- Confirm finished debate does NOT reconnect (phase ref gate).

---

# Phase 1 — CEO Review (autoplan, SELECTIVE EXPANSION)

### 0A. Premise challenge
- **P1 — "bottleneck to 'alive' is rendering, not content."** ⚠️ CONTESTED by both voices.
  On gemma3-CPU the real risks are (a) *argument quality* (streaming a dull argument just makes
  the dullness last longer) and (b) *narrative legibility* (a judge can't tell real streaming
  from faked). Load-bearing and only "confirmed by author." → **premise gate.**
- **P2 — token streaming is additive.** ✅ Valid (eng-confirmed: new `_stream_utterance`, WS
  `token` event; shared helper + headless path untouched).
- **P3 — round-level judging stays, single flat score.** ⚠️ CONTESTED. Both voices: a lone
  number + arrow reads as a *progress bar*, not a debate; the score must explain itself.
- **P4 — "distribution = runs on laptop."** ✅ Valid, but both voices flag the real demo enemy
  is **time-to-first-token** on contended CPU (5–15s), which a 15–20s timeout does not cure —
  it converts dead air into a failure path, not delight.

### 0B. What already exists (leverage map)
- Token streaming: `gateway.stream()` ✅ · WS `{type,data}` envelope ✅ · frontend hook+view ✅.
- **The novel asset is already built:** collide→debate→**capture** (`party/capture.py`),
  XP/evolve (`party/progress.py`), and **train** (`training/gepa.py`, GEPA grounded in real
  battle memories per commit 22f0ddf). The defensible loop exists in code; the demo just doesn't
  surface it.

### 0C. Dream-state delta
CURRENT (working slice, paragraph dumps) → THIS PLAN (streaming spectacle) → 12-MONTH IDEAL
(a game where you *train AI agents that measurably improve*). Both voices: the 12-month-
defensible thing is the **train loop**, not the spectator spectacle.

### 0C-bis. New alternative surfaced by review
**Approach D — "Arena + judge-explanation + capture/train climax":** one-screen arena; player
picks a stance/power-up; two AIs clash; judge explains the *decisive move* in one quotable
sentence; HP changes for a legible reason; climax = capture the winner and show it getting
better. Streaming *supports* this; it is not the thesis.

### 0D. Scope decision (logged)
Re-spining the demo around capture/train + judge-explanation is a **strategy/taste decision**,
not auto-decidable — routed to the premise gate (per autoplan: premises are the one human gate;
a critical finding confirmed by BOTH voices is flagged regardless of bias-toward-action).

### 0E. Temporal interrogation
HOUR 1: build streaming. HOUR 6: if local arguments are still dull/illegible, streaming bought
nothing. → Voices: **validate content quality first** (run 5 cold debates on the demo laptop).

### CEO Dual Voices — Consensus Table
```
  Dimension                            Claude   Codex   Consensus
  ──────────────────────────────────── ──────── ──────── ──────────
  1. Premises valid?                    No(P1)   No(P1)   DISAGREE-w/-plan (P1 weak)
  2. Right problem to solve?            Reframe  Reframe  CONFIRMED → re-spine
  3. Scope calibration correct?         Over-inv Over-inv CONFIRMED → re-prioritize
  4. Alternatives sufficiently explored?No(voice)No(presc)CONFIRMED gap (audio/pre-cache)
  5. Competitive/market risks covered?  Commodity Commodity CONFIRMED (streaming = 2023 toy)
  6. 6-month trajectory sound?          Train=moat Judge=AI CONFIRMED (moat is elsewhere)
```
Both voices independently converged: **streaming is the wrong thing to LEAD with**; the novel,
defensible asset (capture → train-your-own-debater + a judge that explains *why* an argument
won) should be the spine. Keep streaming as polish + the typewriter as insurance.

### NOT in scope (this plan, regardless of gate outcome)
- Weight-based RL / new training algorithms (already exists; do not touch).
- TTS/voice (Wave 3 consideration only).

### CEO Completion Summary
The plan is competently engineered and additive, but the dual voices raise a **critical
strategic finding**: it optimizes an *imperceptible* engineering win (real vs faked streaming)
on a medium (small typewriter text) that is poor for the stated noisy room, while the genuinely
novel asset (capture/train + judge-explanation) is deferred. Decision routed to the premise gate
→ **user chose RE-SPINE (Approach D)**: moat leads, streaming demoted to Wave 2 polish.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Principle | Rationale | Rejected |
|---|-------|----------|-----------|-----------|----------|
| 1 | CEO | Re-spine demo around capture/train + judge-explanation (Approach D) | GATE (user) | Both voices 6/6: streaming is imperceptible + commodity; moat is the train loop | Keep streaming spine |
| 2 | CEO | Demote token streaming to Wave 2 polish | P3 pragmatic | Real-vs-faked streaming unperceivable to judges; keep typewriter fallback | Streaming as Wave 1 spine |
| 3 | CEO | Move judge "why it won" + 2 dims into Wave 1 | P1 completeness | Legibility is the cheapest sell of "they're DEBATING" | Defer to stretch |
| 4 | CEO | Add pre-cache opening round (Wave 2) | P2 boil-lakes | Kills time-to-first-token dead air the timeout can't fix | Rely on timeout only |
| 5 | CEO | Add pre-wave content-quality validation (5 cold debates) | P6 bias-to-action | Validates contested premise P1 in 30 min before investing | Assume content is fine |
| 6 | CEO | Keep weight-based RL / new training algos OUT of scope | P4 DRY | Capture/train already built; reuse, don't rebuild | Expand training scope |
| 7 | Design | Mandate jumbo full-width "ARGUMENT WON BECAUSE" verdict banner as hero | P1 completeness | Both voices: highest-impact change; one hero per frame | Flat per-line verdict text |
| 8 | Design | Add required UI states table (thinking/training/no-improvement/before-after/etc.) | P5 explicit | Critical gap; demo hard-fails in unspecified states | Leave states to implementer |
| 9 | Design | Add non-text 8-ft win/loss signal + min type scale to DoD | P1 completeness | Text-first UI fails the stated noisy room | Text-only legibility |
| 10 | Design | DemoArcPanel = one continuous flow; orchestrator owns transitions | P5 explicit | Arc scattered across 3 surfaces; seams unowned | Screen-swaps across panels |
| 11 | Design | Include stance/power-up picker (WS-2) rather than orphan it | P5 explicit | Mentioned once, unassigned; sells player agency | Leave orphaned |
| 12 | Eng | Re-draw WS-1 = full verdict path (judge+orchestrator emission+schemas+_to_verdict) | P5 explicit | Judge fields can't reach the wire if WS-1 owns judge.py alone | "owns judge.py only" |
| 13 | Eng | New JudgeVerdict fields Optional + add actor_id | P5 explicit | Frozen-contract backcompat + scoreboard needs side | Required fields |
| 14 | Eng | Pull hook hp/phase handling into Wave 1 (WS-2) | P1 completeness | Live-HP DoD depends on it; deferring is an ordering inversion | Leave in Wave 2 |
| 15 | Eng | WS-2 single writer of TS verdict interface; WS-3 embeds BattleDebateView | P5 explicit | Avoid shared-interface collision between subagents | Both edit interface |
| 16 | Eng | Verdict arrow basis = score−50; fix VerdictBadge | P5 explicit | Scores are unsigned 0-100; sign logic always "up" | Sign-based arrow |
| 17 | Eng | Demo determinism: forced/seeded capture + weak-seed baseline genome | P6 bias-to-action | Climax must not be a coin-flip / false "improved" | Live probabilistic |
| 18 | Eng | before/after = GEPA self-play delta, explicitly labeled (not battle score) | P5 explicit | 3 score scales must not be conflated | Conflate scales |
| 19 | Eng | Pull risky-glue tests (parity, capture→train smoke, GEPA budget, hook) into Wave 1 | P1 completeness | Thesis path is the riskiest; can't defer | Defer all tests to Wave 3 |

---

## Cross-Phase Themes
**Theme 1 — Demo determinism & content quality** — flagged in CEO (validate content cold,
pre-cache opening round) AND Eng (sync GEPA blocks, capture can fail, delta often 0).
High-confidence: the demo's biggest risk is non-determinism on a local model, not features.
**Theme 2 — Legibility over motion** — flagged in CEO (commodity spectacle, narrative clarity)
AND Design (text-first UI in a text-hostile room; jumbo verdict banner). High-confidence:
one big "why it won" + a non-text win signal beats animated text.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/autoplan` | Scope & strategy | 1 | ✅ clean | Re-spine to capture/train + judge-why (6/6 voices) |
| Eng Review | `/autoplan` | Architecture & tests | 1 | ✅ clean | Wave 1 re-drawn disjoint; 19 issues auto-fixed; tests pulled fwd |
| Design Review | `/autoplan` | UI/UX gaps | 1 | ✅ clean | Jumbo verdict banner, UI-states, non-text 8-ft signal, owned arc |
| Codex Review | `/autoplan` voices | Independent 2nd opinion | 3 | ✅ ran | CEO+Design+Eng, all consensus, 0 disagreements |

**VERDICT:** APPROVED via /autoplan — SELECTIVE_EXPANSION. Re-spined at premise gate; all dual
voices reached consensus. Ready to orchestrate Wave 1 (3 disjoint subagents). Restore point +
test plan artifact in `~/.gstack/projects/NicoV7-BerkeleyAIHackathon2026/`.

---

# Phase 2 — Design Review (autoplan)

### Design Litmus Scorecard (dual voices)
```
  Dimension                 Claude  Codex   Consensus
  ────────────────────────── ─────── ─────── ──────────
  1. Information hierarchy    4/10    weak    CONFIRMED — heroes named, not ranked
  2. Missing user states      3/10    under   CONFIRMED — critical gap
  3. Emotional arc legibility 5/10    scatter CONFIRMED — split across 3 surfaces, seams unowned
  4. Specificity              4/10    ambig   CONFIRMED — backend-precise, design-vague
  5. Readability @ 8 ft       3/10    text!   CONFIRMED — text-first UI in a text-hostile room
```
Litmus: *"the plan permits a legible demo; it does not force one."* Both voices independently.

### Auto-fixes folded into the plan (structural — P5/P1)
1. **Jumbo verdict banner is the mandated hero.** After every exchange, a full-width
   `ARGUMENT WON BECAUSE: <one sentence>` banner — largest type on screen, one hero per frame.
   Logic/Persuasion chips + damage + HP are secondary. (Highest-impact change; both voices.)
2. **Non-text win/loss signal (8-ft DoD).** Win/loss + magnitude must read at distance via a
   NON-text cue (color flash / HP slam / screen-side glow) *before* anyone parses the sentence.
   Add min type scale + one-hero-per-frame rule to DoD.
3. **UI states table is now required (WS owners must implement each):**
   | State | Required UI |
   |-------|-------------|
   | Pre-battle / empty arena | combatant cards + topic, "Begin" affordance |
   | Model thinking | "thinking" affordance within 200ms (pulsing avatar / dots) — never a frozen blank |
   | First-token wait (5–15s CPU) | thinking affordance + pre-cached opening round so the money shot is ~0s |
   | Training in progress | narrated/determinate bar ("optimizing… candidate 2/4") |
   | No improvement / delta ≤ 0 | honest "held its ground" state OR rig before-battle with a deliberately weak genome so delta is reliably positive (decide in pre-wave validation) |
   | Before/after | oversized `12 → 31` delta, directional motion, nothing else animates |
   | Rematch result | second battle visibly marked "trained version" |
   | Capture failed | explicit beat (capture can fail) |
   | WS drop / finished | no reconnect on won/lost; clean end card |
4. **Arc choreography is owned, not scattered.** `DemoArcPanel` becomes a **single continuous
   flow** with explicit beat markers `1 Fight → 2 Capture → 3 Train → 4 Rematch → Improved`;
   the orchestrator owns the inter-beat transitions (collide→fight→capture→train→rematch).
   Mandate continuity (state changes on one stage), not screen-swaps.
5. **Stance/power-up picker (from Approach D 0C-bis):** assign to WS-2 as a pre-exchange choice,
   or cut — do not leave orphaned. (Default: include as a small pre-round chooser; it sells
   player agency.)

### Design Taste Decisions (→ final gate)
- **T-D1: Promote one audio sting (KO/capture) into Wave 1?** Both voices: audio is the only
  true head-turn in a noisy room, currently exiled to Wave 3. Recommend **yes** (browser
  `SpeechSynthesis`/a single cached sting, ~1hr) — but it's a risk/scope taste call.

---

# Phase 3 — Eng Review (autoplan, FULL_REVIEW)

### Scope challenge / what already exists
- judge.py `score_round` already returns `score`+`rationale` with a json_repair+heuristic ladder
  — adding `why`/`logic`/`persuasion` is a small additive rubric+dataclass change.
- BUT the verdict is hand-rebuilt downstream: `_apply_round_damage` (orchestrator.py:298) drops
  to `score`+`rationale`, and `run_round_stream` rebuilds `verdict_payload` (orchestrator.py:402).
  `JudgeVerdict` (schemas.py:99) is a **frozen contract with required fields and no `actor_id`**.
  → New fields must be **Optional** on the schema, threaded through BOTH emission sites, and
  mapped in `_to_verdict` (debate.py:195) for REST parity. WS-1 "owns judge.py" is insufficient.
- Capture/train EXIST but are **not a wired flow**: capture returns `CaptureResult`; GEPA
  (`training.py:46`) runs **synchronously/blocking, minutes-long on CPU, emits no progress**;
  there is **no rematch endpoint** (rematch = new encounter). Three different score scales:
  capture (none), GEPA self-play `score_delta`, live `JudgeVerdict.score` (0-100).

### Eng Dual Voices — Consensus Table
```
  Dimension                  Claude    Codex     Consensus
  ────────────────────────── ───────── ───────── ──────────
  1. Architecture sound?      No(F1/F8) No(#1/#7) CONFIRMED — verdict path + ownership wrong
  2. Test coverage sufficient?No(F-test)No(gaps)  CONFIRMED — pull glue test into Wave 1
  3. Performance risks?       GEPA crit GEPA crit CONFIRMED — sync GEPA blocks demo
  4. Security threats?        none new  none new  CONFIRMED — additive, low surface
  5. Error paths handled?     No(F5/F7) No(#5/#6) CONFIRMED — no-improve + capture-fail
  6. Deployment risk?         demo det. demo det. CONFIRMED — needs demo determinism
```

### Architecture (corrected) — verdict path + climax flow
```
JUDGE/VERDICT PATH (WS-1, backend, single writer of verdict contract):
  judge.score_round ──(why,logic,persuasion,score)──> _apply_round_damage (orchestrator)
        │                                                   │ widen scored tuple
        └── heuristic fills `why` on fallback               ▼
                              run_round_stream → verdict_payload {+actor_id,+why,+dims}
                                   │                         │
                            WS `verdict` event         REST _to_verdict (debate.py)
                                   │                         │  JudgeVerdict (Optional fields)
                                   ▼                         ▼
                          useEncounterStream  ◄── single TS-interface writer = WS-2

CLIMAX FLOW (WS-3): capture(forced, low HP) → captured monster id
   → train/gepa (CAPPED rounds=1/variants=1 OR offline replay) → genome_version++ + score_delta
   → rematch = NEW encounter w/ trained monster → DemoArcPanel embeds <BattleDebateView/>
   → before/after = GEPA self-play delta (LABELED "training score", NOT conflated w/ battle score)
   NOTE: after battle, Redis evicted on _finalize (debate.py:83) → read "after" from PG Encounter.
```

### Eng auto-fixes (re-drawn ownership — now genuinely disjoint) — P5/P3
- **WS-1 = full verdict path (backend only):** judge.py + the two orchestrator emission sites +
  `JudgeVerdict` Optional fields incl. `actor_id` in schemas.py + `_to_verdict` REST mapping.
  Single writer of the verdict contract. (Fixes F1/F2/F8, Codex #1/#2.)
- **WS-2 = frontend + single writer of the WS TS interface:** `useEncounterStream.ts` (add
  `hp`/`phase` handlers — **pulled from Wave 2**, required for live HP; add verdict fields;
  phase ref), `BattleDebateView.tsx` (jumbo banner; fix `VerdictBadge` to use **score−50**, not
  `score>=0`), `Scoreboard.tsx` (arrow basis = score−50). (Fixes F3/F9/F10, Codex #8/#9.)
- **WS-3 = climax (owns DemoArcPanel + demo-determinism backend):** `DemoArcPanel.tsx` embeds
  `<BattleDebateView/>` (no TS-interface edits); a **demo-mode**: forced/seeded capture
  (capture.py flag), **weak-seed baseline genome** so delta is reliably > 0, GEPA capped or
  replayed. Reads "after" transcript from Postgres. (Fixes F4/F5/F6/F7, Codex #3/#4/#5/#6.)
- **Tests pulled into Wave 1** (not Wave 3): verdict contract parity + backcompat, judge
  additive fields, capture→train→rematch smoke, GEPA demo-budget, hook hp/phase. (See test plan
  artifact: `~/.gstack/projects/.../nicov-...-test-plan-20260620-140810.md`.)

### Failure Modes Registry
| Failure | Trigger | Mitigation | Critical? |
|---------|---------|------------|-----------|
| GEPA blocks demo (minutes, no progress) | live GEPA on CPU | cap rounds=1/variants=1 OR offline pre-train+replay; progress affordance | **YES** |
| "It improved" is false (delta=0) | no variant beats baseline | weak-seed baseline genome; honest "held its ground" state | **YES** |
| Capture fails at climax | 5% roll at low HP | demo-mode forced capture / seeded RNG | med |
| New verdict fields break REST/old JSON | required schema fields | Optional fields + `_to_verdict` + parity test | **YES** |
| Live HP never animates | hook ignores `hp`/`phase` | WS-2 adds handlers (pulled into Wave 1) | high |
| Scoreboard always "up" | arrow from unsigned score | arrow basis = score−50 | med |
| Subagent edit collisions | shared orchestrator/TS interface | single-writer ownership above | high |

### Eng Taste Decision (→ final gate)
- **T-E1: GEPA demo strategy?** (A) **Offline pre-train + replay** the artifact — most reliable,
  ~0 wait, but the training step is "canned"; (B) **Cap live GEPA** (rounds=1, variants=1) —
  genuinely live but ~1–2 min wait + needs a progress affordance; (C) background job + polling —
  most work. Recommend **A** for demo reliability (keep B behind a flag for the "it's real" tell).

### Eng Completion Summary
Wave 1 as originally drawn was not safely parallelizable — the judge DoD needed files the plan
left unowned/deferred, the climax was three unwired subsystems with a blocking GEPA call and a
non-deterministic payoff, and the live-HP DoD depended on a deferred hook change. Re-drawn into
three genuinely disjoint workstreams (verdict-path backend / frontend+TS-interface / climax+demo-
determinism), with risky-glue tests pulled into Wave 1 and a demo-determinism strategy required.
