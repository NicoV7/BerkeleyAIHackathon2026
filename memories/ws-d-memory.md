# WS-D Memory — Persistent Memory + Hybrid RAG

## Owns

- `apps/api/app/memory/embeddings.py` — `embed(texts) -> list[list[float]]` via gateway nomic-embed-text
- `apps/api/app/memory/store.py` — `write_event(...)` summarise + embed + keyword extract + insert Memory row
- `apps/api/app/memory/retriever.py` — `retrieve(...)` hybrid pgvector cosine + trigram ILIKE, merged via RRF
- `apps/api/app/routers/memory.py` — `GET /api/monsters/{id}/memories?q=&type=&k=`

## Key Decisions

1. **Datetime naive fix**: `models.py` declares `created_at` as `TIMESTAMP WITHOUT TIME ZONE` but `_now()` yields timezone-aware datetimes. `store.py` explicitly passes `created_at=datetime.now(timezone.utc).replace(tzinfo=None)` to avoid asyncpg rejection. This is a known cross-cutting issue that affects ALL other writers of time-stamped rows (WS-A through WS-F).

2. **Summarisation model**: defaults to `"gemma3:1b"` (small, fast). Callers can override via the `model=` parameter. Falls back to content truncation if LLM call fails.

3. **Embedding model**: `nomic-embed-text` (768-dim). Pulled on setup; confirmed available.

4. **RRF_K = 60**: standard constant; each retrieval leg runs up to `k*3` candidates before merging.

5. **Empty-query fallback**: `retrieve(... query="")` returns most-recent memories (ordered by `created_at desc`) rather than failing, so the HTTP endpoint works without a query string.

6. **No stop at import time**: all files are import-safe (errors surface at call time); WS-B/WS-E can import even if DB/ollama is unreachable.

## Interfaces (exact signatures)

```python
# embeddings.py
async def embed(texts: list[str]) -> list[list[float]]: ...

# store.py
async def write_event(
    session: AsyncSession,
    monster_id: str,
    run_id: str,
    event_type: EventType | str,       # "BATTLE" | "PLAYER" | "CHARACTER"
    content: str,
    encounter_id: Optional[str] = None,
    salience: float = 0.5,
    model: Optional[str] = None,       # summarisation model, default "gemma3:1b"
) -> Memory: ...

# retriever.py
async def retrieve(
    session: AsyncSession,
    monster_id: str,
    query: str,
    k: int = 4,
    event_type: Optional[EventType | str] = None,
) -> list[dict]: ...  # dicts shaped like MemoryItem
```

## HTTP Endpoint

```
GET /api/monsters/{monster_id}/memories
  ?q=<natural language query>    (optional; omit for recent-first)
  &type=BATTLE|PLAYER|CHARACTER  (optional filter)
  &k=<int>                       (default 8)
-> MemoryQueryResult { monster_id, items: list[MemoryItem] }
```

## Verification Results

- `nomic-embed-text:latest` pulled and confirmed in Ollama (`gemma3:1b` also available).
- 4 memories written for a test monster; retrieve("climate statistics evidence") correctly returned BATTLE memory (with climate/statistical content) ranked #1.
- `GET /api/monsters/{id}/memories?q=climate+statistics&k=4` returns 200 with 4 ranked items.
- `GET /api/monsters/{id}/memories?type=BATTLE` returns 2 BATTLE-only items (recency order).
- `/api/health` continues returning `{"status":"ok"}`.

## Frozen-Contract Gaps / Notes for Orchestrator

- **datetime aware/naive mismatch**: `models.py _now()` returns `datetime.now(timezone.utc)` (aware), but all table columns are `TIMESTAMP WITHOUT TIME ZONE`. Any workstream writing to any table must strip tzinfo or pass naive UTC. Recommend WO-0 add `timezone_aware_column=False` or switch the engine to use `timezone=True` in `create_async_engine`. See: https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#datetime-timezone
- **FK constraint on memories**: `Memory.monster_id` FKs to `monsters.id` and `Memory.run_id` FKs to `runs.id`. Callers of `write_event` must pass real IDs from the DB (not test UUIDs). WS-B/WS-E should already have these from encounter context.

## Open TODOs (Wave 2)

- [ ] Add pgvector IVFFlat index warm-up (needs `>= 1 list` rows before build).
- [ ] Salience scoring: currently caller-supplied; could auto-score from verdict damage.
- [ ] Add `pg_trgm.similarity()` score to keyword leg (currently pure ILIKE OR — would improve ranking quality).
- [ ] GRPO training loop (WS-F) could consume retrieved memories as preference signal.
- [ ] Streaming support for memory injection into debate context (WS-B).
- [ ] Snapshot memories to Redis for ultra-low-latency access during active encounters.
