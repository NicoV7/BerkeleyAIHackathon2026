"""FastAPI entrypoint.

Mounts the health router (Wave 0) and tolerantly auto-mounts Wave 1 routers as
they land. Each workstream drops a module in app/routers/ exposing `router`;
list its name in OPTIONAL_ROUTERS and it mounts when present — no merge conflict
on a shared include list.
"""
from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.session import init_db
from app.gateway.gateway import gateway
from app.routers import health

log = logging.getLogger("uvicorn.error")

# Wave 1 workstreams add their router module name here (or it's tried anyway).
OPTIONAL_ROUTERS = [
    "map",
    "encounter",
    "debate",
    "party",
    "memory",
    "training",
    "runs",
    "world",
    # Gacha wave (Wave A): named-persona summoning + post-battle drops.
    # Replaces the deleted `capture` router.
    "gacha",
]


async def _init_memory_cache() -> None:
    """Ensure the RedisVL memory index exists and warm it from recent pg rows.

    Best-effort: a missing module / older Redis just leaves retrieval on the
    durable pgvector path. Never blocks startup.
    """
    try:
        from sqlalchemy import select

        from app.db.models import Memory
        from app.db.session import SessionLocal
        from app.memory import redis_index

        if not await redis_index.ensure_index():
            return
        # Warm the cache with the most recent embedded memories so already-played
        # battles are immediately searchable.
        async with SessionLocal() as session:
            stmt = (
                select(Memory)
                .where(Memory.embedding.isnot(None))
                .order_by(Memory.created_at.desc())
                .limit(500)
            )
            rows = (await session.execute(stmt)).scalars().all()
        for m in rows:
            await redis_index.index_memory(m)
        log.info("RedisVL memory cache warmed: %d rows", len(rows))
    except Exception as e:  # noqa: BLE001
        log.info("RedisVL memory cache init skipped: %s", e)


async def _seed_personas() -> None:
    """Idempotent upsert of the gacha persona catalog (Wave 0).

    Best-effort: a missing module / DB error just leaves the catalog as-is so
    we never block startup. Tables already exist (init_db ran first).
    """
    try:
        from app.db.session import SessionLocal
        from app.party.personas_seed import upsert_personas

        async with SessionLocal() as session:
            n = await upsert_personas(session)
        log.info("Personas seeded: %d rows", n)
    except Exception as e:  # noqa: BLE001
        log.info("Persona seed skipped: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("DB initialized")
    await _seed_personas()
    await _init_memory_cache()
    # Warm the actor + judge models at startup so the first battle round isn't a cold
    # start (cold gemma3:1b first-token can exceed the streaming guard → fallback text).
    try:
        import asyncio as _asyncio

        from app.debate.orchestrator import prewarm_models

        _asyncio.create_task(prewarm_models())
    except Exception:  # noqa: BLE001 — prewarm is best-effort, never block startup
        pass
    yield
    await gateway.aclose()


app = FastAPI(title="Debate RPG API", version="0.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)

for name in OPTIONAL_ROUTERS:
    try:
        mod = importlib.import_module(f"app.routers.{name}")
        app.include_router(mod.router)
        log.info("Mounted router: %s", name)
    except ModuleNotFoundError:
        log.info("Router not present yet (skipping): %s", name)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to mount router %s: %s", name, e)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "debate-rpg-api", "docs": "/docs", "health": "/api/health"}
