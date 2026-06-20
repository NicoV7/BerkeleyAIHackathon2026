# Wave 0 — Foundation (orchestrator)

The contract-frozen base every Wave 1 workstream builds on. Do NOT rename/remove
these; only add (new optional columns, new router modules, new schema fields as Optional).

## Owns
- `infra/docker-compose.yml` (services: postgres+pgvector, redis, ollama, api, web), `infra/postgres/init.sql` (vector + pg_trgm), `infra/ollama/pull-models.sh`.
- `apps/api/app/config.py` — `settings` singleton (env-driven).
- `apps/api/app/db/models.py` — **all tables**: Run, Monster, Skill, GambitRule, Memory (pgvector `embedding` dim=768 + `keywords` tsvector-via-text + ivfflat/gin indexes), Encounter, TrainingArtifact. Enums: MonsterOwner, DebateType (LOGOS/PATHOS/ETHOS/CHAOS/SOCRATIC/RHETORIC), EventType, EncounterResult, RunStatus.
- `apps/api/app/db/session.py` — async engine, `SessionLocal`, `get_session` dep, `init_db()` (create_all on startup, no Alembic).
- `apps/api/app/redis_state.py` — **frozen Redis key schema** for encounters: `enc:{id}:{meta|transcript|hp|queue|judge|momentum}` + key builders + thin helpers (`append_utterance`, `get_transcript`, `set_hp`, `get_hp_map`, `clear_encounter`, `ping`). Redis is source of truth DURING an encounter.
- `apps/api/app/gateway/{gateway.py,models.py}` — `gateway` singleton. `complete()/stream()/embed()/health()`. Providers via httpx: ollama (default), anthropic, openai. Alias registry in models.py (`default`, `judge`, `gemma`, `qwen`, `claude`, `gpt`). Bottom-up: defaults to local Ollama.
- `apps/api/app/schemas.py` — **frozen Pydantic API contract** (Run/Map/Encounter/Gambit/Memory/Training request+response shapes).
- `apps/api/app/main.py` — app + CORS + lifespan(init_db). **Tolerant router auto-mount**: drop `app/routers/<name>.py` exposing `router`; it auto-mounts (names tried: map, encounter, debate, party, memory, capture, training). No shared include-list to conflict on.
- `apps/api/app/routers/health.py` — `/api/health` (db, redis, gateway).
- `packages/shared/enums.ts` — mirrors DebateType + TYPE_CHART + `typeMultiplier()`; `types.gen.ts` placeholder (regen via web `pnpm gen:types` from OpenAPI).
- `apps/web/` — Vite+React 19+Tailwind v4 shell. `state/store.ts` (zustand: runId, screen, activeEncounterId), `api/client.ts` (`api.get/post/...`, `wsUrl()`), `App.tsx` (menu→start run + screen nav + placeholder panels per workstream).

## Interfaces Wave 1 consumes
- LLM: `from app.gateway.gateway import gateway` → `await gateway.complete(messages, model="default")`, `gateway.stream(...)`, `gateway.embed([...])`.
- DB: `from app.db.session import get_session` (FastAPI dep), models from `app.db.models`.
- Redis: helpers in `app.redis_state`.
- Schemas: import response/request models from `app.schemas`.

## Decisions
- httpx-only gateway (no provider SDKs) → light container, offline-capable, easy streaming. Anthropic streaming intentionally falls back to one chunk (Wave 2 can improve).
- `create_all` instead of Alembic for hackathon speed; pgvector/pg_trgm via init.sql.
- API container pinned to Python 3.12 (host is 3.14, thin wheel coverage).
- Frontend proxies `/api` → :8000 in dev, so the browser is same-origin (WS works too).

## Open TODOs (Wave 1 fills)
- Routers: map (WS-A), encounter/debate (WS-B), party+gambits (WS-C), memory (WS-D), capture (WS-E), training (WS-F).
- `party/generator.py` (WS-A) — starter party roll, used by `/api/runs`.
- Memory store must populate `keywords` on write (WS-D).
