"""Unit tests for the Memory Recall endpoint (Wave C: the headline ability).

`POST /api/encounters/{eid}/memory-recall` peeks the shared Redis transcript,
quotes the enemy line that hit hardest back at them, and counters in the lead
party monster's persona voice. These tests exercise the router HANDLER
directly (no FastAPI client / no real Redis / no real Ollama):

  * Happy path: well-formed transcript + working gateway -> MemoryRecallResult
    with mp_spent=60, populated highlighted_line/counter_text/transcript_slice,
    and damage > 0 from the Wave-0-extended compute_damage.
  * Cache miss path: empty transcript -> graceful fallback with mp_spent=30
    (half-MP refund), damage=0, and a generic counter that never 500s.
  * MP gate: a coach with mp_before < 60 -> HTTP 400, no gateway call.
  * Model failure: gateway raises -> graceful fallback (damage=0, refund 30).
"""
from __future__ import annotations

from typing import Any

import pytest

from app.debate.orchestrator import Combatant
from app.routers import debate as debate_router
from app.schemas import MemoryRecallResult


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


def _coach_combatant() -> Combatant:
    """Lead party monster with gacha-wave stats (atk/def_/mp/max_mp/domain)."""
    coach = Combatant(
        monster_id="party-1",
        name="Socratesaur",
        type="LOGOS",
        role="party",
        hp=100,
        max_hp=100,
        level=5,
        owner="player",
        persona={"voice": "I know that I know nothing — so does anyone here?", "tagline": "tag"},
        harness={},
        skills=[],
        model="stub-model",
    )
    # Combatant is a dataclass-like; attach the gacha stats via attribute set so
    # the router's `getattr(coach, 'atk', ...)` calls find them.
    coach.atk = 14  # type: ignore[attr-defined]
    coach.def_ = 11  # type: ignore[attr-defined]
    coach.max_mp = 60  # type: ignore[attr-defined]
    coach.domain = "PHILOSOPHY"  # type: ignore[attr-defined]
    return coach


def _enemy_combatant() -> Combatant:
    enemy = Combatant(
        monster_id="enemy-1",
        name="Pedant",
        type="PATHOS",  # LOGOS->PATHOS is super-effective (1.5) in the type chart
        role="enemy",
        hp=100,
        max_hp=100,
        level=4,
        owner="wild",
        persona={},
    )
    enemy.atk = 10  # type: ignore[attr-defined]
    enemy.def_ = 8  # type: ignore[attr-defined]
    enemy.max_mp = 50  # type: ignore[attr-defined]
    enemy.domain = "GENERAL"  # type: ignore[attr-defined]
    return enemy


def _populated_transcript() -> list[dict]:
    """A 4-line transcript with one party utterance and three enemy lines, so
    `_pick_highlighted_line` has a real "most-recent enemy line" to surface."""
    return [
        {"turn": 1, "actor_id": "party-1", "actor_role": "party",
         "skill_used": None, "text": "Pineapple complements salty pizza.", "ts": 1.0},
        {"turn": 1, "actor_id": "enemy-1", "actor_role": "enemy",
         "skill_used": None, "text": "Pineapple has no place on pizza.", "ts": 1.1},
        {"turn": 2, "actor_id": "party-1", "actor_role": "party",
         "skill_used": None, "text": "Hawaii proves you wrong.", "ts": 2.0},
        {"turn": 2, "actor_id": "enemy-1", "actor_role": "enemy",
         "skill_used": None, "text": "Italians would riot at the suggestion.", "ts": 2.1},
    ]


class _FakeRedis:
    """Tiny stand-in for `app.redis_state.get_redis()` covering only the calls
    the Memory Recall endpoint makes (lrange against `k_judge(eid)`)."""

    def __init__(self, judge_blobs: list[str] | None = None) -> None:
        self._judge_blobs = list(judge_blobs or [])

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        # The endpoint only lranges k_judge — return whatever the test set up.
        return list(self._judge_blobs)


@pytest.fixture
def patch_router(monkeypatch: pytest.MonkeyPatch):
    """Patch every external touch-point the router does: Redis (meta/transcript/
    judge/mp/hp/set_mp/set_hp/append_utterance) and the combatant loader.

    Returns the mutable state dict so each test can tweak transcript/mp/etc.
    """
    state: dict[str, Any] = {
        "meta": {
            "topic": "Pineapple belongs on pizza.",
            "run_id": "run-1",
            "turn_no": 2,
            "phase": "debating",
        },
        "combatants": [_coach_combatant(), _enemy_combatant()],
        "transcript": _populated_transcript(),
        "mp_map": {},  # empty -> endpoint falls back to coach.max_mp (60)
        "judge_blobs": [],
        # Sinks for verifying the side-effects.
        "set_mp_calls": [],
        "set_hp_calls": [],
        "appended": [],
    }

    async def fake_get_meta(eid: str) -> dict[str, str]:
        return {k: str(v) for k, v in state["meta"].items()}

    async def fake_load_combatants(eid: str) -> list[Combatant]:
        return state["combatants"]

    async def fake_get_transcript(eid: str) -> list[dict]:
        return list(state["transcript"])

    async def fake_get_mp_map(eid: str) -> dict[str, int]:
        return dict(state["mp_map"])

    async def fake_set_mp(eid: str, monster_id: str, mp: int) -> None:
        state["set_mp_calls"].append((eid, monster_id, mp))

    async def fake_set_hp(eid: str, monster_id: str, hp: int) -> None:
        state["set_hp_calls"].append((eid, monster_id, hp))

    async def fake_append_utterance(eid: str, utterance: dict) -> None:
        state["appended"].append((eid, utterance))

    def fake_get_redis():
        return _FakeRedis(state["judge_blobs"])

    monkeypatch.setattr(debate_router, "get_meta", fake_get_meta)
    monkeypatch.setattr(debate_router, "load_combatants", fake_load_combatants)
    monkeypatch.setattr(debate_router, "get_transcript", fake_get_transcript)
    monkeypatch.setattr(debate_router, "get_mp_map", fake_get_mp_map)
    monkeypatch.setattr(debate_router, "set_mp", fake_set_mp)
    monkeypatch.setattr(debate_router, "set_hp", fake_set_hp)
    monkeypatch.setattr(debate_router, "append_utterance", fake_append_utterance)

    # `get_redis` is imported lazily inside the handler from `app.redis_state`,
    # so patch it on the underlying module.
    import app.redis_state as redis_mod

    monkeypatch.setattr(redis_mod, "get_redis", fake_get_redis)
    return state


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_memory_recall_happy_path(patch_router, gateway_mock) -> None:
    """Populated transcript + working gateway -> well-formed MemoryRecallResult."""
    result = await debate_router.memory_recall(eid="enc-1", session=None)

    assert isinstance(result, MemoryRecallResult)
    assert result.encounter_id == "enc-1"
    assert result.coach_monster_id == "party-1"

    # MP economy: full 60 MP spent, max_mp(60) - 60 = 0 remaining.
    assert result.mp_spent == 60
    assert result.mp_remaining == 0

    # The most-recent enemy line is the highlighted one.
    assert result.highlighted_line == "Italians would riot at the suggestion."

    # The counter is the (stub) gateway response, non-empty.
    assert result.counter_text.strip()

    # transcript_slice surfaces 5 most-recent lines (the original 4 + appended counter).
    assert len(result.transcript_slice) == 5
    # The appended counter sits at the end of the slice and is prefixed with "party:".
    assert result.transcript_slice[-1].startswith("party:")

    # Damage flows through compute_damage (LOGOS vs PATHOS = 1.5x, atk/def =
    # 14/8, skill_mult 1.6, domain PHILOSOPHY vs neutral topic = 1.0). It must
    # be a positive integer.
    assert isinstance(result.damage, int)
    assert result.damage > 0

    # Side-effects: MP debited via set_mp (0), enemy HP set to 100 - damage,
    # and one utterance appended to the transcript.
    assert patch_router["set_mp_calls"] == [("enc-1", "party-1", 0)]
    assert len(patch_router["set_hp_calls"]) == 1
    assert patch_router["set_hp_calls"][0][:2] == ("enc-1", "enemy-1")
    expected_hp = max(0, 100 - result.damage)
    assert patch_router["set_hp_calls"][0][2] == expected_hp
    assert len(patch_router["appended"]) == 1
    appended_utt = patch_router["appended"][0][1]
    assert appended_utt["actor_id"] == "party-1"
    assert appended_utt["actor_role"] == "party"
    assert appended_utt["skill_used"] == "Memory Recall"


@pytest.mark.asyncio
async def test_memory_recall_cache_miss_refunds_half_mp(
    patch_router, gateway_mock
) -> None:
    """Empty transcript -> graceful fallback: damage=0, mp_spent=30 (half cost)."""
    patch_router["transcript"] = []

    result = await debate_router.memory_recall(eid="enc-1", session=None)

    assert isinstance(result, MemoryRecallResult)
    assert result.encounter_id == "enc-1"
    assert result.coach_monster_id == "party-1"
    assert result.damage == 0
    assert result.mp_spent == 30  # half of 60
    assert result.mp_remaining == 30  # 60 (max_mp default) - 30 refunded
    # Counter is a templated fallback, not blank.
    assert result.counter_text.strip()
    # The fallback writes half-cost MP via set_mp (the refund path).
    assert patch_router["set_mp_calls"] == [("enc-1", "party-1", 30)]
    # No HP write + no utterance appended on the fallback path.
    assert patch_router["set_hp_calls"] == []
    assert patch_router["appended"] == []


@pytest.mark.asyncio
async def test_memory_recall_blocked_below_mp_cost(
    patch_router, gateway_mock
) -> None:
    """A coach with < 60 MP -> HTTP 400, no gateway call, no side-effects."""
    from fastapi import HTTPException

    # Pre-populate the MP map with a sub-cost balance so the gate trips before
    # the endpoint reaches the gateway.
    patch_router["mp_map"] = {"party-1": 10}

    with pytest.raises(HTTPException) as exc_info:
        await debate_router.memory_recall(eid="enc-1", session=None)

    assert exc_info.value.status_code == 400
    assert "MP" in str(exc_info.value.detail)
    # No gateway call, no side-effects.
    assert gateway_mock.complete_calls == []
    assert patch_router["set_mp_calls"] == []
    assert patch_router["set_hp_calls"] == []
    assert patch_router["appended"] == []


@pytest.mark.asyncio
async def test_memory_recall_gateway_failure_degrades_to_fallback(
    patch_router, monkeypatch
) -> None:
    """A raising gateway must NOT propagate — fall back to half-MP refund + 0 damage."""
    import app.gateway.gateway as gw_module

    async def boom(messages, **kwargs):
        raise RuntimeError("ollama is down")

    monkeypatch.setattr(gw_module.gateway, "complete", boom)

    result = await debate_router.memory_recall(eid="enc-1", session=None)

    assert isinstance(result, MemoryRecallResult)
    assert result.damage == 0
    assert result.mp_spent == 30
    assert result.mp_remaining == 30
    # On a model failure the highlighted line is still rendered (we picked it
    # before generating) so the player understands why nothing landed.
    assert "Italians" in result.highlighted_line
    # No HP write, no appended utterance on the fallback path.
    assert patch_router["set_hp_calls"] == []
    assert patch_router["appended"] == []


@pytest.mark.asyncio
async def test_memory_recall_uses_persona_voice_in_prompt(
    patch_router, gateway_mock
) -> None:
    """The coach's persona.voice must appear in the prompt sent to the gateway."""
    await debate_router.memory_recall(eid="enc-1", session=None)

    assert len(gateway_mock.complete_calls) == 1
    user_msg = next(
        m for m in gateway_mock.complete_calls[0]["messages"] if m["role"] == "user"
    )
    # The Socratesaur's `persona.voice` is grafted into the prompt verbatim.
    assert "I know that I know nothing" in user_msg["content"]
    # The highlighted line is quoted back literally.
    assert "Italians would riot" in user_msg["content"]
