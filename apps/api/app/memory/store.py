"""Memory store — write per-monster events to the `memories` table.

Public API:
    write_event(session, monster_id, run_id, event_type, content,
                encounter_id=None, salience=0.5, model=None) -> Memory

Import-safe: all heavy imports are inside the function body so the module loads
even if the DB or gateway is unreachable.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EventType, Memory
from app.gateway.gateway import gateway
from app.memory.embeddings import embed

# Common English stop-words to filter out of keywords
_STOP = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "can", "not", "no", "nor",
    "so", "yet", "both", "either", "neither", "whether", "as", "if", "then",
    "that", "this", "these", "those", "it", "its", "i", "you", "he", "she",
    "we", "they", "them", "their", "our", "your", "my", "his", "her",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "all", "any", "each", "every", "some", "such", "than", "up", "out",
    "about", "into", "through", "during", "before", "after", "above",
    "below", "between", "just", "also", "very", "also", "more", "most",
    "other", "only", "same", "own", "again", "further", "once",
})


def _extract_keywords(text: str, max_words: int = 30) -> str:
    """Extract salient lowercased keywords from text for trigram search.

    Strips punctuation, lowercases, removes stop-words, deduplicates, and
    returns a space-joined string suitable for pg_trgm GIN index queries.
    """
    tokens = re.findall(r"[a-zA-Z']+", text.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for tok in tokens:
        tok = tok.strip("'")
        if len(tok) >= 3 and tok not in _STOP and tok not in seen:
            seen.add(tok)
            keywords.append(tok)
            if len(keywords) >= max_words:
                break
    return " ".join(keywords)


async def write_event(
    session: AsyncSession,
    monster_id: str,
    run_id: str,
    event_type: EventType | str,
    content: str,
    encounter_id: Optional[str] = None,
    salience: float = 0.5,
    model: Optional[str] = None,
) -> Memory:
    """Summarize content, embed summary, extract keywords, and persist to DB.

    Args:
        session:      Async SQLAlchemy session (FastAPI dep or explicit).
        monster_id:   ID of the monster this memory belongs to.
        run_id:       ID of the current run.
        event_type:   EventType enum value or string ("BATTLE"/"PLAYER"/"CHARACTER").
        content:      Full event content (may be long).
        encounter_id: Optional encounter FK.
        salience:     0-1 importance weight (default 0.5).
        model:        Override the summarisation model (default "llama3.2:3b").

    Returns:
        The persisted Memory ORM object.
    """
    # Normalise event_type
    if isinstance(event_type, str):
        event_type = EventType(event_type.upper())

    # 1. Summarise to one sentence
    summ_model = model or "llama3.2:3b"
    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise summariser. "
                "Summarise the following event in exactly ONE short sentence "
                "(max 25 words). No extra commentary."
            ),
        },
        {"role": "user", "content": content},
    ]
    try:
        summary = (await gateway.complete(messages, model=summ_model, max_tokens=64)).strip()
    except Exception:  # noqa: BLE001
        # Fallback: truncate content
        summary = content[:200].strip()

    # 2. Embed the summary
    try:
        vecs = await embed([summary])
        embedding = vecs[0]
    except Exception:  # noqa: BLE001
        embedding = None

    # 3. Extract keywords
    keywords = _extract_keywords(content + " " + summary)

    # 4. Persist
    # created_at comes from the model's naive-UTC default (_now).
    memory = Memory(
        monster_id=monster_id,
        run_id=run_id,
        event_type=event_type,
        content=content,
        summary=summary,
        embedding=embedding,
        keywords=keywords,
        salience=salience,
        encounter_id=encounter_id,
    )
    session.add(memory)
    await session.commit()
    await session.refresh(memory)

    # Write-through to the RedisVL hot-cache (best-effort; never breaks the
    # durable pg write). Falls back transparently if Redis/index is unavailable.
    try:
        from app.memory import redis_index

        await redis_index.index_memory(memory)
    except Exception:  # noqa: BLE001
        pass

    return memory
