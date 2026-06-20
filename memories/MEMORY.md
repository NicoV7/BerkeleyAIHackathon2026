# Memory Index

One line per memory file. Loaded first to decide relevance. See `README.md` for the convention.

- [wave-0-foundation.md](wave-0-foundation.md) — foundation: DB schema, LLM gateway, Redis contract, frozen API schemas, web shell, router auto-mount.
- [ws-a-overworld.md](ws-a-overworld.md) — Phaser overworld + map/run/move API + starter-party/wild generation.
- [ws-b-debate.md](ws-b-debate.md) — debate turn engine, judge, damage, encounter+debate routers, WS stream, run_self_play.
- [ws-c-gambits.md](ws-c-gambits.md) — FF12 gambit DSL (choose_action), gambit CRUD router, battle/debate UI + WS hook.
- [ws-d-memory.md](ws-d-memory.md) — hybrid RAG: embeddings, write_event, retrieve (pgvector + trigram, RRF), memory router.
- [ws-e-party.md](ws-e-party.md) — capture (HP-gated), XP/level/evolution, capture router + party listing, PartyScreen.
- [ws-f-training.md](ws-f-training.md) — GEPA (DSPy + hand-rolled fallback) + GRPO-HITL, genome mutate/apply, training router + UI.
- Integration notes (Wave 2, orchestrator): `_now()` now naive-UTC (fixed root datetime bug all WS hit); WS stream envelope key is `type` (not `kind`); orchestrator opens its own session to call WS-D `retrieve(session, monster_id, query, k=)`; App.tsx mounts all 5 screens incl. `#gambits/{id}` hash route; model defaults = gemma3:1b (gemma3:4b pulled for quality).
