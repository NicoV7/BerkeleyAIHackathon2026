"""RedisVL vector hot-cache for per-monster memories.

A fast ANN layer that sits IN FRONT OF the durable pgvector store
(`memory/store.py` + `memory/retriever.py`). pgvector remains the source of
truth; this index just serves sub-millisecond recall during live encounters and
is fully rebuildable from Postgres at any time.

Design:
- Best-effort everywhere. Every public coroutine swallows errors so a Redis
  blip / missing module can never break the durable write or retrieval path.
- Dedicated binary Redis client (NO ``decode_responses``) — vectors are stored
  as raw float32 bytes. This is separate from ``redis_state.get_redis()`` which
  decodes responses for the encounter cache.
- Keys live under the ``mem:`` prefix, disjoint from the frozen ``enc:{id}:*``
  encounter schema, so the two coexist in the same Redis.

Requires Redis 8+ (bundles the search & query engine) or Redis Stack.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("uvicorn.error")

# nomic-embed-text — must match the pgvector column dim (EMBED_DIM in models.py).
_EMBED_DIM = 768
_PREFIX = "mem:"

_index: Any = None  # cached AsyncSearchIndex


def _schema() -> dict:
    return {
        "index": {"name": settings.redis_index_name, "prefix": _PREFIX},
        "fields": [
            {"name": "id", "type": "tag"},
            {"name": "monster_id", "type": "tag"},
            {"name": "run_id", "type": "tag"},
            {"name": "event_type", "type": "tag"},
            {"name": "summary", "type": "text"},
            {"name": "content", "type": "text"},
            {"name": "salience", "type": "numeric"},
            {"name": "created_ts", "type": "numeric"},
            {
                "name": "embedding",
                "type": "vector",
                "attrs": {
                    "dims": _EMBED_DIM,
                    "distance_metric": "cosine",
                    "algorithm": "hnsw",
                    "datatype": "float32",
                },
            },
        ],
    }


async def _get_index() -> Any:
    """Lazily build the AsyncSearchIndex bound to a dedicated binary client."""
    global _index
    if _index is not None:
        return _index
    from redis.asyncio import Redis
    from redisvl.index import AsyncSearchIndex

    # decode_responses MUST be False here — vectors are binary.
    client = Redis.from_url(settings.redis_url)
    _index = AsyncSearchIndex.from_dict(_schema(), redis_client=client)
    return _index


async def ensure_index() -> bool:
    """Create the index if absent. Idempotent; never raises. Returns success."""
    if not settings.memory_cache_enabled:
        return False
    try:
        index = await _get_index()
        # overwrite=False keeps existing data; a pre-existing index is fine.
        await index.create(overwrite=False)
        return True
    except Exception as e:  # noqa: BLE001
        log.info("RedisVL ensure_index skipped: %s", e)
        return False


def _vec_bytes(vec: list[float]) -> bytes:
    import numpy as np

    return np.asarray(vec, dtype=np.float32).tobytes()


def _event_value(event_type: Any) -> str:
    """Normalise an EventType / str to its stored tag value (e.g. 'BATTLE')."""
    if hasattr(event_type, "value"):
        return str(event_type.value)
    s = str(event_type)
    return s.upper() if s.islower() else s


async def index_memory(mem: Any) -> None:
    """Write-through a single Memory ORM row into the Redis index. Best-effort."""
    if not settings.memory_cache_enabled:
        return
    if getattr(mem, "embedding", None) is None:
        return  # nothing to ANN-search on
    try:
        index = await _get_index()
        created = getattr(mem, "created_at", None)
        created_ts = created.timestamp() if created is not None else 0.0
        record = {
            "id": mem.id,
            "monster_id": mem.monster_id,
            "run_id": mem.run_id or "",
            "event_type": _event_value(mem.event_type),
            "summary": mem.summary or "",
            "content": mem.content or "",
            "salience": float(mem.salience if mem.salience is not None else 0.5),
            "created_ts": float(created_ts),
            "embedding": _vec_bytes(mem.embedding),
        }
        await index.load([record], id_field="id")
    except Exception as e:  # noqa: BLE001
        log.info("RedisVL index_memory skipped for %s: %s", getattr(mem, "id", "?"), e)


async def search(
    monster_id: str,
    query_vec: list[float],
    k: int = 4,
    event_type: Optional[Any] = None,
) -> list[dict]:
    """Vector KNN over one monster's memories. Returns MemoryItem-shaped dicts.

    Returns [] on any error / empty index so callers fall back to pgvector.
    """
    if not settings.memory_cache_enabled or not query_vec:
        return []
    try:
        from redisvl.query import VectorQuery
        from redisvl.query.filter import Tag

        index = await _get_index()
        flt = Tag("monster_id") == monster_id
        if event_type is not None:
            flt = flt & (Tag("event_type") == _event_value(event_type))

        query = VectorQuery(
            vector=_vec_bytes(query_vec),
            vector_field_name="embedding",
            return_fields=["id", "event_type", "summary", "content", "salience", "created_ts"],
            num_results=k,
            filter_expression=flt,
        )
        hits = await index.query(query)
        return [_hit_to_dict(h) for h in hits]
    except Exception as e:  # noqa: BLE001
        log.info("RedisVL search miss (falling back to pgvector): %s", e)
        return []


def _hit_to_dict(hit: dict) -> dict:
    """Map a RedisVL hit to the same shape as retriever._memory_to_dict."""
    from datetime import datetime

    ts = hit.get("created_ts")
    created_at = ""
    try:
        if ts not in (None, ""):
            created_at = datetime.utcfromtimestamp(float(ts)).isoformat()
    except (ValueError, OSError):
        created_at = ""
    sal = hit.get("salience")
    # RedisVL returns the document key ("mem:<uuid>") as `id`; strip the prefix
    # so the shape matches retriever._memory_to_dict (bare uuid).
    mem_id = str(hit.get("id", ""))
    if mem_id.startswith(_PREFIX):
        mem_id = mem_id[len(_PREFIX):]
    return {
        "id": mem_id,
        "event_type": hit.get("event_type", ""),
        "summary": hit.get("summary", ""),
        "content": hit.get("content", ""),
        "salience": float(sal) if sal not in (None, "") else 0.5,
        "created_at": created_at,
    }
