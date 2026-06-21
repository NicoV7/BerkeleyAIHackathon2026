"""Unit tests for ``app.debate.coach.coach_argument`` (ARGUE COPILOT, Agent 8).

The player-first pivot: the lead PARTY monster COACHES the player's drafted
argument into a stronger one. The coach's quality is driven by the monster's
TRAINED genome, so a better-trained monster gives better help.

These tests are pure-logic: NO real Ollama, NO DB, NO Redis. We monkeypatch the
encounter-state loaders (``get_meta`` / ``load_combatants``) and the gateway's
``complete`` so the coaching flow runs deterministically on a bare host.

Asserted contract:
  * The coach's genome ``system_prompt`` is injected into the coaching prompt
    (the training -> better-help link).
  * A valid ``AssistResult`` with >= 1 suggestion is always returned.
  * An empty draft still yields a (from-scratch) suggestion.
  * A gateway failure degrades gracefully — no raise, still a suggestion.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from app.debate.coach import coach_argument
from app.debate.orchestrator import Combatant
from app.schemas import AssistResult


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

# A distinctive trained-genome system prompt we can grep for in the coaching
# prompt to prove the monster's training feeds the coach.
TRAINED_SYSTEM_PROMPT = "Win the framing first — define the terms before arguing."


def _coach_combatant() -> Combatant:
    """Lead party monster with a TRAINED genome (system_prompt in harness)."""
    return Combatant(
        monster_id="party-1",
        name="Socratesaur",
        type="LOGOS",
        role="party",
        hp=100,
        max_hp=100,
        level=5,
        owner="player",
        persona={"name": "Socratesaur", "tone": "incisive and confident"},
        harness={"system_prompt": TRAINED_SYSTEM_PROMPT},
        skills=[],
        model="stub-model",
    )


def _enemy_combatant() -> Combatant:
    return Combatant(
        monster_id="enemy-1",
        name="Sophist",
        type="PATHOS",
        role="enemy",
        hp=100,
        max_hp=100,
        level=4,
        owner="wild",
    )


@pytest.fixture
def patch_encounter(monkeypatch: pytest.MonkeyPatch):
    """Patch the encounter-state loaders coach_argument imports lazily.

    Returns a setter so each test can control the combatant roster.
    """
    state: dict[str, Any] = {
        "meta": {"topic": "Should AI be regulated?", "run_id": "run-1"},
        "combatants": [_coach_combatant(), _enemy_combatant()],
    }

    async def fake_get_meta(eid: str) -> dict[str, str]:
        return state["meta"]

    async def fake_load_combatants(eid: str) -> list[Combatant]:
        return state["combatants"]

    async def fake_transcript(eid: str) -> list[dict]:
        return [
            {"actor_role": "enemy", "text": "AI is impossible to regulate."},
        ]

    import app.routers.encounter as enc_mod
    import app.debate.orchestrator as orch_mod

    monkeypatch.setattr(enc_mod, "get_meta", fake_get_meta)
    monkeypatch.setattr(enc_mod, "load_combatants", fake_load_combatants)
    monkeypatch.setattr(orch_mod, "get_transcript_safe", fake_transcript)
    return state


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_coach_uses_trained_genome_system_prompt(
    patch_encounter, gateway_mock
) -> None:
    """The monster's trained genome system_prompt must appear in the coaching
    prompt — this is the training -> better-coaching link."""
    result = await coach_argument(
        session=None,
        eid="enc-1",
        draft="AI is dangerous and should be controlled.",
        skill_id=None,
    )

    assert isinstance(result, AssistResult)
    assert result.encounter_id == "enc-1"
    assert result.coach_monster_id == "party-1"
    assert len(result.suggestions) >= 1

    # The gateway saw exactly one coaching call; its system message must carry the
    # trained genome system prompt.
    assert len(gateway_mock.complete_calls) == 1
    messages = gateway_mock.complete_calls[0]["messages"]
    system_msg = next(m for m in messages if m["role"] == "system")
    assert TRAINED_SYSTEM_PROMPT in system_msg["content"]

    # The user prompt should carry the live topic, the enemy's last line, and draft.
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "Should AI be regulated?" in user_msg["content"]
    assert "impossible to regulate" in user_msg["content"]
    assert "AI is dangerous" in user_msg["content"]


@pytest.mark.asyncio
async def test_coach_returns_valid_suggestion(patch_encounter, gateway_mock) -> None:
    """A well-formed model response parses into a suggestion with improved text."""
    result = await coach_argument(None, "enc-1", "my rough draft", None)
    sug = result.suggestions[0]
    assert sug.improved.strip()  # non-empty improved argument
    assert isinstance(sug.rationale, str)
    assert isinstance(sug.angle, str)


@pytest.mark.asyncio
async def test_coach_parses_markers(patch_encounter, monkeypatch) -> None:
    """Improved / RATIONALE / ANGLE markers are split into their fields."""
    import app.gateway.gateway as gw_module

    async def fake_complete(messages, **kw) -> str:
        return (
            "Regulation is not just possible, it is overdue — every powerful "
            "technology has been governed, and AI is no exception.\n"
            "RATIONALE: Reframes 'impossible' as a historical pattern.\n"
            "ANGLE: historical precedent"
        )

    monkeypatch.setattr(gw_module.gateway, "complete", fake_complete)

    result = await coach_argument(None, "enc-1", "we should regulate AI", "logos")
    sug = result.suggestions[0]
    assert "overdue" in sug.improved
    assert "RATIONALE" not in sug.improved
    assert "ANGLE" not in sug.improved
    assert sug.rationale == "Reframes 'impossible' as a historical pattern."
    assert sug.angle == "historical precedent"
    assert sug.skill_id == "logos"


@pytest.mark.asyncio
async def test_empty_draft_still_yields_suggestion(
    patch_encounter, gateway_mock
) -> None:
    """Empty draft -> coach from scratch; still a valid suggestion."""
    result = await coach_argument(None, "enc-1", "", None)
    assert len(result.suggestions) >= 1
    assert result.suggestions[0].improved.strip()
    # The user prompt should signal a from-scratch draft.
    user_msg = next(
        m for m in gateway_mock.complete_calls[0]["messages"] if m["role"] == "user"
    )
    assert "not written anything" in user_msg["content"]


@pytest.mark.asyncio
async def test_gateway_failure_degrades_gracefully(
    patch_encounter, monkeypatch
) -> None:
    """A raising gateway must NOT propagate — fall back to a cleaned-draft tip."""
    import app.gateway.gateway as gw_module

    async def boom(messages, **kw) -> str:
        raise RuntimeError("ollama is down")

    monkeypatch.setattr(gw_module.gateway, "complete", boom)

    result = await coach_argument(None, "enc-1", "regulate AI now", None)
    assert isinstance(result, AssistResult)
    assert len(result.suggestions) >= 1
    sug = result.suggestions[0]
    assert sug.improved.strip()
    # The fallback preserves the player's draft text.
    assert "regulate AI now" in sug.improved
    # Coach-offline rationale is a generic actionable tip.
    assert "offline" in sug.rationale.lower()


@pytest.mark.asyncio
async def test_empty_draft_gateway_failure_seeds_opener(
    patch_encounter, monkeypatch
) -> None:
    """Empty draft + dead gateway: still produce a usable opener, no raise."""
    import app.gateway.gateway as gw_module

    async def boom(messages, **kw) -> str:
        raise RuntimeError("down")

    monkeypatch.setattr(gw_module.gateway, "complete", boom)

    result = await coach_argument(None, "enc-1", "", None)
    assert result.suggestions[0].improved.strip()


@pytest.mark.asyncio
async def test_missing_encounter_state_still_returns_result(
    monkeypatch, gateway_mock
) -> None:
    """If state loading fails entirely (no coach), coach_argument still returns a
    valid AssistResult with a fallback suggestion rather than raising."""
    import app.routers.encounter as enc_mod

    async def bad_meta(eid: str):
        raise RuntimeError("redis down")

    monkeypatch.setattr(enc_mod, "get_meta", bad_meta)

    result = await coach_argument(None, "enc-1", "some draft", None)
    assert isinstance(result, AssistResult)
    assert result.encounter_id == "enc-1"
    assert result.coach_monster_id is None
    assert len(result.suggestions) >= 1
