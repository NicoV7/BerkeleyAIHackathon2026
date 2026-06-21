"""WS-A backend unit — run save / resume persistence.

Covers app.routers.runs without requiring a live Postgres:

  * Pure serialization/mapping helpers (``_iso``, ``_split_party_captured``,
    ``_run_resume_state``) are exercised against transient (unsaved) ORM rows so
    collection + execution are always green on a bare host. These guard the
    RunResumeState / RunSaveResult contract shape and the ``resumable`` marker
    semantics (True iff ``saved_at`` is set).

  * A DB-backed round-trip (save -> stamp -> resume) is included but gated behind
    the ``require_db`` fixture, so it *skips* (never errors) without a live stack.

No Postgres, no Redis, no network for the pure-logic tests.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime

import pytest

# Import-time touches only pure Python + the frozen schemas/models; if the impl
# hasn't landed, skip the whole file rather than erroring collection.
runs = pytest.importorskip("app.routers.runs")

from app.db.models import DebateType, Monster, MonsterOwner, Run, RunStatus  # noqa: E402
from app.schemas import RunResumeState, RunSaveResult  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures / factories (transient ORM rows — no DB)
# --------------------------------------------------------------------------- #
def _make_run(**overrides) -> Run:
    defaults = dict(
        id="run-1",
        debate_topic="Should pineapple go on pizza?",
        seed=7,
        player_x=4,
        player_y=9,
        status=RunStatus.active,
    )
    defaults.update(overrides)
    return Run(**defaults)


def _make_monster(*, owner=MonsterOwner.player, created_at=None, **overrides) -> Monster:
    defaults = dict(
        run_id="run-1",
        owner=owner,
        name="LogiKnight",
        type=DebateType.logos,
        persona={},
        harness={},
        skills=[{"name": "Logical Thrust", "type": "LOGOS"}],
        level=3,
        xp=120,
        max_hp=130,
        evolution_stage=1,
    )
    defaults.update(overrides)
    m = Monster(**defaults)
    if created_at is not None:
        m.created_at = created_at
    return m


# --------------------------------------------------------------------------- #
# _iso — datetime serialization
# --------------------------------------------------------------------------- #
def test_iso_returns_none_for_none() -> None:
    assert runs._iso(None) is None


def test_iso_serializes_datetime_to_isoformat() -> None:
    dt = datetime(2026, 6, 20, 12, 30, 45)
    assert runs._iso(dt) == "2026-06-20T12:30:45"


# --------------------------------------------------------------------------- #
# _split_party_captured — player roster projection & ordering
# --------------------------------------------------------------------------- #
def test_split_party_captured_excludes_non_player_owners() -> None:
    # Arrange — a mix of player + wild monsters.
    player = _make_monster(owner=MonsterOwner.player, id="p1")
    wild = _make_monster(owner=MonsterOwner.wild, id="w1", name="Stumpit")
    # Act
    party, captured = runs._split_party_captured([player, wild])
    # Assert — only the player-owned monster appears.
    assert [s.id for s in party] == ["p1"]
    assert [s.id for s in captured] == ["p1"]
    assert all(s.owner == "player" for s in party)


def test_split_party_captured_maps_all_summary_fields() -> None:
    # Arrange
    m = _make_monster(
        id="p9", name="EthosGuard", level=5, xp=200, max_hp=150, evolution_stage=2
    )
    # Act
    party, _ = runs._split_party_captured([m])
    # Assert — full MonsterSummary projection is faithful.
    s = party[0]
    assert (s.id, s.name, s.level, s.xp, s.max_hp, s.evolution_stage) == (
        "p9",
        "EthosGuard",
        5,
        200,
        150,
        2,
    )
    assert s.type == "LOGOS"
    assert s.skills == [{"name": "Logical Thrust", "type": "LOGOS"}]


def test_split_party_captured_orders_by_created_at_then_id() -> None:
    # Arrange — later-captured monster has a newer created_at.
    starter = _make_monster(id="starter", created_at=datetime(2026, 1, 1))
    later = _make_monster(id="later", created_at=datetime(2026, 2, 1))
    # Act — pass out of order; helper must sort deterministically.
    party, captured = runs._split_party_captured([later, starter])
    # Assert
    assert [s.id for s in party] == ["starter", "later"]
    assert [s.id for s in captured] == ["starter", "later"]


def test_split_party_captured_empty_when_no_player_monsters() -> None:
    wild = _make_monster(owner=MonsterOwner.wild, id="w1")
    party, captured = runs._split_party_captured([wild])
    assert party == []
    assert captured == []


# --------------------------------------------------------------------------- #
# _run_resume_state — assembled contract + resumable marker
# --------------------------------------------------------------------------- #
def test_run_resume_state_carries_run_fields() -> None:
    # Arrange
    run = _make_run(player_x=4, player_y=9, status=RunStatus.active)
    mons = [_make_monster(id="p1")]
    # Act
    state = runs._run_resume_state(run, mons, saved_at=None)
    # Assert — shape + field passthrough.
    assert isinstance(state, RunResumeState)
    assert state.id == "run-1"
    assert state.debate_topic == "Should pineapple go on pizza?"
    assert state.player_x == 4
    assert state.player_y == 9
    assert state.status == "active"
    assert [s.id for s in state.party] == ["p1"]


def test_run_resume_state_not_resumable_when_saved_at_unset() -> None:
    # Arrange / Act — never saved.
    state = runs._run_resume_state(_make_run(), [], saved_at=None)
    # Assert
    assert state.resumable is False
    assert state.saved_at is None


def test_run_resume_state_resumable_when_saved_at_set() -> None:
    # Arrange
    saved = datetime(2026, 6, 20, 8, 0, 0)
    # Act
    state = runs._run_resume_state(_make_run(), [_make_monster(id="p1")], saved_at=saved)
    # Assert — resume marker flips on and the timestamp is serialized.
    assert state.resumable is True
    assert state.saved_at == "2026-06-20T08:00:00"


def test_run_resume_state_ended_status_serializes_enum_value() -> None:
    run = _make_run(status=RunStatus.ended)
    state = runs._run_resume_state(run, [], saved_at=None)
    assert state.status == "ended"


# --------------------------------------------------------------------------- #
# RunSaveResult contract shape (pure schema check)
# --------------------------------------------------------------------------- #
def test_run_save_result_shape() -> None:
    # Arrange / Act
    ts = datetime(2026, 6, 20, 8, 0, 0).isoformat()
    result = RunSaveResult(run_id="run-1", saved=True, saved_at=ts, party_size=3)
    # Assert
    assert result.run_id == "run-1"
    assert result.saved is True
    assert result.saved_at == ts
    assert result.party_size == 3


# --------------------------------------------------------------------------- #
# DB-backed round-trip — gated; SKIPS (never errors) without a live stack
# --------------------------------------------------------------------------- #
#
# These use a host-reachable engine (the app's SessionLocal targets the
# Docker-internal `postgres` host, which doesn't resolve from the test host).
# We mirror the conftest `require_db` probe's localhost rewrite so "the probe
# says reachable" and "this session connects" are the same condition.


@contextlib.asynccontextmanager
async def _host_session():
    """Yield an AsyncSession bound to the host-reachable DATABASE_URL.

    Also runs the idempotent `runs.saved_at` ALTER (the same statement
    app.db.session.init_db applies) so the column exists on a fresh DB without
    booting the whole app. Engine is per-use and disposed afterwards.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlmodel import SQLModel

    from app.config import settings
    from app.db import models  # noqa: F401  (registers tables on metadata)

    url = settings.database_url
    for docker_host in ("@postgres:", "@db:"):
        url = url.replace(docker_host, "@localhost:")

    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.execute(
            text("ALTER TABLE runs ADD COLUMN IF NOT EXISTS saved_at TIMESTAMP")
        )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


def test_save_then_resume_roundtrip_persists(require_db) -> None:  # noqa: ANN001
    """Create -> save -> resume against a real DB; party + saved marker survive."""

    async def _scenario() -> tuple[RunSaveResult, RunResumeState]:
        run_id = f"itest-run-{uuid.uuid4().hex}"
        async with _host_session() as session:
            run = Run(
                id=run_id,
                debate_topic="Resume me",
                seed=11,
                player_x=2,
                player_y=3,
                status=RunStatus.active,
            )
            session.add(run)
            await session.commit()  # run must exist before the monster FK
            # A player-owned monster so the party is non-empty on resume.
            mon = Monster(
                id=f"itest-mon-{uuid.uuid4().hex}",
                run_id=run_id,
                owner=MonsterOwner.player,
                name="LogiKnight",
                type=DebateType.logos,
                persona={},
                harness={},
                skills=[],
            )
            session.add(mon)
            await session.commit()

            # Before saving: resumable is False.
            pre = await runs.get_run(run_id, session)
            assert pre.resumable is False
            assert pre.saved_at is None
            assert len(pre.party) == 1

            save = await runs.save_run(run_id, session)

        # After saving in a fresh session/engine: marker + party survive.
        async with _host_session() as s2:
            post = await runs.get_run(run_id, s2)
        return save, post

    save, post = asyncio.run(_scenario())
    assert save.saved is True
    assert save.party_size == 1
    assert post.resumable is True
    assert post.saved_at is not None
    assert len(post.party) == 1


def test_get_run_missing_raises_404(require_db) -> None:  # noqa: ANN001
    from fastapi import HTTPException

    async def _scenario() -> None:
        async with _host_session() as session:
            with pytest.raises(HTTPException) as exc:
                await runs.get_run("does-not-exist", session)
            assert exc.value.status_code == 404

    asyncio.run(_scenario())


def test_get_run_party_returns_player_monsters(monkeypatch) -> None:
    """The UI HUD/party/training screens call GET /runs/{id}/party."""

    class FakeSession:
        async def get(self, _model, run_id: str) -> Run:
            return _make_run(id=run_id)

    async def fake_player_monsters(_session, run_id: str) -> list[Monster]:
        return [_make_monster(id="p1", run_id=run_id)]

    monkeypatch.setattr(runs, "_player_monsters", fake_player_monsters)

    party = asyncio.run(runs.get_run_party("run-1", FakeSession()))

    assert [m.id for m in party] == ["p1"]
    assert all(m.owner == "player" for m in party)


def test_get_run_party_missing_run_raises_404() -> None:
    from fastapi import HTTPException

    class FakeSession:
        async def get(self, _model, _run_id: str) -> None:
            return None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(runs.get_run_party("missing", FakeSession()))

    assert exc.value.status_code == 404
