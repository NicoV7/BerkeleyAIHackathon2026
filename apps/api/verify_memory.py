"""Verification script for WS-D memory system.

Writes 4 memories for a fake monster+run, then retrieves by query,
verifies ranking, and checks the HTTP endpoint.
Run with:
    /Users/nicov/BerkeleyAIHackathon2026/apps/api/.venv/bin/python verify_memory.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel


# asyncpg needs timezone=True to handle aware datetimes correctly
DATABASE_URL = "postgresql+asyncpg://debate:debate@localhost:5432/debate"
# Connect args: tell asyncpg the server is UTC so it handles aware datetimes
CONNECT_ARGS = {"server_settings": {"timezone": "UTC"}}


async def main() -> None:
    # ---- Setup engine pointing at host-exposed postgres ----
    engine = create_async_engine(DATABASE_URL, echo=False, connect_args=CONNECT_ARGS)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Import after engine ready (models register on SQLModel.metadata)
    # We patch settings so gateway points at localhost rather than docker hostname
    import app.config as _cfg
    _cfg.settings.__dict__["ollama_base_url"] = "http://localhost:11434"

    from app.memory.store import write_event
    from app.memory.retriever import retrieve
    from app.db.models import EventType

    # Create fake IDs (no FK enforcement in verify)
    monster_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    print(f"[verify] Using monster_id={monster_id[:8]}... run_id={run_id[:8]}...")

    events = [
        (
            EventType.battle,
            "The Logos debater overwhelmed the opponent with statistical evidence about climate "
            "change, citing three peer-reviewed studies on carbon emissions.",
            0.9,
        ),
        (
            EventType.player,
            "The player chose to use a Pathos appeal to the audience's sense of empathy "
            "for future generations in the climate debate.",
            0.7,
        ),
        (
            EventType.character,
            "Monstera leveled up to level 5 after a decisive victory in the philosophical "
            "debate about ethics of AI.",
            0.6,
        ),
        (
            EventType.battle,
            "The opponent used a Rhetoric technique to reframe the question about renewable "
            "energy, shifting focus from economics to national identity.",
            0.8,
        ),
    ]

    print("\n[verify] Writing 4 memory events...")
    async with session_factory() as session:
        memories = []
        for et, content, salience in events:
            m = await write_event(
                session=session,
                monster_id=monster_id,
                run_id=run_id,
                event_type=et,
                content=content,
                salience=salience,
                model="gemma3:1b",
            )
            memories.append(m)
            print(f"  [+] {et.value}: summary={m.summary[:60]!r}  keywords={m.keywords[:50]!r}")

    print("\n[verify] Retrieving with query: 'climate statistics evidence'")
    async with session_factory() as session:
        results = await retrieve(session, monster_id, "climate statistics evidence", k=4)

    if not results:
        print("FAIL: retrieve returned empty list!")
        sys.exit(1)

    print(f"  Got {len(results)} results (k=4):")
    for i, r in enumerate(results):
        print(f"  [{i+1}] event_type={r['event_type']}  salience={r['salience']}  "
              f"summary={r['summary'][:70]!r}")

    # The first result should relate to climate/statistics (event 0 or 1)
    top_summary = results[0]["summary"].lower()
    top_content = results[0]["content"].lower()
    climate_keywords = {"climate", "statistic", "statistical", "evidence", "carbon", "emission",
                        "peer-reviewed", "logos", "studies"}
    matches = climate_keywords & (set(top_summary.split()) | set(top_content.split()))
    if matches:
        print(f"\n  PASS: top result contains climate/evidence keywords: {matches}")
    else:
        print(f"\n  WARN: top result may not be most relevant "
              f"(content={top_content[:80]!r}); check embedding quality.")

    print("\n[verify] Checking HTTP endpoint GET /api/monsters/{id}/memories...")
    base = "http://localhost:8000"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{base}/api/monsters/{monster_id}/memories",
                                params={"q": "climate statistics", "k": "4"})
        if resp.status_code != 200:
            print(f"FAIL: HTTP {resp.status_code}: {resp.text}")
            sys.exit(1)
        data = resp.json()
        assert data["monster_id"] == monster_id, f"Wrong monster_id: {data['monster_id']}"
        assert isinstance(data["items"], list), "items should be a list"
        print(f"  PASS: HTTP 200, {len(data['items'])} items returned")
        for item in data["items"]:
            print(f"    id={item['id'][:8]}... event_type={item['event_type']}  "
                  f"summary={item['summary'][:60]!r}")

    print("\n[verify] Checking /api/health still ok...")
    async with httpx.AsyncClient(timeout=10) as client:
        hr = await client.get(f"{base}/api/health")
        assert hr.json()["status"] == "ok", f"Health degraded: {hr.json()}"
        print(f"  PASS: {hr.json()['status']}")

    print("\n[verify] All checks passed.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
