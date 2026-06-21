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
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS player_name TEXT DEFAULT 'Player'",
            "ALTER TABLE encounters ADD COLUMN IF NOT EXISTS transcript JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE encounters ADD COLUMN IF NOT EXISTS verdicts JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE encounters ADD COLUMN IF NOT EXISTS final_hp JSONB DEFAULT '{}'::jsonb",
            # WS-A persistence: run save/resume marker. TIMESTAMP WITHOUT TIME
            # ZONE to match the other naive-UTC timestamp columns.
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS saved_at TIMESTAMP",
            # THEME topics: per-run theme chosen at run start (battles draw a
            # random topic within it). Nullable/additive; debate_topic stays set.
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS theme VARCHAR",
            # Avatar selection persists on the run so empty-start onboarding can
            # apply the chosen type to the first pulled monster.
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS avatar_type VARCHAR",
            # ---- Gacha wave: stat columns on monsters (additive) ----
            "ALTER TABLE monsters ADD COLUMN IF NOT EXISTS atk INT NOT NULL DEFAULT 10",
            'ALTER TABLE monsters ADD COLUMN IF NOT EXISTS "def" INT NOT NULL DEFAULT 10',
            "ALTER TABLE monsters ADD COLUMN IF NOT EXISTS mp INT NOT NULL DEFAULT 50",
            "ALTER TABLE monsters ADD COLUMN IF NOT EXISTS max_mp INT NOT NULL DEFAULT 50",
            "ALTER TABLE monsters ADD COLUMN IF NOT EXISTS domain VARCHAR NOT NULL DEFAULT 'GENERAL'",
            "ALTER TABLE monsters ADD COLUMN IF NOT EXISTS wiki_url VARCHAR",
            "ALTER TABLE monsters ADD COLUMN IF NOT EXISTS wiki_hydrated BOOLEAN NOT NULL DEFAULT FALSE",
            # Avatar selection: marks the player's chosen-avatar starter as the
            # run's permanent main character (lead). Additive; defaults False.
            "ALTER TABLE monsters ADD COLUMN IF NOT EXISTS is_avatar BOOLEAN NOT NULL DEFAULT FALSE",
            # ---- Economy wave (WS-1): coin wallet on the run ----
            # create_all never ALTERs the existing `runs` table, so the additive
            # coins column needs an explicit IF NOT EXISTS here. New economy
            # tables (items, player_inventory, shop_stock) are auto-created by
            # create_all above (models imported -> registered on metadata).
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS coins INTEGER NOT NULL DEFAULT 0",
        ):
            await conn.execute(text(stmt))


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async DB session."""
    async with SessionLocal() as session:
        yield session
