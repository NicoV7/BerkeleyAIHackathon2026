# WS-B — Debate Engine (the core wow-moment)

## Goal
The turn-based debate battle: 1-3 enemy agents + the player's party debate the run's
topic. The shared conversation is cached in Redis (every agent sees everything). A
Judge agent scores arguments; damage applies to HP; a side at 0 HP wins/loses. Live
updates stream over WebSocket.

## You OWN
- `apps/api/app/debate/orchestrator.py` — the turn loop
- `apps/api/app/debate/judge.py` — judge agent + structured scoring (JSON + json-repair retry)
- `apps/api/app/debate/damage.py` — damage formula (use type chart)
- `apps/api/app/routers/encounter.py` — `POST /api/encounters`, `GET /api/encounters/{id}`
- `apps/api/app/routers/debate.py` — `POST .../turn`, `POST .../auto`, `WS .../stream`, `/flee`

## Interfaces
- Consume: `gateway` (complete/stream), `app.redis_state` helpers (append_utterance, get_transcript, set_hp, get_hp_map, key builders), models, schemas, `get_session`.
- Consume from WS-A: `app.party.generator.generate_wild` / starter party (for enemy setup).
- Consume STUBS (define a thin Protocol/duck-typed call; the real impls land from other workstreams — guard with try/except or a default):
  - Gambits (WS-C): `from app.debate.gambits import choose_action` returning an action dict given (monster, battle_state). If import fails, fall back to "argue best point".
  - RAG memory (WS-D): `from app.memory.retriever import retrieve` returning a list of memory summaries. If import fails, return [].
- Expose for WS-F (training self-play): a function `run_self_play(party_monster, sparring_monster, topic, rounds) -> dict` that runs a headless debate (no Redis/WS needed; in-memory) and returns transcript + net score. Put it in `orchestrator.py`.

## Turn loop (per `app/schemas.py` shapes)
1. Build context for the actor: shared Redis transcript (window/summarize if long) + topic + side + injected RAG memories + harness/persona/skills.
2. For party: call gambits `choose_action` (forced behavior); constrain the prompt (skill/target/tone).
3. Generate utterance via gateway (stream tokens to WS when in the live path).
4. Append utterance to Redis transcript so the next actor sees it.
5. After the round: Judge scores each utterance -> JudgeVerdict; compute damage; apply to Redis HP; update momentum.
6. Win/loss when a side hits 0 HP. Wild HP < 25% => mark capturable (push over WS; expose capturable_ids in TurnResult).

## Encounter setup
- `POST /api/encounters` seeds Redis (meta, transcript empty, hp for all combatants, queue/turn order by initiative from level+type), writes an Encounter row, returns EncounterState.
- On completion, snapshot transcript to `encounters.transcript_ref` and write HP/result back to Postgres (single idempotent finalize).

## Judge (quality-critical)
- Short rubric, JSON-only output, one-shot example. Parse with `json_repair`. If parse fails, fall back to a heuristic score (length/keyword-overlap with topic). Judge model alias = "judge" (defaults local; can be pinned to claude later).

## Definition of done
- Can create an encounter and run `/turn` and `/auto` against the running stack producing utterances + verdicts + HP changes (test with the api venv / a pytest hitting redis+ollama, or a script).
- WS `/stream` emits utterance/verdict/hp events.
- `run_self_play(...)` works headless (WS-F depends on it).

## Rules
- Do NOT edit frozen shared files (models, redis_state, schemas, enums, main.py). Add Optional schema fields only if essential and note them in your memory file.
- Do NOT edit pyproject.toml; note new deps in `memories/ws-b-debate.md`.
- Keep enemy generations parallel (`asyncio.gather`) and judging round-level to control latency on a local model.
- When done: write `memories/ws-b-debate.md` and commit.
