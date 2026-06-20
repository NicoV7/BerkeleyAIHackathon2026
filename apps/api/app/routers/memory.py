"""Memory router — GET /api/monsters/{id}/memories.

Returns hybrid-ranked memories for a monster, optionally filtered by event type
and a natural language query.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.memory.retriever import retrieve
from app.schemas import MemoryItem, MemoryQueryResult

router = APIRouter(prefix="/api/monsters", tags=["memory"])


@router.get("/{monster_id}/memories", response_model=MemoryQueryResult)
async def get_memories(
    monster_id: str,
    q: Optional[str] = None,
    type: Optional[str] = None,
    k: int = 8,
    session: AsyncSession = Depends(get_session),
) -> MemoryQueryResult:
    """Retrieve memories for a monster using hybrid RAG (vector + trigram).

    Query params:
        q:    Natural language query (optional; if absent, returns recent memories).
        type: Event type filter: BATTLE | PLAYER | CHARACTER (optional).
        k:    Max results (default 8).
    """
    query_str = q or ""

    try:
        raw = await retrieve(session, monster_id, query_str, k=k, event_type=type)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    items = [
        MemoryItem(
            id=r["id"],
            event_type=r["event_type"],
            summary=r["summary"],
            content=r["content"],
            salience=r["salience"],
            created_at=r["created_at"],
        )
        for r in raw
    ]
    return MemoryQueryResult(monster_id=monster_id, items=items)
