# Memory Index

One line per memory file. Loaded first to decide relevance. See `README.md` for the convention.

- [wave-0-foundation.md](wave-0-foundation.md) — foundation: DB schema, LLM gateway, Redis contract, frozen API schemas, web shell, router auto-mount.
- [ws-a-overworld.md](ws-a-overworld.md) — Phaser overworld + map/run/move API + starter-party/wild generation.
- [ws-b-debate.md](ws-b-debate.md) — debate turn engine, judge, damage, encounter+debate routers, WS stream, run_self_play.
- [ws-c-gambits.md](ws-c-gambits.md) — FF12 gambit DSL (choose_action), gambit CRUD router, battle/debate UI + WS hook.
- [ws-d-memory.md](ws-d-memory.md) — hybrid RAG: embeddings, write_event, retrieve (pgvector + trigram, RRF), memory router.
- [ws-e-party.md](ws-e-party.md) — capture (HP-gated), XP/level/evolution, capture router + party listing, PartyScreen.
- [ws-f-training.md](ws-f-training.md) — GEPA (DSPy + hand-rolled fallback) + GRPO-HITL, genome mutate/apply, training router + UI.
- [ws-g-human-argue.md](ws-g-human-argue.md) — Wave 2 frontend pivot: human-argues (PlayerArgueRequest + run_human_round_stream + argue WS/REST), tasteful-pixel design system, dual ReasoningTrend, skill chips, bug fixes. Note: combatants need `gemma3:1b` pulled.
- Integration notes (Wave 2, orchestrator): `_now()` now naive-UTC (fixed root datetime bug all WS hit); WS stream envelope key is `type` (not `kind`); orchestrator opens its own session to call WS-D `retrieve(session, monster_id, query, k=)`; App.tsx mounts all 5 screens incl. `#gambits/{id}` hash route; model defaults = gemma3:1b (gemma3:4b pulled for quality).
- Battle persistence (debate.py `_finalize`): on win/loss/flee, snapshot transcript+verdicts+final_hp to durable `Encounter` columns (added via idempotent ALTER in `init_db`), write one embedded BATTLE `Memory` per party member (via WS-D `write_event`, content = POV summary + full transcript) so GEPA/RAG have real data, then `clear_conversation(eid)` evicts the heavy Redis keys (transcript+judge). `build_encounter_state` falls back to the durable snapshot when Redis is evicted.
- GEPA grounding (training/gepa.py): `run_gepa` now pulls the monster's recent BATTLE memories (`_load_battle_memories`), recovers the real debate topic from memory content (`_topic_from_battles`), and seeds the reflective loop with a critique of a real loss (`_seed_directive_from_battles`) before rehearsing self-play. Falls back to a generic topic when the agent has no battle history.
- Debate timeouts (debate.py): `/turn` wraps a round in `asyncio.wait_for(ROUND_TIMEOUT_S=120)` -> 504; `/auto` enforces `AUTO_BUDGET_S=240` wall-clock + per-round timeout, returning progress instead of hanging on a slow local model.
- Datetime workaround collapsed: `_now()` (naive UTC) is the single source of truth. Removed `_naive_created_at` (encounter.py) + explicit `created_at=` overrides in store.py/genome.py and their now-dead datetime imports.
