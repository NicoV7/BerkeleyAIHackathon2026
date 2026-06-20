"""T2 integration — end-to-end memory write → retrieve round-trip (DB-backed).

Exercises the real persistence + retrieval path:

    app.memory.store.write_event(...)   # summarise → embed → keyword → persist
    app.memory.retriever.retrieve(...)  # pgvector cosine + trigram, merged via RRF

against a live Postgres. The LLM gateway is neutralised with the deterministic
`gateway_mock` fixture (from tests/conftest.py) so summarisation/embedding are
stable and offline — only the database is real.

Host-friendliness: every test depends on the `require_db` fixture, which
*skips* (never errors) when Postgres is unreachable from the host. Collection
therefore always passes on a bare host while the live Docker stack is mid-edit.
Run collection-only with:

    cd apps/api && python -m pytest tests/integration/test_memory_rag.py --collect-only
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

# Mark every test in this module as an asyncio coroutine test.
pytestmark = pytest.mark.asyncio


@contextlib.asynccontextmanager
async def _host_session():
    """Yield an AsyncSession bound to the *host-reachable* DATABASE_URL.

    The app's own `SessionLocal` engine targets the Docker-internal host
    (e.g. ``postgres``), which does not resolve from the host running the
    tests. The conftest reachability probe (`require_db`) rewrites that host to
    ``localhost``; we mirror that rewrite here so "the probe says reachable"
    and "this session connects" are the *same* condition. The engine is
    created per-use and disposed afterwards so no global state leaks.
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.config import settings

    # Mirror the conftest probe: rewrite Docker-internal hosts to localhost so
    # this session reaches the port the compose stack publishes on the host.
    url = settings.database_url
    for docker_host in ("@postgres:", "@db:"):
        url = url.replace(docker_host, "@localhost:")

    engine = create_async_engine(url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def _seed_run_and_monster(session):
    """Persist a Run + Monster so Memory's FKs (run_id, monster_id) resolve.

    Returns (run_id, monster_id). Uses unique ids so repeated runs against a
    shared dev DB never collide.
    """
    from app.db.models import (
        DebateType,
        Monster,
        MonsterOwner,
        Run,
        RunStatus,
    )

    run = Run(
        id=f"itest-run-{uuid.uuid4().hex}",
        debate_topic="Is a hotdog a sandwich?",
        seed=0,
        player_x=0,
        player_y=0,
        status=RunStatus.active,
    )
    monster = Monster(
        id=f"itest-mon-{uuid.uuid4().hex}",
        run_id=run.id,
        owner=MonsterOwner.player,
        name="Socratesaur",
        type=DebateType.logos,
        persona={"tone": "measured", "tactics": ["evidence"]},
        harness={"system": "You argue with rigor."},
        skills=[],
        level=1,
        xp=0,
        max_hp=100,
        evolution_stage=0,
    )
    # Commit the parent Run first: `monster.run_id` is a plain string column,
    # not an ORM relationship, so SQLAlchemy can't infer the insert ordering
    # and may otherwise flush the Monster before the Run exists.
    session.add(run)
    await session.commit()
    session.add(monster)
    await session.commit()
    return run.id, monster.id


async def test_written_event_is_retrievable_by_query(require_db, gateway_mock):
    """A written memory is retrievable end-to-end by a semantically-matching query."""
    # Arrange: a real session + persisted FK parents (run, monster).
    from app.memory.retriever import retrieve
    from app.memory.store import write_event

    async with _host_session() as session:
        run_id, monster_id = await _seed_run_and_monster(session)
        content = (
            "The opponent claimed pineapple ruins pizza, but conceded "
            "that fruit on savory dishes is common worldwide."
        )

        # Act: write the event (summarise/embed via stubbed gateway, then persist).
        written = await write_event(
            session=session,
            monster_id=monster_id,
            run_id=run_id,
            event_type="BATTLE",
            content=content,
            salience=0.9,
        )

        # ...then retrieve it back with a query that overlaps its keywords.
        results = await retrieve(
            session=session,
            monster_id=monster_id,
            query="pineapple pizza concession",
            k=4,
        )

    # Assert: the persisted row round-trips, and the query surfaces it.
    assert written.id is not None
    assert written.monster_id == monster_id
    retrieved_ids = {r["id"] for r in results}
    assert written.id in retrieved_ids, (
        f"written memory {written.id!r} not surfaced by retrieve; "
        f"got {retrieved_ids!r}"
    )
    hit = next(r for r in results if r["id"] == written.id)
    assert hit["content"] == content
    assert hit["event_type"] == "BATTLE"


async def test_retrieve_scopes_results_to_owning_monster(require_db, gateway_mock):
    """Retrieval for one monster never leaks another monster's memories."""
    # Arrange: two distinct monsters, each with its own memory.
    from app.memory.retriever import retrieve
    from app.memory.store import write_event

    async with _host_session() as session:
        run_a, mon_a = await _seed_run_and_monster(session)
        run_b, mon_b = await _seed_run_and_monster(session)

        mem_a = await write_event(
            session=session,
            monster_id=mon_a,
            run_id=run_a,
            event_type="BATTLE",
            content="Monster A dismantled the tariff argument with trade data.",
        )
        mem_b = await write_event(
            session=session,
            monster_id=mon_b,
            run_id=run_b,
            event_type="BATTLE",
            content="Monster B leaned on emotional storytelling about lost jobs.",
        )

        # Act: query monster A with terms that match monster B's memory too.
        results_a = await retrieve(
            session=session,
            monster_id=mon_a,
            query="argument data storytelling jobs",
            k=8,
        )

    # Assert: only monster A's memory comes back; B's is fully scoped out.
    ids_a = {r["id"] for r in results_a}
    assert mem_a.id in ids_a
    assert mem_b.id not in ids_a


async def test_empty_query_returns_recent_memory_for_monster(require_db, gateway_mock):
    """An empty query falls back to recency and still returns the written event."""
    # Arrange: one monster with a single freshly-written memory.
    from app.memory.retriever import retrieve
    from app.memory.store import write_event

    async with _host_session() as session:
        run_id, monster_id = await _seed_run_and_monster(session)
        written = await write_event(
            session=session,
            monster_id=monster_id,
            run_id=run_id,
            event_type="PLAYER",
            content="The player opened with a sharp rhetorical question.",
        )

        # Act: retrieve with a blank query (recency fallback path).
        results = await retrieve(
            session=session,
            monster_id=monster_id,
            query="",
            k=4,
        )

    # Assert: the recency fallback surfaces the just-written memory.
    assert written.id in {r["id"] for r in results}
