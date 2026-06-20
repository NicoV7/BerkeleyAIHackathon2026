# AI Debate RPG — Build Plan (Orchestrated, Wave-Based)

## Context
We're building an **AI RPG roguelike for the Berkeley AI Hackathon 2026**: a Pokémon-style game where the player traverses a tile overworld, collides with wandering enemies, and enters **debate encounters** against 1–3 enemy LLM agents arguing a user-chosen topic. The player has a **party of AI "monster" agents** (random persona / type / skills / harness) that are **not directly controlled** — instead the player authors **Final Fantasy 12 "Gambit"** behavior rules and **trains/evolves** them so they battle autonomously. Training is the forced interaction with the RL system. Everything must run **local-first via Docker**, defaulting to a **local model (Gemma 3 / Qwen via Ollama)** through a gateway, with Claude/OpenAI pluggable on top.

I (Claude) act as **orchestrator**: land a foundation wave, then spawn parallel subagents (one per workstream, isolated in git worktrees) to ship the six systems concurrently, then integrate and run an end-to-end demo.

**Outcome:** a playable demo — new run with a topic → walk the map → collide → autonomous gambit-driven debate with judge/HP → capture a defeated wild enemy → train a party member (GEPA + one GRPO-HITL preference cycle) → re-battle and see measurable improvement.

## Decisions (locked with user)
- **Frontend:** Vite + React (SPA, no Next.js — it's a game, not content). **Phaser 3** owns *only* the overworld canvas; all battle/party/gambit/training UI is plain React + Tailwind + shadcn/ui. Zustand state, TanStack Query, WebSocket for live debate.
- **Backend:** Python **FastAPI** (async + WebSocket). REST `/turn` + `/auto` return full deltas so the game is playable even if WS drops.
- **Model gateway:** wrap **LiteLLM** behind our own `LLMGateway` interface (swappable). Default `ollama/gemma3` (and `qwen`), with `anthropic/*` and `openai/*` pluggable. Judge agent can be pinned to Claude for quality while debaters stay local.
- **RL:** prompt/harness-only, **no weight updates, no GPU**. GEPA via **DSPy** (with a hand-rolled reflective fallback) for general training; a **GRPO-style human-in-the-loop preference loop** (group of genome mutations → rollouts → human ranking → adopt top variant) for battle performance.
- **Data:** Postgres + **pgvector** (durable state + memory/RAG), **Redis** (live encounter conversation + HP), SQLModel + Alembic.
- **Contracts:** Pydantic → OpenAPI → `openapi-typescript` into `packages/shared/types.gen.ts` (single source of truth).

> Library APIs to **verify at build time** (knowledge cutoff Jan 2026): LiteLLM router + Ollama provider strings; DSPy GEPA optimizer name/`compile()` signature; `instructor`/JSON-mode for small-model structured output; embedding model availability (`nomic-embed-text` / fastembed). Each has a documented fallback below so no single API drift blocks the demo.

## Monorepo Layout
```
apps/web/    Vite+React+Phaser  (game/, ui/, state/, api/, ws/, public/assets/)
apps/api/    FastAPI            (gateway/, debate/, memory/, training/, party/, routers/, db/, redis_state.py)
packages/shared/  types.gen.ts (OpenAPI→TS), enums.ts (TYPES + SKILLS catalogs)
infra/       docker-compose.yml, postgres/init.sql (CREATE EXTENSION vector), ollama/pull-models.sh
prompts/     plan files (this plan is copied here as prompts/ok-claude-use-toasty-pascal.md); per-workstream subagent briefs go here too
memories/    in-repo agent memory log — each subagent appends what it built/decided so later waves & sessions can resume
```
**`prompts/`** is the canonical home for all plan files and the per-workstream subagent briefs I hand out in Wave 1 (one markdown brief per WS-A…WS-F). **`memories/`** is an in-repo, version-controlled memory store: every subagent writes a short `memories/<workstream>.md` logging files it owns, key decisions, interfaces it exposed/consumed, and open TODOs — so integration (Wave 2) and any future session can pick up without re-deriving context. Both folders are created and committed in Wave 0 with a README explaining the convention.
Compose services: `web` (5173), `api` (8000), `ollama` (11434), `postgres`/pgvector (5432), `redis` (6379), with healthchecks.

## Data Model (summary)
- **Postgres:** `runs`, `monsters` (party + wild enemies: persona jsonb, type enum, level/xp/hp, harness jsonb, skills, genome_version), `skills` (move catalog), `gambit_rules` (priority + condition DSL + action), `memories` (event_type BATTLE|PLAYER|CHARACTER, content, summary, `embedding vector(768)`, `keywords tsvector`, salience — ivfflat + GIN indexes), `encounters`, `training_artifacts`.
- **Redis (per `encounter_id`):** `:meta` hash, `:transcript` list (the shared conversation every agent sees), `:hp` hash, `:queue` turn order, `:judge` verdicts, `:momentum`. TTL ~2h; snapshot to PG on completion.
- **Genome (training target):** `{harness/system_prompt, persona, skill_prompt_fragments, gambit_rules}` — the only thing GEPA/GRPO mutate. Versioned via `genome_version` + `training_artifacts`.

## API Contract (key endpoints)
- **Map/run:** `POST /api/runs {topic}`, `GET /api/runs/{id}/map`, `POST /api/runs/{id}/move {dx,dy}` → `{encounter_id}` on collision.
- **Encounter/debate:** `POST /api/encounters`, `WS /api/encounters/{id}/stream`, `POST /api/encounters/{id}/turn` (advance one round), `POST /api/encounters/{id}/auto {rounds}` (autonomous via gambits), `POST /api/encounters/{id}/capture`, `/flee`.
- **Party/gambits:** `GET /api/runs/{id}/party`, `GET/PATCH /api/monsters/{id}`, `GET/PUT /api/monsters/{id}/gambits`, `GET /api/monsters/{id}/memories?q=&type=`.
- **Training:** `POST /api/monsters/{id}/train/gepa {rounds}` → job; `POST /api/monsters/{id}/train/grpo` → preference batch; `POST /api/training/{job_id}/preference {ranking}`; `GET /api/training/{job_id}`.

## Debate Turn Loop
Round = ordered turns over all combatants (initiative from level + type). Per actor: (1) **build context** = shared Redis transcript (windowed/summarized if long) + topic/side + **hybrid-RAG memory injection** (vector `<=>` + `tsvector`, RRF-merged top-k) + harness/persona/skills; (2) **party gambits evaluate** by priority against live `BattleState` (hp%, ally/enemy state, last verdict, momentum, turn, topic keywords) → first match constrains the prompt (force skill/target/tone) — *this is the forced RL interaction*; (3) **generate** via gateway, stream to WS; (4) **append** to Redis transcript so the next actor sees it. After the round, **Judge** agent scores each utterance (persuasiveness/logic/relevance/rebuttal → 0–100 + rationale, JSON with repair retry). **Damage** = `clamp(score-50) × type_chart × skill_power × momentum × level_scale`; apply to Redis HP. Side at 0 HP → win/loss + XP. Wild HP < threshold → push `capturable` over WS → `/capture` (prob scales with low HP). Notable events → `memory.write_event` (summarize+embed+insert) for future RAG.

## GEPA + GRPO-HITL (no weights)
- **GEPA (offline/auto):** wrap monster behavior as a DSPy module; metric = N self-play debates scored by the Judge (net HP diff / win rate); `dspy.GEPA` reflective optimization → new genome → `training_artifacts`. Fallback: hand-rolled sample→judge→LLM-critique→mutate→keep-best loop.
- **GRPO-HITL (battle perf):** sample K=3–4 genome **mutations** (gambit/skill-fragment/tone perturbations) → roll out each in an identical scenario → human **ranks** transcripts (`/preference`) → group-relative advantage (rank + judge score, baseline = group mean) → adopt top variant + bias the mutation sampler toward winning edit types (bandit over operators). `numpy` only, no TRL/GPU.

## Orchestration — Waves & Subagents
**Wave 0 — Foundation (I land this first, blocks everyone).** docker-compose + init.sql + pull-models.sh + `.env.example`; `apps/api` `main.py`/`config.py`/`db/session.py`/**`db/models.py` (all tables)**/`redis_state.py`; `gateway/` (LLMGateway + Ollama/Anthropic/OpenAI adapters + `/health`); `packages/shared/enums.ts` + OpenAPI→TS script; `apps/web` skeleton; **`prompts/` and `memories/` folders** (each with a `README.md` defining the convention, this plan copied into `prompts/`, and a `memories/MEMORY.md` index); **all Pydantic schemas stubbed (contract freeze).** *Exit criteria:* `docker compose up` brings up 5 services, `/health` returns gateway+db+redis OK, a smoke call reaches Ollama through the gateway, TS types generate.

**Wave 1 — six parallel subagents (each in its own worktree, disjoint file ownership):**
- **WS-A Overworld:** `apps/web/src/game/**`, `public/assets/**`, `routers/map.py`, `party/generator.py`.
- **WS-B Debate engine:** `debate/orchestrator.py`, `judge.py`, `damage.py`, `routers/debate.py` (+WS), `routers/encounter.py`.
- **WS-C Gambits + battle UI:** `debate/gambits.py`, `ui/GambitEditor.tsx`, `ui/BattleDebateView.tsx`, `ws/**`, gambit CRUD in `routers/party.py`.
- **WS-D Memory/RAG:** `memory/embeddings.py`, `store.py`, `retriever.py`, pgvector migration, `routers/memory.py`.
- **WS-E Party/capture/progression:** `party/capture.py`, leveling/XP, `routers/capture.py`, `ui/PartyScreen.tsx`.
- **WS-F Training:** `training/genome.py`, `gepa.py`, `grpo_hitl.py`, `routers/training.py`, `ui/TrainingScreen.tsx`.

Conflict avoidance: only shared files (`db/models.py`, `redis_state.py`, `shared/enums.ts`, router-mount list) are frozen in Wave 0; each WS adds its own router file; integrator wires mounts in Wave 2. B exposes interface stubs that C (gambits), D (RAG), F (self-play) fill.

**Wave 2 — Integration/E2E (I do this solo).** Mount all routers; resolve B↔C/B↔D/B↔F stubs into real calls; seed skills catalog + type chart + Tiled map; small-model prompt tuning; latency passes (`asyncio.gather` enemy gens, round-level judging, transcript windowing, token streaming); run the full demo path.

## Top Risks → De-risk
1. **Small-model judge/JSON quality** (highest): short rubrics + one-shot + JSON-only + `json-repair` retry + heuristic fallback score; keep Claude pluggable for the judge specifically.
2. **DSPy/GEPA drift:** ship hand-rolled reflective loop first; GEPA behind a flag.
3. **GRPO over-scoping:** lock to prompt/gambit bandit, K=3, 1 rollout, day 1.
4. **Phaser↔React friction:** Phaser = overworld only; CSS-grid fallback if it eats >½ day.
5. **CPU Ollama latency:** parallelize gens, round-level judging, windowing, smallest viable model, stream tokens.
6. **Redis↔PG consistency:** Redis is source of truth during an encounter; single idempotent write-back on end.
7. **Merge conflicts:** Wave 0 contract freeze + strict directory ownership + append-only router mounting.

## Verification
- `docker compose up` → all 5 services healthy; `GET /api/health` OK; gateway smoke test returns text from Ollama.
- API contract tests per router (pytest + httpx) hitting `runs → move → encounters → turn/auto → capture → train`.
- E2E demo script: create run with a topic → traverse → collide → autonomous debate (watch WS log, HP, judge verdicts) → capture a weakened wild enemy → run GEPA train + one GRPO preference cycle on a party member → re-battle same enemy and observe higher win/score. Verify memory inspector shows new BATTLE/CHARACTER events being retrieved into context.
- Frontend: `pnpm dev` (or via compose) → overworld walkable, encounter UI streams, gambit editor persists rules, training screen completes a preference loop.

## Skill Audit (plan-mode requirement)
No existing project skill covers "wave-based multi-agent orchestration of a local-first FastAPI + Phaser game with a local-model gateway and prompt-only RL." Proposed new skill after Wave 0 lands: **`debate-rpg-orchestration`** — codifies the worktree-per-workstream pattern, the Wave 0 contract-freeze rule, the `LLMGateway` local-first interface, and the genome/gambit conventions so future sessions can resume or extend any workstream without re-deriving boundaries. (Deferred to implementation; flagged here per the audit step.)
