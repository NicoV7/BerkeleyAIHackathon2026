# WS-F — Training (GEPA + GRPO-HITL), prompt/genome only

Lets the player evolve a party member's GENOME (prompts/persona/skill fragments/
gambit rules) so it debates better. No weight updates, no GPU. Two paths: GEPA
(offline reflective) and GRPO-HITL (sample K mutations, human ranks, adopt best).

## Owns
- `apps/api/app/training/genome.py` — genome read/write, `mutate(genome, op)`
  operators, `sample_mutations`, `system_prompt(genome)`, `apply_genome(...)`
  (writes back to Monster row, bumps `genome_version`, writes a `TrainingArtifact`).
- `apps/api/app/training/selfplay.py` — self-play seam (`play(...)`): tries WS-B's
  `run_self_play`, falls back to a local genome-driven 2-debater rollout + judge.
- `apps/api/app/training/gepa.py` — `run_gepa(session, monster, rounds)`: tries
  DSPy GEPA (deferred — see below) then a hand-rolled reflective loop. Returns
  `(new_genome, score_delta)`.
- `apps/api/app/training/grpo_hitl.py` — `start_grpo(...) -> PreferenceBatch`,
  `apply_preference(session, job_id, ranking, monster=...)`, in-memory `_JOBS`
  store + per-monster mutation-op `_BANDIT`.
- `apps/api/app/routers/training.py` — `router` (auto-mounted by main.py):
  POST /api/monsters/{id}/train/gepa (-> TrainJob),
  POST /api/monsters/{id}/train/grpo (-> PreferenceBatch),
  POST /api/training/{job_id}/preference (PreferenceSubmit -> TrainJob),
  GET  /api/training/{job_id} (-> TrainJob).
- `apps/web/src/ui/TrainingScreen.tsx` — default-exported component (NOT yet wired
  into App.tsx). Picks a party member, runs GEPA (shows delta), runs a GRPO cycle
  (shows K transcripts, ↑/↓ rank, submit, shows adopted result).

## Decisions
- **Genome shape**: `{harness, persona, skill_prompt_fragments, gambit_rules, skills}`.
  `skill_prompt_fragments` and `gambit_rules` are stored INSIDE `monster.harness`
  (JSONB) — we do NOT mutate the seeded `monster.skills` catalog. `apply_genome`
  folds fragments+gambits back into `harness` on persist.
- **DSPy**: NOT installed in the container (confirmed `ModuleNotFoundError`). The
  hand-rolled reflective fallback is the live path. `_run_dspy_gepa` deliberately
  raises so we use the fallback (no unverified GEPA wiring shipped). When a
  verified DSPy GEPA integration is ready, replace that function body.
- **GEPA fallback loop**: baseline self-play score -> each round sample 3
  mutations, self-play-score each, keep best; LLM critiques a losing transcript
  into a coaching directive that seeds the next round.
- **GRPO advantage**: `combined = 0.6*rank_score + 0.4*(judge/100)`, baseline =
  group mean; adopt argmax-advantage variant. `score_delta` reported =
  adopted.judge − group-mean judge. Winning op's bandit weight += 1.
- **Frozen-model workaround**: `TrainingArtifact.created_at` model default is
  tz-aware but the column is `TIMESTAMP WITHOUT TIME ZONE` (asyncpg rejects the
  mix). `apply_genome` inserts an explicit naive-UTC `created_at`. No schema edit.
- No new schema fields were needed (reused TrainRequest/TrainJob/PreferenceBatch/
  PreferenceVariant/PreferenceSubmit/Utterance as-is).
- Router/self-play use `model="gemma3:1b"` for dev speed (router const `_MODEL`);
  `selfplay.DEFAULT_MODEL="default"` for production.
- Jobs + bandit are process-local dicts (hackathon scope); GEPA runs
  synchronously inside the POST and records a job in the same `_JOBS` store so
  GET /training/{job_id} works uniformly.

## Interfaces
- **Self-play signature ASSUMED for WS-B** (must match):
  `run_self_play(party_monster, sparring_monster, topic, rounds) -> dict`
  returning `{"transcript": [Utterance-like dicts], "score": float 0..100 (party)}`.
  Sync or async both supported. Transcript dicts: `{turn, actor_id, actor_role
  ('party'|'enemy'|'judge'), skill_used, text, ts}`. Until WS-B lands it, the
  local fallback runs (genomes only, no DB needed).
- Consumes: `app.gateway.gateway`, `app.db.models` (Monster, TrainingArtifact),
  `app.db.session.get_session`, schemas.

## Evidence (verified against live stack, gemma3:1b)
- GEPA via API: status `done`, returned score_delta, 1 `gepa` TrainingArtifact
  written (accepted=t), `genome_version` bumped, harness gained a directive.
- Full GRPO cycle via API: start -> PreferenceBatch (3 variants, 3-turn
  transcripts each, judge scores) -> submit ranking -> status `done`, genome
  adopted, 1 `grpo` TrainingArtifact (accepted=t). genome_version 1->3 total.
- `/api/health` still `ok`. `pnpm/tsc -b` on web typechecks clean.
- NOTE: host venv CANNOT reach Postgres (DB host is Docker-internal `postgres`);
  test via the live API + `docker compose exec postgres psql`, not host python.

## Open TODOs (Wave 2)
- Wire `TrainingScreen` into `App.tsx` (the `screen === "training"` panel).
- Swap to WS-B's `run_self_play` once present (selfplay.py already tries it first;
  just confirm the dict shape above matches).
- Optional: real DSPy GEPA in `_run_dspy_gepa` if `dspy` gets installed
  (pyproject `[training]` extra) — verify `dspy.GEPA` API at runtime.
- Judge on gemma3:1b is flat/harsh (often returns 20) so deltas are ~0 on tiny
  rollouts; bigger model (judge alias) or more rounds gives signal. Adoption
  logic is correct regardless (top variant always adopted).
- `MonsterSummary` schema doesn't expose genome_version/persona; a debug endpoint
  to surface the evolved genome would help the UI show before/after.
