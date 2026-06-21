"""Unit tests for THEME topics (Fast & Fun Battles).

Pure logic, no DB/Redis/network. Pins the contract the rest of the feature
relies on:

  * ``THEMES`` is non-empty and every theme has at least one topic.
  * ``pick_random_topic(seed, theme)`` is deterministic for a given (seed, theme)
    and returns a topic FROM that theme.
  * An unknown / empty theme falls back to the FULL catalog and NEVER raises
    (so encounter creation can't 500).
  * ``topics_for_theme`` returns in-theme topics (or full catalog on unknown).
"""
from __future__ import annotations

import pytest

from app.debate.topics import (
    THEMES,
    TOPIC_CATALOG,
    TOPICS_BY_THEME,
    pick_random_topic,
    topics_for_theme,
)


def test_themes_non_empty():
    assert THEMES, "THEMES must be non-empty"
    assert len(THEMES) >= 3
    for theme in THEMES:
        assert TOPICS_BY_THEME[theme], f"theme {theme!r} must have topics"


def test_catalog_is_union_of_themes():
    union = [t for topics in TOPICS_BY_THEME.values() for t in topics]
    assert TOPIC_CATALOG == union
    # No duplicate topics across themes.
    assert len(TOPIC_CATALOG) == len(set(TOPIC_CATALOG))


@pytest.mark.parametrize("theme", THEMES)
def test_pick_in_theme(theme):
    topic = pick_random_topic(seed=123, theme=theme)
    assert topic in TOPICS_BY_THEME[theme]


@pytest.mark.parametrize("theme", THEMES)
def test_pick_deterministic_for_seed_and_theme(theme):
    a = pick_random_topic(seed=7, theme=theme)
    b = pick_random_topic(seed=7, theme=theme)
    assert a == b


def test_pick_varies_across_seeds():
    # Across many seeds within one theme we should see more than one topic.
    results = {pick_random_topic(seed=s, theme=THEMES[0]) for s in range(50)}
    assert len(results) > 1


def test_case_insensitive_theme():
    topic = pick_random_topic(seed=1, theme=THEMES[0].lower())
    assert topic in TOPICS_BY_THEME[THEMES[0]]


@pytest.mark.parametrize("bad", ["Nonexistent", "", None, "   not a theme   "])
def test_unknown_or_empty_theme_falls_back_no_raise(bad):
    # Must not raise and must yield a topic from the full catalog.
    topic = pick_random_topic(seed=42, theme=bad)
    assert topic in TOPIC_CATALOG


def test_no_theme_uses_full_catalog():
    topic = pick_random_topic(seed=99)
    assert topic in TOPIC_CATALOG


def test_topics_for_theme():
    assert topics_for_theme(THEMES[0]) == TOPICS_BY_THEME[THEMES[0]]
    # Unknown / empty -> full catalog.
    assert topics_for_theme("nope") == TOPIC_CATALOG
    assert topics_for_theme(None) == TOPIC_CATALOG
    # Returns a copy (mutating must not corrupt the catalog).
    got = topics_for_theme(THEMES[0])
    got.append("MUTATION")
    assert "MUTATION" not in TOPICS_BY_THEME[THEMES[0]]


def test_pick_random_topic_no_seed_still_in_catalog():
    # Unseeded path uses the module-global RNG; just assert membership/no-raise.
    assert pick_random_topic() in TOPIC_CATALOG
    assert pick_random_topic(theme=THEMES[0]) in TOPICS_BY_THEME[THEMES[0]]
