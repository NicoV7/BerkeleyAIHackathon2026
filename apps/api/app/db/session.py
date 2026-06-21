"""Async SQLAlchemy engine + session, plus schema bootstrap.

For the hackathon we use `create_all` on startup (no Alembic) — pgvector and
pg_trgm extensions come from infra/postgres/init.sql.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create tables if they don't exist. Import models so they register."""
    from sqlalchemy import text

    from app.db import models  # noqa: F401  (registers tables on SQLModel.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # Lightweight idempotent migrations: create_all never ALTERs existing
        # tables, so additive columns need explicit IF NOT EXISTS here.
        for stmt in (
            "ALTER TABLE encounters ADD COLUMN IF NOT EXISTS transcript JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE encounters ADD COLUMN IF NOT EXISTS verdicts JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE encounters ADD COLUMN IF NOT EXISTS final_hp JSONB DEFAULT '{}'::jsonb",
            # WS-A persistence: run save/resume marker. TIMESTAMP WITHOUT TIME
            # ZONE to match the other naive-UTC timestamp columns.
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS saved_at TIMESTAMP",
        ):
            await conn.execute(text(stmt))


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async DB session."""
    async with SessionLocal() as session:
        yield session
