# WS-F — Training (GEPA + GRPO-HITL), prompt/genome only (no weights, no GPU)

## Goal
Let the player train/evolve party members so they battle better. Two paths, both
optimizing the monster GENOME (`{harness/system_prompt, persona, skill_prompt_fragments,
gambit_rules}`) — never model weights:
- GEPA: offline reflective prompt evolution (general training).
- GRPO-HITL: sample K genome mutations, roll out, human ranks transcripts, adopt the best.

## You OWN
- `apps/api/app/training/genome.py` — read/write a monster's genome dict; `mutate(genome, op)` operators; apply genome back to the Monster row (+bump genome_version, write TrainingArtifact).
- `apps/api/app/training/gepa.py` — `run_gepa(session, monster, rounds)`: try DSPy GEPA; on ImportError/any failure FALL BACK to a hand-rolled reflective loop (sample variants -> self-play score via WS-B `run_self_play` -> LLM critique of transcript -> propose improved prompt -> keep best). Returns new genome + score delta.
- `apps/api/app/training/grpo_hitl.py` — `start_grpo(session, monster)`: sample K=3 mutations, roll each out via `run_self_play`, return a PreferenceBatch (K variants + transcripts + judge scores). `apply_preference(job_id, ranking)`: group-relative advantage (rank + judge score, baseline=group mean) -> adopt top variant's genome; bias the mutation sampler toward winning op types (simple bandit dict). Keep an in-memory job store (dict) keyed by job_id for the hackathon.
- `apps/api/app/routers/training.py` — `POST /api/monsters/{id}/train/gepa` (-> TrainJob), `POST /api/monsters/{id}/train/grpo` (-> PreferenceBatch), `POST /api/training/{job_id}/preference` (PreferenceSubmit), `GET /api/training/{job_id}` (-> TrainJob).
- `apps/web/src/ui/TrainingScreen.tsx` — pick a party member, run GEPA (show score delta), or run a GRPO cycle: show K transcripts, let the user rank them, submit, show the adopted result.

## Interfaces
- Consume: `from app.debate.orchestrator import run_self_play` (WS-B exposes it; if not present yet, define a local minimal self-play that just calls the gateway so you can develop independently, then switch to WS-B's in Wave 2 — note this in your memory file). gateway, models (Monster, TrainingArtifact), schemas (TrainRequest, TrainJob, PreferenceBatch, PreferenceVariant, PreferenceSubmit), get_session.
- Expose: the training endpoints + genome helpers.

## DSPy / GEPA
- DSPy is an OPTIONAL dependency (already in pyproject `[training]` extra) and may NOT be installed in the running container. Your code MUST work without it via the hand-rolled fallback. Guard the import. Treat the exact `dspy.GEPA` API as "verify at runtime"; if anything about it fails, use the fallback. The fallback is the primary deliverable; DSPy is a bonus.

## GRPO-HITL specifics
- K=3-4 variants, 1 rollout each (latency). Advantage = normalize(rank_score + judge_score) - group_mean. Adopt argmax; persist TrainingArtifact(kind='grpo'). Mutation op bandit: dict op->weight, increment winners.

## Definition of done
- `run_gepa` (fallback path) improves or at least returns a valid new genome + delta against the live stack.
- A full GRPO cycle: start -> PreferenceBatch with 3 transcripts -> submit ranking -> genome adopted + TrainingArtifact written.
- `pnpm --filter web build` typechecks.

## Rules
- Do NOT edit frozen shared files (models, redis_state, schemas, enums, main.py) — `training` router auto-mounts. Add Optional schema fields only if essential; note in memory file.
- Do NOT edit pyproject.toml (dspy is already an optional extra). Note any other dep needs in `memories/ws-f-training.md`.
- When done: write `memories/ws-f-training.md` and commit.
