"""Direct route tests for the Wave 2 world living-layer endpoints."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.routers import world as world_router
from app.world import event_log, figures


class _FakeSession:
    """Minimal async session returning one run for session.get(...)."""

    def __init__(self, run: Any) -> None:
        self._run = run

    async def get(self, _model: Any, _ident: Any) -> Any:
        return self._run


class _FakeRedis:
    """In-memory Redis subset for route-level event-log tests."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value

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
            self.strings.pop(key, None)
            self.lists.pop(key, None)


class _Dialogue:
    text = "Welcome back to Aldermere."
    cached = False
    cache_key = "npctalk:test"


ALDERMERE_NEARBY_DUNGEONS = {
    "den:166:532",
    "den:236:628",
    "den:292:552",
}


def _run(seed: int = 1729):
    from app.db.models import Run, RunStatus

    return Run(
        id="living-route-run",
        debate_topic="Should memory change judgment?",
        theme=None,
        seed=seed,
        player_x=80,
        player_y=656,
        status=RunStatus.active,
    )


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(event_log, "get_redis", lambda: redis)
    return redis


@pytest.fixture(autouse=True)
def reset_figure_catalog() -> None:
    figures.reset_catalog_cache()
    yield
    figures.reset_catalog_cache()


async def test_talk_to_canonical_npc_returns_anchor_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_dialogue(run_id, anchor, region, **_kwargs):
        assert run_id == "living-route-run"
        assert anchor.name == "Marin the Innkeeper"
        assert region is not None and region.name == "Aldermere Village"
        return _Dialogue()

    monkeypatch.setattr(world_router.npcs, "generate_dialogue", fake_dialogue)

    out = await world_router.talk_to_npc(
        "living-route-run", "town_208_560__0", _FakeSession(_run())
    )

    assert out["npc_id"] == "town_208_560__0"
    assert out["name"] == "Marin the Innkeeper"
    assert out["text"] == "Welcome back to Aldermere."


async def test_talk_to_canonical_npc_accepts_conversation_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dialogue:
        text = "The cellars need a steady hand."
        cached = False
        cache_key = "npcchat:test"
        conversation_id = "thread-1"
        history = [
            {"role": "player", "text": "Any work nearby?"},
            {"role": "npc", "text": text},
        ]

    async def fake_dialogue(run_id, anchor, region, **kwargs):
        assert run_id == "living-route-run"
        assert anchor.name == "Marin the Innkeeper"
        assert region is not None and region.name == "Aldermere Village"
        assert kwargs["player_message"] == "Any work nearby?"
        assert kwargs["conversation_id"] == "thread-1"
        return Dialogue()

    monkeypatch.setattr(world_router.npcs, "generate_dialogue", fake_dialogue)

    out = await world_router.talk_to_npc(
        "living-route-run",
        "town_208_560__0",
        _FakeSession(_run()),
        world_router.NPCTalkRequest(
            message="Any work nearby?",
            conversation_id="thread-1",
        ),
    )

    assert out["conversation_id"] == "thread-1"
    assert out["history"][0]["role"] == "player"


async def test_list_figures_marks_recruited_state(fake_redis: _FakeRedis) -> None:
    assert await figures.recruit("living-route-run", "socrates") is True

    out = await world_router.list_figures(
        "living-route-run", _FakeSession(_run())
    )

    by_id = {item["id"]: item for item in out["figures"]}
    assert by_id["socrates"]["recruited"] is True
    assert by_id["curie"]["recruited"] is False


async def test_accept_quest_uses_canonical_dungeon_candidates(
    fake_redis: _FakeRedis,
) -> None:
    out = await world_router.accept_quest(
        "living-route-run",
        world_router.QuestAcceptRequest(npc_id="town_208_560__2"),
        _FakeSession(_run()),
    )

    quest = out["quest"]
    assert quest["objective"] == "clear_dungeon"
    assert quest["target"] in ALDERMERE_NEARBY_DUNGEONS
    assert "Clear" in quest["title"]


async def test_aldermere_quest_givers_spread_nearby_dungeon_targets(
    fake_redis: _FakeRedis,
) -> None:
    targets = set()
    for npc_id in ("town_208_560__2", "town_208_560__3", "town_208_560__4"):
        out = await world_router.accept_quest(
            "living-route-run",
            world_router.QuestAcceptRequest(npc_id=npc_id),
            _FakeSession(_run()),
        )
        targets.add(out["quest"]["target"])

    assert targets <= ALDERMERE_NEARBY_DUNGEONS
    assert len(targets) >= 2


async def test_accept_quest_rejects_merchants(fake_redis: _FakeRedis) -> None:
    with pytest.raises(HTTPException) as err:
        await world_router.accept_quest(
            "living-route-run",
            world_router.QuestAcceptRequest(npc_id="town_208_560__1"),
            _FakeSession(_run()),
        )

    assert err.value.status_code == 409


async def test_accept_quest_skips_cleared_nearby_dungeons(
    fake_redis: _FakeRedis,
) -> None:
    await event_log.append("living-route-run", "dungeon_cleared", poi="den:166:532")
    await event_log.append("living-route-run", "dungeon_cleared", poi="den:236:628")

    out = await world_router.accept_quest(
        "living-route-run",
        world_router.QuestAcceptRequest(npc_id="town_208_560__2"),
        _FakeSession(_run()),
    )

    assert out["quest"]["target"] == "den:292:552"


async def test_accept_quest_returns_none_when_nearby_pool_is_exhausted(
    fake_redis: _FakeRedis,
) -> None:
    for target in ALDERMERE_NEARBY_DUNGEONS:
        await event_log.append("living-route-run", "dungeon_cleared", poi=target)

    out = await world_router.accept_quest(
        "living-route-run",
        world_router.QuestAcceptRequest(npc_id="town_208_560__2"),
        _FakeSession(_run()),
    )

    assert out["quest"] is None


async def test_available_quests_marks_active_and_available_quest_givers(
    fake_redis: _FakeRedis,
) -> None:
    accepted = await world_router.accept_quest(
        "living-route-run",
        world_router.QuestAcceptRequest(npc_id="town_208_560__2"),
        _FakeSession(_run()),
    )

    out = await world_router.list_available_quest_offers(
        "living-route-run", _FakeSession(_run())
    )
    offers = {offer["npc_id"]: offer for offer in out["offers"]}

    assert offers["town_208_560__2"]["status"] == "active"
    assert offers["town_208_560__2"]["quest_id"] == accepted["quest"]["quest_id"]
    assert offers["town_208_560__3"]["status"] == "available"
    assert "town_208_560__1" not in offers


async def test_available_quests_excludes_exhausted_quest_givers(
    fake_redis: _FakeRedis,
) -> None:
    for target in ALDERMERE_NEARBY_DUNGEONS:
        await event_log.append("living-route-run", "dungeon_cleared", poi=target)

    out = await world_router.list_available_quest_offers(
        "living-route-run", _FakeSession(_run())
    )
    offers = {offer["npc_id"]: offer for offer in out["offers"]}

    assert "town_208_560__2" not in offers


async def test_clearing_active_target_removes_that_target_from_future_offers(
    fake_redis: _FakeRedis,
) -> None:
    accepted = await world_router.accept_quest(
        "living-route-run",
        world_router.QuestAcceptRequest(npc_id="town_208_560__2"),
        _FakeSession(_run()),
    )
    target = accepted["quest"]["target"]

    await world_router.append_world_event(
        "living-route-run",
        world_router.WorldEventRequest(kind="dungeon_cleared", data={"poi": target}),
        _FakeSession(_run()),
    )
    out = await world_router.accept_quest(
        "living-route-run",
        world_router.QuestAcceptRequest(npc_id="town_208_560__2"),
        _FakeSession(_run()),
    )

    assert out["quest"] is not None
    assert out["quest"]["target"] != target


async def test_world_event_completion_updates_quest_status(
    fake_redis: _FakeRedis,
) -> None:
    accepted = await world_router.accept_quest(
        "living-route-run",
        world_router.QuestAcceptRequest(npc_id="town_208_560__2"),
        _FakeSession(_run()),
    )
    target = accepted["quest"]["target"]

    completed = await world_router.append_world_event(
        "living-route-run",
        world_router.WorldEventRequest(
            kind="dungeon_cleared", data={"poi": target, "name": "Drystone Keep"}
        ),
        _FakeSession(_run()),
    )
    listed = await world_router.list_run_quests(
        "living-route-run", _FakeSession(_run())
    )

    assert completed["completed_quests"] == [accepted["quest"]["quest_id"]]
    assert listed["quests"][0]["status"] == "completed"


async def test_summon_requires_recruitment_then_returns_voice_prompt(
    fake_redis: _FakeRedis,
) -> None:
    with pytest.raises(HTTPException) as err:
        await world_router.summon_figure(
            "living-route-run",
            world_router.SummonRequest(figure_id="socrates", battle_state={}),
            _FakeSession(_run()),
        )
    assert err.value.status_code == 409

    await world_router.append_world_event(
        "living-route-run",
        world_router.WorldEventRequest(
            kind="figure_recruited", data={"figure_id": "socrates"}
        ),
        _FakeSession(_run()),
    )
    out = await world_router.summon_figure(
        "living-route-run",
        world_router.SummonRequest(
            figure_id="socrates", battle_state={"topic": "knowledge is virtue"}
        ),
        _FakeSession(_run()),
    )

    assert out["summoned"] is True
    assert out["figure"]["id"] == "socrates"
    assert "knowledge is virtue" in out["turn_prompt"]


async def test_profile_reflects_logged_world_events(fake_redis: _FakeRedis) -> None:
    await event_log.append("living-route-run", "dungeon_cleared", poi="den:784:320")
    await event_log.append("living-route-run", "boss_defeated", boss_id="drystone")
    await event_log.append("living-route-run", "fallacy_flagged", fallacy="strawman")
    await figures.recruit("living-route-run", "curie")

    out = await world_router.get_profile(
        "living-route-run", _FakeSession(_run())
    )

    profile = out["profile"]
    assert profile["dungeons_cleared"] == 1
    assert profile["bosses_defeated"] == 1
    assert profile["weakness"] == ["strawman"]
    assert profile["alignment"] == {"evidential": 1}
