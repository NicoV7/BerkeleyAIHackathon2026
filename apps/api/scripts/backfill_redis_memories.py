"""Backfill the RedisVL memory hot-cache from all pgvector Memory rows.

Use after a Redis flush, an index schema change, or first enabling the cache on
a populated database. Idempotent: re-loading a row overwrites its hash in place.

Run inside the api container so it can reach Postgres + Redis:

    docker compose exec api python -m scripts.backfill_redis_memories
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db.models import Memory
from app.db.session import SessionLocal
from app.memory import redis_index


async def main() -> None:
    created = await redis_index.ensure_index()
    if not created:
        print("RedisVL index unavailable (cache disabled or Redis < 8). Nothing to do.")
        return

    async with SessionLocal() as session:
        stmt = select(Memory).where(Memory.embedding.isnot(None)).order_by(Memory.created_at)
        rows = (await session.execute(stmt)).scalars().all()

    count = 0
    for m in rows:
        await redis_index.index_memory(m)
        count += 1
    print(f"Backfilled {count} memories into RedisVL index '{redis_index.settings.redis_index_name}'.")


if __name__ == "__main__":
    asyncio.run(main())
