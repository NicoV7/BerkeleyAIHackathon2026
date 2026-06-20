# WS-D — Memory / Hybrid RAG

## Goal
Persistent per-monster memory of run events (BATTLE / PLAYER / CHARACTER), with hybrid
retrieval (vector similarity + keyword/trigram) injected into agents during debates and
training.

## You OWN
- `apps/api/app/memory/embeddings.py` — `embed(texts) -> list[vec]` via the gateway's embed (nomic-embed-text)
- `apps/api/app/memory/store.py` — `write_event(session, monster_id, run_id, event_type, content, ...)`: summarize (short, via gateway), embed, populate `keywords` (lowercased keyword string for trigram index), insert Memory row
- `apps/api/app/memory/retriever.py` — `retrieve(session, monster_id, query, k=4, event_type=None) -> list[MemoryItem-like]`: hybrid search = pgvector cosine (`embedding <=> q`) + trigram/ILIKE keyword match on `keywords`, merged via Reciprocal Rank Fusion, top-k
- `apps/api/app/routers/memory.py` — `GET /api/monsters/{id}/memories?q=&type=` (-> MemoryQueryResult) for the inspector/debug UI

## Interfaces
- Expose: `retrieve(...)` (WS-B injects results into agent context — it imports `from app.memory.retriever import retrieve`, falling back to [] if absent, so ship it), and `write_event(...)` (WS-B/WS-E call on notable events).
- Consume: `gateway.embed`, models (Memory, EMBED_DIM=768), schemas (MemoryItem, MemoryQueryResult), get_session.

## Details
- Embedding dim is 768 (nomic-embed-text). The Memory.embedding column is `Vector(768)`.
- For pgvector cosine in SQLAlchemy, use the `pgvector` package's operators (e.g. `Memory.embedding.cosine_distance(q)`); order ascending, limit.
- `keywords` is a plain text column with a GIN trigram index (`gin_trgm_ops`). Populate it with a normalized bag of salient words from content; query with ILIKE/`%` or `pg_trgm` similarity.
- RRF: rank lists from each method, score = sum(1/(60+rank)); sort desc; take top-k.
- Keep summaries short (one sentence) — they get injected into small-model context.

## Definition of done
- A pytest (or script) that: writes 3-4 memories for a monster, then `retrieve` returns the most relevant by a query — runs against the live postgres+ollama (embeddings) stack.
- `GET /api/monsters/{id}/memories` returns items.

## Rules
- Do NOT edit frozen shared files (models, redis_state, schemas, enums, main.py) — `memory` router auto-mounts. If you need a Memory column you don't have, STOP and note it in your memory file (the orchestrator will reconcile) rather than editing models.py.
- Do NOT edit pyproject.toml; note new deps in `memories/ws-d-memory.md`.
- When done: write `memories/ws-d-memory.md` and commit.
