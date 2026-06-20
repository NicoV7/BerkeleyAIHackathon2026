"""Tests for the WS-1 judge explanation fields (why/logic/persuasion).

Covers app.debate.judge.score_round + heuristic_score:
  * JudgeScore now carries why/logic/persuasion (additive fields).
  * The heuristic fallback fills a non-empty `why` and numeric logic/persuasion
    when the gateway is unavailable (gateway.complete raises -> fallback path).

No DB/network needed: gateway.complete is monkeypatched. Coroutines are driven
with asyncio.run() so these tests do not depend on pytest-asyncio being present.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from app.debate import judge as judge_mod
from app.debate.judge import JudgeScore, heuristic_score, score_round


# --------------------------------------------------------------------------- #
# JudgeScore dataclass carries the new additive fields
# --------------------------------------------------------------------------- #
def test_judge_score_has_why_logic_persuasion_fields():
    field_names = {f.name for f in dataclasses.fields(JudgeScore)}
    assert {"why", "logic", "persuasion"}.issubset(field_names)
    # Backward-compatible originals still present.
    assert {"actor_id", "score", "rationale"}.issubset(field_names)


def test_judge_score_instance_exposes_new_fields():
    js = JudgeScore(actor_id="a", score=72.0, rationale="ok")
    # Defaults keep old call sites working.
    assert js.why == ""
    assert js.logic == 0.0
    assert js.persuasion == 0.0
    # And the fields are settable / readable.
    js2 = JudgeScore(
        actor_id="b",
        score=80.0,
        rationale="strong",
        why="It reframed the burden of proof.",
        logic=85.0,
        persuasion=78.0,
    )
    assert js2.why == "It reframed the burden of proof."
    assert js2.logic == 85.0
    assert js2.persuasion == 78.0


# --------------------------------------------------------------------------- #
# heuristic_score: deterministic numeric fallback
# --------------------------------------------------------------------------- #
def test_heuristic_score_returns_numeric_in_range():
    s = heuristic_score(
        "Should cities ban cars downtown?",
        "Cities should ban cars downtown because it reduces pollution and "
        "improves public transit and pedestrian safety substantially.",
    )
    assert isinstance(s, float)
    assert 0.0 <= s <= 100.0


def test_heuristic_score_empty_text_is_low():
    assert heuristic_score("any topic", "") == 20.0
    assert heuristic_score("any topic", "   ") == 20.0


def test_heuristic_score_on_topic_beats_off_topic():
    topic = "Should cities ban cars downtown?"
    on_topic = (
        "Cities should ban cars downtown to cut pollution and free road space "
        "for pedestrians and transit."
    )
    off_topic = "Bananas are yellow and grow on trees in warm climates."
    assert heuristic_score(topic, on_topic) > heuristic_score(topic, off_topic)


# --------------------------------------------------------------------------- #
# score_round heuristic fallback path: gateway.complete raises
# --------------------------------------------------------------------------- #
def _raise_complete(*_args, **_kwargs):
    raise RuntimeError("gateway down (simulated)")


def test_score_round_fallback_fills_why_and_dims(monkeypatch):
    """When gateway.complete raises, every JudgeScore must still carry a
    non-empty `why` and numeric logic/persuasion (heuristic fallback)."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("gateway down (simulated)")

    monkeypatch.setattr(judge_mod.gateway, "complete", _boom)

    items = [
        {"actor_id": "alpha", "text": "Renewables are now cheaper than coal and scale fast."},
        {"actor_id": "beta", "text": "Coal still provides reliable baseload power at night."},
    ]
    results = asyncio.run(score_round("Should we phase out coal?", items))

    assert len(results) == len(items)
    for r, it in zip(results, items):
        assert isinstance(r, JudgeScore)
        assert r.actor_id == it["actor_id"]
        # Heuristic fills a non-empty explanation — demo never shows a blank.
        assert isinstance(r.why, str)
        assert r.why.strip() != ""
        # logic/persuasion are numeric and in-range (not the 0.0 sentinel only).
        assert isinstance(r.logic, float)
        assert isinstance(r.persuasion, float)
        assert 0.0 <= r.logic <= 100.0
        assert 0.0 <= r.persuasion <= 100.0
        # score is the heuristic value, numeric and in range.
        assert isinstance(r.score, float)
        assert 0.0 <= r.score <= 100.0


def test_score_round_fallback_dims_match_heuristic_score(monkeypatch):
    """On the pure-fallback path the dims default to the heuristic score."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("gateway down (simulated)")

    monkeypatch.setattr(judge_mod.gateway, "complete", _boom)

    topic = "Should remote work be the default?"
    items = [{"actor_id": "solo", "text": "Remote work boosts focus and cuts commute waste."}]
    results = asyncio.run(score_round(topic, items))

    r = results[0]
    expected = heuristic_score(topic, items[0]["text"])
    assert r.score == expected
    assert r.logic == expected
    assert r.persuasion == expected
    assert r.why.strip() != ""


def test_score_round_empty_items_returns_empty(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("should not be called")

    monkeypatch.setattr(judge_mod.gateway, "complete", _boom)
    assert asyncio.run(score_round("topic", [])) == []


# --------------------------------------------------------------------------- #
# score_round honors model-provided why/logic/persuasion (non-fallback path)
# --------------------------------------------------------------------------- #
def test_score_round_uses_model_provided_fields(monkeypatch):
    payload = (
        '{"alpha": {"score": 88, "why": "It reframed the burden of proof.", '
        '"logic": 90, "persuasion": 84, "rationale": "Tight reasoning."}}'
    )

    async def _ok(*args, **kwargs):
        return payload

    monkeypatch.setattr(judge_mod.gateway, "complete", _ok)

    items = [{"actor_id": "alpha", "text": "A well-reasoned argument here."}]
    results = asyncio.run(score_round("A topic", items))

    r = results[0]
    assert r.score == 88.0
    assert r.why == "It reframed the burden of proof."
    assert r.logic == 90.0
    assert r.persuasion == 84.0
    assert r.rationale == "Tight reasoning."


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
