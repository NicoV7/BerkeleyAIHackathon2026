"""NPC dialogue cache tests for the Wave 2 living layer.

The cache key is intentionally tied to the NPC id, archetype, recent event tail,
and recruited figure count. These tests keep that contract host-safe by faking
Redis and the hosted LLM adapter; no network or live Redis is required.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.schemas import NPCAnchor, Region
from app.llm.hosted_adapter import STUB_RESPONSE
from app.world import event_log, figures, npcs


class _FakeRedis:
    """In-memory Redis subset for event logs and NPC string cache."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value
        if ex is not None:
            self.expiries[key] = ex

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


class _FakeAdapter:
    """Records prompts and returns a fresh deterministic line per miss."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, prompt: str, **_kwargs: Any) -> str:
        self.prompts.append(prompt)
        return f"dialogue-{len(self.prompts)}"


class _SilentAdapter:
    """Simulates a local run with no hosted NPC provider configured."""

    async def complete(self, _prompt: str, **_kwargs: Any) -> str:
        return STUB_RESPONSE


class _FailingAdapter:
    """Adapter double that simulates provider/network failure."""

    async def complete(self, prompt: str, **_kwargs: Any) -> str:
        raise RuntimeError(f"provider failed for {prompt[:12]}")


class _StubAdapter:
    """Adapter double matching the no-hosted-key production path."""

    async def complete(self, prompt: str, **_kwargs: Any) -> str:
        return STUB_RESPONSE


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(event_log, "get_redis", lambda: redis)
    monkeypatch.setattr(npcs, "get_redis", lambda: redis)
    return redis


@pytest.fixture(autouse=True)
def reset_figure_catalog() -> None:
    figures.reset_catalog_cache()
    yield
    figures.reset_catalog_cache()


def _anchor(archetype: str = "innkeeper") -> NPCAnchor:
    return NPCAnchor(
        npc_id="town_208_560__0",
        archetype=archetype,
        x=3,
        y=4,
        name="Marin the Innkeeper",
    )


def _region() -> Region:
    return Region(
        name="Aldermere",
        biome="town",
        bounds=[0, 0, 15, 11],
        lore="The capital, where every traveller is remembered.",
    )


async def test_dialogue_hits_cache_when_event_tail_and_roster_are_unchanged(
    fake_redis: _FakeRedis,
) -> None:
    adapter = _FakeAdapter()

    first = await npcs.generate_dialogue(
        "run-cache", _anchor(), _region(), adapter=adapter
    )
    second = await npcs.generate_dialogue(
        "run-cache", _anchor(), _region(), adapter=adapter
    )

    assert first.cached is False
    assert second.cached is True
    assert first.text == second.text == "dialogue-1"
    assert first.cache_key == second.cache_key
    assert len(adapter.prompts) == 1
    assert fake_redis.expiries[first.cache_key] == npcs.CACHE_TTL_S


async def test_dialogue_cache_invalidates_when_recent_event_tail_changes(
    fake_redis: _FakeRedis,
) -> None:
    adapter = _FakeAdapter()

    before = await npcs.generate_dialogue(
        "run-events", _anchor(), _region(), adapter=adapter
    )
    await event_log.append(
        "run-events", "dungeon_cleared", poi="den:784:320", name="Drystone Keep"
    )
    after = await npcs.generate_dialogue(
        "run-events", _anchor(), _region(), adapter=adapter
    )

    assert before.cache_key != after.cache_key
    assert after.cached is False
    assert after.text == "dialogue-2"
    assert len(adapter.prompts) == 2
    assert "dungeon_cleared" in adapter.prompts[-1]
    assert "Drystone Keep" in adapter.prompts[-1]


async def test_dialogue_cache_invalidates_when_recruited_figure_count_changes(
    fake_redis: _FakeRedis,
) -> None:
    adapter = _FakeAdapter()

    before = await npcs.generate_dialogue(
        "run-figures", _anchor(), _region(), adapter=adapter
    )
    assert await figures.recruit("run-figures", "socrates") is True
    after = await npcs.generate_dialogue(
        "run-figures", _anchor(), _region(), adapter=adapter
    )

    assert before.cache_key != after.cache_key
    assert after.cached is False
    assert after.text == "dialogue-2"
    assert len(adapter.prompts) == 2
    assert "Socrates" in adapter.prompts[-1]


async def test_conversation_keeps_history_and_player_message(
    fake_redis: _FakeRedis,
) -> None:
    adapter = _FakeAdapter()

    greeting = await npcs.generate_dialogue(
        "run-chat",
        _anchor("quest_giver"),
        _region(),
        conversation_id="chat-1",
        adapter=adapter,
    )
    reply = await npcs.generate_dialogue(
        "run-chat",
        _anchor("quest_giver"),
        _region(),
        player_message="What work is nearby?",
        conversation_id="chat-1",
        adapter=adapter,
    )

    assert greeting.cached is False
    assert reply.cached is False
    assert reply.conversation_id == "chat-1"
    assert [turn["role"] for turn in reply.history] == ["npc", "player", "npc"]
    assert reply.history[1]["text"] == "What work is nearby?"
    assert "Conversation so far:" in adapter.prompts[-1]
    assert "dialogue-1" in adapter.prompts[-1]
    assert "The player says: What work is nearby?" in adapter.prompts[-1]


async def test_silent_provider_falls_back_to_world_event_dialogue(
    fake_redis: _FakeRedis,
) -> None:
    await event_log.append(
        "run-offline",
        "dungeon_cleared",
        poi="den:166:532",
        name="Cellar of Bad Premises",
    )

    result = await npcs.generate_dialogue(
        "run-offline",
        _anchor("innkeeper"),
        _region(),
        player_message="Did you hear what happened?",
        conversation_id="offline-1",
        adapter=_SilentAdapter(),
    )

    assert result.text != STUB_RESPONSE
    assert "Cellar of Bad Premises" in result.text
    assert "breathe easier" in result.text


async def test_dialogue_falls_back_to_scripted_greeting_when_adapter_fails(
    fake_redis: _FakeRedis,
) -> None:
    result = await npcs.generate_dialogue(
        "run-fallback", _anchor("merchant"), _region(), adapter=_FailingAdapter()
    )

    assert result.cached is False
    assert "Marin the Innkeeper" in result.text
    assert "counter is open" in result.text
    assert fake_redis.strings[result.cache_key] == result.text


async def test_dialogue_replaces_adapter_stub_with_scripted_greeting(
    fake_redis: _FakeRedis,
) -> None:
    result = await npcs.generate_dialogue(
        "run-stub", _anchor("innkeeper"), _region(), adapter=_StubAdapter()
    )

    assert result.text != STUB_RESPONSE
    assert "Make camp here" in result.text


def test_events_tail_hash_ignores_timestamps() -> None:
    first = [event_log.Event("boss_defeated", {"boss_id": "drystone"}, ts=1.0)]
    second = [event_log.Event("boss_defeated", {"boss_id": "drystone"}, ts=99.0)]

    assert npcs.events_tail_hash(first) == npcs.events_tail_hash(second)
