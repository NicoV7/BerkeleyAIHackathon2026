# WS-B — Debate Engine (turn-based core)

## Owns
- `apps/api/app/debate/damage.py` — type chart (mirrors enums.ts) + `compute_damage(...)`.
- `apps/api/app/debate/judge.py` — round-level judge (`score_round`), JSON + json_repair + heuristic ladder.
- `apps/api/app/debate/orchestrator.py` — turn loop (`run_round_stream`), `Combatant`/`Event`, `run_self_play`.
- `apps/api/app/routers/encounter.py` — `POST/GET /api/encounters` (+ shared Redis<->state helpers reused by debate router).
- `apps/api/app/routers/debate.py` — `POST .../turn`, `.../auto`, `.../flee`, `WS .../stream`.
- `apps/api/scripts/verify_ws_b.py` — host verification script.

## What works (verified against live stack, gemma3:1b)
- Create encounter -> seeds Redis (meta/hp/queue/momentum) + Encounter row. Pulls
  party from Monster table, enemy from WS-A `generate_wild` (live; n=1, or n=3 if
  enemy_group_id). Falls back to a fabricated player+wild monster if absent.
- `/turn`: one round = sequential utterances (each sees prior via Redis) -> one
  judge call -> damage -> HP -> momentum -> phase. Real LLM text + model-driven
  judge scores observed; heuristic kicks in only when the model output is unusable.
- `/auto N`: loops rounds until a side hits 0 HP. Observed phase=won (enemy to 0).
- `WS /stream`: emits `state -> utterance* -> verdict* -> hp -> phase -> round_done`.
  Verified full sequence on a fresh encounter. Refuses to advance won/lost battles.
- `/flee`: marks lost + finalizes EncounterResult.flee (idempotent).
- `run_self_play(party, sparring, topic, rounds)` returns transcript+verdicts+net_score
  headlessly (no Redis/WS). Accepts Monster ORM rows OR plain dicts.
- `/api/health` still ok.

## Interfaces for other workstreams

### choose_action (WS-C gambits) — battle_state dict keys passed:
`from app.debate.gambits import choose_action`; called as
`choose_action(monster_dict, battle_state)` for PARTY actors only, wrapped in
try/except. Expected return: an action dict; we read `behavior`, `skill`,
`target`, `tone` (all optional). Default action: `{"behavior": "argue your
strongest point", "skill": None, "target": None, "tone": None}`.
`monster_dict` = {id, name, type, level, skills, persona, harness}.
`battle_state` keys (STABLE CONTRACT):
- `hp`: dict monster_id -> int
- `max_hp`: dict monster_id -> int
- `last_verdict_score`: float
- `turn_no`: int
- `topic`: str
- `momentum`: dict side("party"|"enemy") -> float
- `self_id`: str
- `ally_ids`: list[str]
- `enemy_ids`: list[str]

### retrieve (WS-D RAG) — call signature used:
`from app.memory.retriever import retrieve`; called as
`retrieve(monster_id, topic, run_id=run_id, k=3)`, awaited if coroutine, wrapped
in try/except (returns [] on absence/error). Accepts return items as `str`,
or dict with `summary`/`content`, or objects with `.summary`. Injected into the
actor system prompt as `What you remember: ...`. (Live `/turn` path only;
`run_self_play` skips RAG for determinism.) NOTE: WS-D's HTTP route is
`/api/monsters/{id}/memories?q=&k=` — if the python `retrieve` symbol differs,
adjust this call; it's behind a defensive import so a mismatch just yields [].

### run_self_play (WS-F) — signature:
`run_self_play(party_monster, sparring_monster, topic: str, rounds: int = 3) -> dict`.
Sync wrapper (uses a worker thread if already inside an event loop). Returns:
`{topic, transcript[utt dicts], verdicts[dicts], party_id, sparring_id, party_hp,
sparring_hp, party_avg_score, sparring_avg_score, net_score, result, rounds_played}`.
`net_score` = party_avg_score - sparring_avg_score.

## Decisions
- Damage = clamp(score-50,0,50) * type_mult * skill_mult * momentum * level_scale,
  rounded; only score>50 deals damage. level_scale = 1+0.05*(lvl_atk-lvl_def),
  clamped 0.5..2.0. Multi-enemy: damage split across living defenders.
- Momentum per side nudged by round net score swing, clamped 0.7..1.3.
- Judge is ROUND-LEVEL (one call/round) for latency. Resilience ladder:
  primary "judge" alias model -> fallback_model (combatants' own model, passed by
  orchestrator) -> deterministic heuristic. Positional fallback maps results by
  order if a small model echoes example/ wrong keys.
- Redis is source of truth during the battle; meta hash stores a serialized
  combatant roster ("combatants" field) so the engine rehydrates between calls.
  Finalize writes result + transcript_ref to Postgres once (idempotent).
- Capturable: wild enemy alive and hp <= 25% max -> phase "capturable" +
  capturable_ids in TurnResult / phase event (WS-E consumes).
- Initiative order = level desc, party-first tiebreak, then name.

## Frozen-contract gaps (NOT edited; worked around)
- **created_at tz mismatch**: db.models defaults `created_at` to tz-AWARE
  (`datetime.now(timezone.utc)`) but columns are `TIMESTAMP WITHOUT TIME ZONE`
  (init.sql). asyncpg rejects tz-aware values. We strip tzinfo via
  `_naive_created_at()` before every insert in encounter.py. RECOMMEND foundation
  make columns timestamptz OR default naive `datetime.utcnow()`. (WS-A's
  generator already does `datetime.utcnow()` to dodge this.)
- DB enum labels are the enum *names* (lowercase: `logos`, `player`), not the
  uppercase *values*. ORM handles this; only matters for raw SQL.
- `get_session` yields a plain SQLAlchemy `AsyncSession` (no `.exec()`); use
  `.execute()` + `.scalars()`.
- No Optional schema fields were added — schemas.py untouched.

## Deps
- None new. Uses existing `json_repair`, `httpx`, `redis`, `sqlalchemy`.
- Verify script uses `websockets` (already in venv) for the WS test.

## Open TODOs (Wave 2)
- Pin judge to a stronger model (alias "judge" -> claude/gpt) for sharper scoring;
  set env `JUDGE_MODEL` or update gateway registry. gemma3:4b alias not pulled here.
- Stream tokens (not just whole utterances) over WS for typewriter UX — gateway
  has `stream()`; current live path uses `complete()` per utterance.
- Transcript summarization when long (currently a fixed last-16 window).
- Per-skill `skill_mult` from the Skill table (currently hardcoded 1.0).
- Multi-party (>1 player monster) targeting UX; engine already supports N-vs-M.
- Snapshot full transcript JSON to durable storage (currently transcript_ref =
  the redis key string, which TTLs after 2h).
