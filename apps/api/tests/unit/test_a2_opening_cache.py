"""Unit tests for A2 — theme opening pre-bake makes the opening cache HIT.

Autoplan finding (A2): the cache premise "topics repeat" is FALSE — encounter.py
seeds the per-battle topic off the encounter UUID, drawing a RANDOM topic within
the run's theme each battle. So warming only the single drawn topic almost never
hits next time. What the player commits to is a THEME, and every battle in the run
draws from that theme's small topic set (topics.TOPICS_BY_THEME). Pre-baking ALL
of a theme's openings is what makes the opening cache actually hit on subsequent
battles.

These tests verify:
  1. After pregenerate_theme_openings(theme), get_or_create_opening hits (no new
     generation) for EVERY topic in that theme — i.e. whichever topic the next
     encounter draws is already warm.
  2. The pre-bake generates each topic at most once (no-op on an already-warm
     topic), and theme warming is what makes a DIFFERENT topic in the same theme
     hit (the single-topic premise would have missed).

Gateway + redis seams are faked; no network, no real Redis.
"""
from __future__ import annotations

import pytest

from app.debate import materialize as mz
from app.debate.topics import TOPICS_BY_THEME, topics_for_theme


class _FakeRedis:
    """In-memory string store implementing the get/set(ex=) surface materialize uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fr = _FakeRedis()
    monkeypatch.setattr(mz, "get_redis", lambda: fr)
    return fr


@pytest.fixture
def count_generations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every topic the gateway is asked to generate an opening for."""
    generated: list[str] = []

    async def fake_complete(messages, model=None, **k):
        # The topic appears verbatim in the system prompt; extract it for assertions.
        sys = messages[0]["content"]
        generated.append(sys)
        return "I argue AGAINST this: it collapses under one concrete question."

    monkeypatch.setattr(mz.gateway, "complete", fake_complete)
    return generated


# --------------------------------------------------------------------------- #
# 1. Pre-bake warms EVERY topic in the theme -> all subsequent draws HIT
# --------------------------------------------------------------------------- #


async def test_theme_prebake_makes_all_topics_hit(
    fake_cache: _FakeRedis, count_generations: list[str]
) -> None:
    theme = "Ethics"
    topics = topics_for_theme(theme)

    # Pre-bake the whole theme. Returns the count generated (all of them, cold).
    generated = await mz.pregenerate_theme_openings(theme)
    assert generated == len(topics)

    # Now EVERY topic in the theme is a cache hit (no further generation).
    before = len(count_generations)
    for topic_text in topics:
        text, hit = await mz.get_or_create_opening(topic_text)
        assert hit is True, f"topic {topic_text!r} should be a cache hit after pre-bake"
        assert text
    assert len(count_generations) == before, "no new generation should occur on hits"


# --------------------------------------------------------------------------- #
# 2. Single-topic warm MISSES on a sibling topic; theme warm HITS it (the fix)
# --------------------------------------------------------------------------- #


async def test_single_topic_warm_misses_sibling_but_theme_warm_hits(
    fake_cache: _FakeRedis, count_generations: list[str]
) -> None:
    theme = "Technology"
    topics = topics_for_theme(theme)
    assert len(topics) >= 2
    drawn, sibling = topics[0], topics[1]

    # Old behavior: warm only the single drawn topic.
    await mz.pregenerate_opening(drawn)

    # A sibling topic in the SAME theme is still a MISS (this is the A2 finding).
    assert await mz.get_cached_opening(sibling) is None

    # New behavior: pre-bake the theme; now the sibling HITS.
    await mz.pregenerate_theme_openings(theme)
    text, hit = await mz.get_or_create_opening(sibling)
    assert hit is True
    assert text


async def test_theme_prebake_is_idempotent(
    fake_cache: _FakeRedis, count_generations: list[str]
) -> None:
    theme = "Society"
    topics = topics_for_theme(theme)

    first = await mz.pregenerate_theme_openings(theme)
    assert first == len(topics)
    gen_after_first = len(count_generations)

    # Second call is a full no-op (every topic already warm).
    second = await mz.pregenerate_theme_openings(theme)
    assert second == 0
    assert len(count_generations) == gen_after_first, "no regeneration on a warm theme"


async def test_unknown_theme_falls_back_to_full_catalog(
    fake_cache: _FakeRedis, count_generations: list[str]
) -> None:
    """An unknown/empty theme warms the full catalog (never raises, still useful)."""
    all_topics = [t for ts in TOPICS_BY_THEME.values() for t in ts]
    generated = await mz.pregenerate_theme_openings("NoSuchTheme")
    assert generated == len(all_topics)
    # A specific topic is now warm regardless of the bogus theme name.
    assert await mz.get_cached_opening(all_topics[0]) is not None
