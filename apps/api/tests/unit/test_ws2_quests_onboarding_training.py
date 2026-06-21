"""WS-2 backend tests — new quest types, empty-start first-pull, quick-train.

Three feature areas, each with a focused test surface:

  Quests (pure / fakeredis): each new objective (hunt_enemy, find_item,
  debate_npc) fires its world event, completes the matching quest, and records
  the reward_spec on the completion event (event -> completion -> reward).

  Onboarding (DB-gated): a NEW run starts with NO party agents (empty_start
  gate), POST /onboarding/first-pull grants exactly ONE agent, and a second call
  is idempotent (no double-grant).

  Quick-train (DB-gated + gateway_mock): consumes a training item and bumps the
  monster's stats DIRECTLY with ZERO LLM calls (assert gateway call count 0).

DB-backed tests skip cleanly without a host-reachable Postgres (require_db).
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.world import event_log, quests


# --------------------------------------------------------------------------- #
# Fake Redis + session for pure quest-completion tests (no DB / no network)
# --------------------------------------------------------------------------- #


class _FakeRedis:
    """In-memory Redis subset for event-log-backed quest tests."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(key, [])
        if start < 0:
            start = max(len(values) + start, 0)
        if end < 0:
            end = len(values) + end
        return values[start:end + 1]

    async def ltrim(self, key: str, start: int, end: int) -> None:
        values = self.lists.get(key, [])
        if start < 0:
            start = max(len(values) + start, 0)
        if end < 0:
            end = len(values) + end
        self.lists[key] = values[start:end + 1]

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.lists.pop(key, None)


class _NoOpSession:
    """Async session whose SQL methods are no-ops (reward payout is best-effort)."""

    def __init__(self, run: Any) -> None:
        self._run = run

    async def get(self, _model: Any, _ident: Any) -> Any:
        return self._run

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no DB in pure quest test")  # forces payout to skip

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _run():
    from app.db.models import Run, RunStatus

    return Run(
        id="ws2-quest-run",
        debate_topic="Should AI debate?",
        seed=7,
        player_x=1,
        player_y=1,
        status=RunStatus.active,
    )


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(event_log, "get_redis", lambda: redis)
    return redis


# --------------------------------------------------------------------------- #
# Quest completion — one test per new objective
# --------------------------------------------------------------------------- #


async def test_hunt_enemy_quest_completes_on_enemy_killed(fake_redis: _FakeRedis) -> None:
    rid = "ws2-quest-run"
    quest = await quests.offer_typed_quest(
        rid, "npc_hunter", "hunt_enemy", "Goblin", target_xy={"x": 5, "y": 6}
    )
    assert quest.objective == "hunt_enemy"
    assert quest.target_xy == {"x": 5, "y": 6}
    assert quest.reward_spec.get("coins", 0) > 0

    completed = await quests.maybe_complete_quests(rid, "enemy_killed", enemy_kind="Goblin")
    assert completed == [quest.quest_id]

    # Reward spec is recorded on the completion event for payout.
    specs = await quests.completed_reward_specs(rid, completed)
    assert specs[quest.quest_id]["coins"] == quest.reward_spec["coins"]

    # Idempotent: a second matching event does not re-complete.
    again = await quests.maybe_complete_quests(rid, "enemy_killed", enemy_kind="Goblin")
    assert again == []


async def test_hunt_enemy_quest_completes_on_monster_id(fake_redis: _FakeRedis) -> None:
    rid = "ws2-quest-run"
    quest = await quests.offer_typed_quest(rid, "npc_hunter", "hunt_enemy", "mon-123")
    completed = await quests.maybe_complete_quests(
        rid, "enemy_killed", enemy_kind="Slime", monster_id="mon-123"
    )
    assert completed == [quest.quest_id]


async def test_find_item_quest_completes_on_item_found(fake_redis: _FakeRedis) -> None:
    rid = "ws2-quest-run"
    quest = await quests.offer_typed_quest(rid, "npc_collector", "find_item", "potion_hp_small")
    completed = await quests.maybe_complete_quests(
        rid, "item_found", item_key="potion_hp_small"
    )
    assert completed == [quest.quest_id]
    # Wrong item does not complete it.
    rid2 = "ws2-quest-run-2"
    q2 = await quests.offer_typed_quest(rid2, "npc_collector", "find_item", "camp_token")
    assert await quests.maybe_complete_quests(rid2, "item_found", item_key="potion_mp_small") == []
    assert q2.objective == "find_item"


async def test_debate_npc_quest_completes_on_npc_debated(fake_redis: _FakeRedis) -> None:
    rid = "ws2-quest-run"
    quest = await quests.offer_typed_quest(rid, "npc_sage", "debate_npc", "rival_001")
    completed = await quests.maybe_complete_quests(rid, "npc_debated", npc_id="rival_001")
    assert completed == [quest.quest_id]


async def test_list_quests_exposes_pin_and_status(fake_redis: _FakeRedis) -> None:
    rid = "ws2-quest-run"
    quest = await quests.offer_typed_quest(
        rid, "npc_hunter", "hunt_enemy", "Goblin", target_xy={"x": 9, "y": 2}
    )
    listed = await quests.list_quests(rid)
    assert listed[0]["status"] == "accepted"
    assert listed[0]["target_xy"] == {"x": 9, "y": 2}
    assert listed[0]["title"]

    await quests.maybe_complete_quests(rid, "enemy_killed", enemy_kind="Goblin")
    listed2 = await quests.list_quests(rid)
    assert listed2[0]["status"] == "completed"
    assert listed2[0]["quest_id"] == quest.quest_id


async def test_world_router_event_completes_and_keeps_clear_dungeon(
    fake_redis: _FakeRedis,
) -> None:
    """The world router append-event path completes new-type quests too."""
    from app.routers import world as world_router

    rid = "ws2-quest-run"
    quest = await quests.offer_typed_quest(rid, "npc_hunter", "hunt_enemy", "Wraith")

    out = await world_router.append_world_event(
        rid,
        world_router.WorldEventRequest(kind="enemy_killed", data={"enemy_kind": "Wraith"}),
        _NoOpSession(_run()),
    )
    assert out["completed_quests"] == [quest.quest_id]
    # Payout is best-effort; the no-DB session forces it to skip without raising.
    assert out["rewards"] == {}


# --------------------------------------------------------------------------- #
# DB-backed: empty-start + first-pull (onboarding) and quick-train
# --------------------------------------------------------------------------- #


def _localhost_url() -> str:
    from app.config import settings

    url = settings.database_url
    for docker_host in ("@postgres:", "@db:"):
        url = url.replace(docker_host, "@localhost:")
    return url


@pytest.mark.usefixtures("require_db")
def test_empty_start_then_first_pull_grants_one_agent(require_db, monkeypatch) -> None:  # noqa: ARG001
    """A new run starts EMPTY; first-pull grants exactly one agent (idempotent)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    import app.db.session as db_session
    from app.db.models import Monster, MonsterOwner
    from app.party.personas_seed import upsert_personas
    from app.routers import map as map_router
    from app.routers import onboarding
    from app.schemas import CreateRunRequest

    async def _go() -> None:
        eng = create_async_engine(_localhost_url(), poolclass=NullPool)
        maker = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(db_session, "engine", eng)
        monkeypatch.setattr(db_session, "SessionLocal", maker)
        # Ensure the empty-start gate is ON for this test regardless of env.
        from app.config import settings
        monkeypatch.setattr(settings, "empty_start_enabled", True)
        try:
            await db_session.init_db()
            async with maker() as s:
                await upsert_personas(s)

            # 1) Create a run: it must start with NO party agents.
            async with maker() as s:
                run_state = await map_router.create_run(
                    CreateRunRequest(topic="t", seed=3), s
                )
            assert run_state.party == []
            run_id = run_state.id

            async with maker() as s:
                res = await s.execute(
                    select(Monster).where(
                        Monster.run_id == run_id,
                        Monster.owner == MonsterOwner.player,
                    )
                )
                assert list(res.scalars().all()) == []

            # 2) First pull grants exactly ONE agent.
            async with maker() as s:
                out = await onboarding.first_pull(
                    run_id, onboarding.FirstPullRequest(seed=11), s
                )
            assert out.granted is True
            assert out.party_size == 1

            async with maker() as s:
                res = await s.execute(
                    select(Monster).where(
                        Monster.run_id == run_id,
                        Monster.owner == MonsterOwner.player,
                    )
                )
                mons = list(res.scalars().all())
            assert len(mons) == 1
            first_id = mons[0].id

            # 3) Idempotent: a second first-pull does NOT create a second agent.
            async with maker() as s:
                out2 = await onboarding.first_pull(
                    run_id, onboarding.FirstPullRequest(seed=11), s
                )
            assert out2.granted is False
            assert out2.party_size == 1
            assert out2.monster.id == first_id
        finally:
            await eng.dispose()

    asyncio.run(_go())


@pytest.mark.usefixtures("require_db")
def test_quick_train_consumes_item_and_bumps_stats_zero_llm(
    require_db, monkeypatch, gateway_stub  # noqa: ARG001
) -> None:
    """Quick-train consumes a training item + bumps stats with ZERO LLM calls."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    import app.db.session as db_session
    import app.gateway.gateway as gw_module
    from app.db.models import DebateType, Monster, MonsterOwner, Run
    from app.economy.catalog import seed_economy
    from app.routers import training
    from app.schemas import QuickTrainRequest

    # Hard-assert ZERO LLM: any gateway call raises, and we also count via stub.
    monkeypatch.setattr(gw_module.gateway, "complete", gateway_stub.complete)
    monkeypatch.setattr(gw_module.gateway, "stream", gateway_stub.stream)
    monkeypatch.setattr(gw_module.gateway, "embed", gateway_stub.embed)

    async def _go() -> None:
        eng = create_async_engine(_localhost_url(), poolclass=NullPool)
        maker = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(db_session, "engine", eng)
        monkeypatch.setattr(db_session, "SessionLocal", maker)
        try:
            await db_session.init_db()
            async with maker() as s:
                await seed_economy(s)

            run_id = str(uuid.uuid4())
            async with maker() as s:
                s.add(Run(id=run_id, debate_topic="t", player_name="P", coins=0))
                await s.commit()

            # A player monster with known baseline stats.
            mon_id = str(uuid.uuid4())
            async with maker() as s:
                s.add(
                    Monster(
                        id=mon_id,
                        run_id=run_id,
                        owner=MonsterOwner.player,
                        name="Trainee",
                        type=DebateType.logos,
                        atk=10,
                        def_=10,
                        mp=50,
                        max_mp=50,
                    )
                )
                await s.commit()

            # Grant one training_atk item to the run's inventory.
            from app.economy.award import grant_item
            async with maker() as s:
                await grant_item(s, run_id, "training_atk", 1)
                await s.commit()

            # Quick-train: ATK should go 10 -> 13 (catalog effect {"atk": 3}).
            async with maker() as s:
                res = await training.quick_train(
                    QuickTrainRequest(monster_id=mon_id, item_key="training_atk"), s
                )
            assert res.applied == {"atk": 3}
            assert res.stats["atk"] == 13
            assert res.remaining_qty == 0

            # Durable: the monster row really moved.
            async with maker() as s:
                m = await s.get(Monster, mon_id)
                assert m.atk == 13

            # Re-using with no inventory left -> 409 (atomic consume rejects).
            with pytest.raises(Exception):
                async with maker() as s:
                    await training.quick_train(
                        QuickTrainRequest(monster_id=mon_id, item_key="training_atk"), s
                    )
        finally:
            await eng.dispose()

    asyncio.run(_go())

    # ZERO LLM calls happened anywhere in quick-train.
    assert gateway_stub.complete_calls == []
    assert gateway_stub.stream_calls == []
    assert gateway_stub.embed_calls == []
