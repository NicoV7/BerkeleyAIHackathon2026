"""Hybrid memory retriever — pgvector cosine + trigram keyword, merged via RRF.

Public API:
    retrieve(session, monster_id, query, k=4, event_type=None) -> list[MemoryItem-like]

Import-safe: safe to import even if DB is unavailable; errors surface at call time.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EventType, Memory
from app.memory.embeddings import embed

# RRF rank constant (standard is 60)
_RRF_K = 60


def _memory_to_dict(m: Memory) -> dict:
    """Convert a Memory ORM object to a MemoryItem-shaped dict."""
    return {
        "id": m.id,
        "event_type": m.event_type.value if hasattr(m.event_type, "value") else str(m.event_type),
        "summary": m.summary,
        "content": m.content,
        "salience": m.salience,
        "created_at": m.created_at.isoformat() if m.created_at else "",
    }


async def retrieve(
    session: AsyncSession,
    monster_id: str,
    query: str,
    k: int = 4,
    event_type: Optional[EventType | str] = None,
) -> list[dict]:
    """Hybrid retrieval: pgvector cosine + trigram keyword, merged via RRF.

    Args:
        session:    Async SQLAlchemy session.
        monster_id: Filter memories to this monster.
        query:      Natural language query string.
        k:          Number of results to return.
        event_type: Optional filter by EventType.

    Returns:
        List of MemoryItem-shaped dicts, ranked best-first.
    """
    if not query or not query.strip():
        # No query: return most recent memories for this monster
        fallback_stmt = (
            select(Memory)
            .where(Memory.monster_id == monster_id)
        )
        if event_type is not None:
            if isinstance(event_type, str):
                try:
                    event_type = EventType(event_type.upper())
                except ValueError:
                    event_type = None
            if event_type is not None:
                fallback_stmt = fallback_stmt.where(Memory.event_type == event_type)
        fallback_stmt = fallback_stmt.order_by(Memory.created_at.desc()).limit(k)
        fb_result = await session.execute(fallback_stmt)
        return [_memory_to_dict(r) for r in fb_result.scalars().all()]

    # Normalise event_type filter
    et_filter: Optional[EventType] = None
    if event_type is not None:
        if isinstance(event_type, str):
            try:
                et_filter = EventType(event_type.upper())
            except ValueError:
                et_filter = None
        else:
            et_filter = event_type

    # -- 1. Vector search (cosine distance, ascending = most similar first) --
    vector_ids: list[str] = []
    try:
        vecs = await embed([query])
        q_vec = vecs[0]

        # Build base query for vector search
        vec_stmt = (
            select(Memory)
            .where(Memory.monster_id == monster_id)
            .where(Memory.embedding.isnot(None))
        )
        if et_filter is not None:
            vec_stmt = vec_stmt.where(Memory.event_type == et_filter)
        vec_stmt = vec_stmt.order_by(Memory.embedding.cosine_distance(q_vec)).limit(k * 3)

        vec_result = await session.execute(vec_stmt)
        vector_rows = vec_result.scalars().all()
        vector_ids = [r.id for r in vector_rows]
    except Exception:  # noqa: BLE001
        vector_rows = []

    # -- 2. Keyword / trigram search (ILIKE on keywords column) --
    keyword_ids: list[str] = []
    try:
        # Build keyword tokens from the query (same logic as extraction)
        import re
        tokens = re.findall(r"[a-zA-Z']+", query.lower())
        # Filter to meaningful tokens (length >= 3)
        kw_tokens = [t.strip("'") for t in tokens if len(t.strip("'")) >= 3][:5]

        if kw_tokens:
            kw_stmt = (
                select(Memory)
                .where(Memory.monster_id == monster_id)
                .where(Memory.keywords.isnot(None))
            )
            if et_filter is not None:
                kw_stmt = kw_stmt.where(Memory.event_type == et_filter)

            # Build OR of ILIKE conditions for each token
            from sqlalchemy import or_
            ilike_clauses = [Memory.keywords.ilike(f"%{tok}%") for tok in kw_tokens]
            kw_stmt = kw_stmt.where(or_(*ilike_clauses)).limit(k * 3)

            kw_result = await session.execute(kw_stmt)
            keyword_rows = kw_result.scalars().all()
            keyword_ids = [r.id for r in keyword_rows]
        else:
            keyword_rows = []
    except Exception:  # noqa: BLE001
        keyword_rows = []

    # -- 3. Reciprocal Rank Fusion --
    # Build id -> Memory map from both result sets
    id_to_memory: dict[str, Memory] = {}
    for r in list(vector_rows) + list(keyword_rows):
        id_to_memory[r.id] = r

    rrf_scores: dict[str, float] = {}

    for rank, mid in enumerate(vector_ids, start=1):
        rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (_RRF_K + rank)

    for rank, mid in enumerate(keyword_ids, start=1):
        rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (_RRF_K + rank)

    # Sort by descending RRF score
    ranked_ids = sorted(rrf_scores.keys(), key=lambda mid: rrf_scores[mid], reverse=True)[:k]

    # If we have no results yet (no embeddings + no keywords), fall back to
    # recency-ordered results for this monster
    if not ranked_ids:
        fallback_stmt = (
            select(Memory)
            .where(Memory.monster_id == monster_id)
        )
        if et_filter is not None:
            fallback_stmt = fallback_stmt.where(Memory.event_type == et_filter)
        fallback_stmt = fallback_stmt.order_by(Memory.created_at.desc()).limit(k)
        fb_result = await session.execute(fallback_stmt)
        return [_memory_to_dict(r) for r in fb_result.scalars().all()]

    return [_memory_to_dict(id_to_memory[mid]) for mid in ranked_ids if mid in id_to_memory]
