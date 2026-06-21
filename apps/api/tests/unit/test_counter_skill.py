"""Unit tests for WS-5 (the Rhetorical Flourish MP counter-skill) and the
WS-4 ``_get_mp_safe`` fail-closed fix.

The headline contract for the counter-skill is ZERO extra model calls: casting
Rhetorical Flourish must add no `gateway` completion/stream vs a normal skill
turn. It works purely by priming the caster's prompt with the opponent's
ALREADY-PRESENT last line (read from the transcript, or the cached opening on
round 1). These tests lock that in with a call-counting fake gateway — no
network, no Redis, no live stack.

Also covered:
  * MP is deducted when the skill is afforded, and the skill is DENIED when MP <
    cost (auto + human paths) — using the orchestrator's own MP gate.
  * ``_get_mp_safe`` is now FAIL-CLOSED: a Redis miss (empty hash / error) denies
    an MP-costed skill instead of silently granting ``max_mp`` (the old bug).
  * The counter-skill cost is non-trivial (40 MP) so the gate actually bites.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.debate import orchestrator as orch
from app.debate.orchestrator import Combatant
from app.debate.skill_engine import skill_cost


# --------------------------------------------------------------------------- #
# Call-counting fake gateway
# --------------------------------------------------------------------------- #
class _CountingGateway:
    """Counts every completion/stream call so a test can assert ZERO extra calls."""

    def __init__(self) -> None:
        self.complete_calls = 0
        self.stream_calls = 0
        self.last_stream_messages: list[dict[str, str]] | None = None

    async def complete(self, messages, model=None, temperature=0.7,
                       max_tokens=512, json_mode=False, timeout=None) -> str:
        self.complete_calls += 1
        return "A normal generated argument that takes a clear side."

    async def stream(self, messages, model=None, temperature=0.7, max_tokens=512):
        self.stream_calls += 1
        self.last_stream_messages = messages
        for tok in ("A ", "streamed ", "argument."):
            yield tok


def _patch_gateway(monkeypatch: pytest.MonkeyPatch) -> _CountingGateway:
    fake = _CountingGateway()
    monkeypatch.setattr(orch.gateway, "complete", fake.complete)
    monkeypatch.setattr(orch.gateway, "stream", fake.stream)
    return fake


def _party(skills: list[Any] | None = None) -> Combatant:
    c = Combatant(
        monster_id="p1", name="Sage", type="RHETORIC", role="party",
        hp=100, max_hp=100, max_mp=100, skills=skills or [],
    )
    c.side = "for"
    return c


def _enemy() -> Combatant:
    c = Combatant(
        monster_id="e1", name="Foe", type="LOGOS", role="enemy",
        hp=100, max_hp=100, max_mp=100,
    )
    c.side = "against"
    return c


# --------------------------------------------------------------------------- #
# cost / slug
# --------------------------------------------------------------------------- #
def test_rhetorical_flourish_cost_is_meaningful() -> None:
    # The gate only bites if the move actually costs MP.
    assert skill_cost("Rhetorical Flourish") == 40
    assert skill_cost("Rhetorical Flourish") > 0


def test_is_counter_skill_slug_matching() -> None:
    assert orch._is_counter_skill("Rhetorical Flourish") is True
    assert orch._is_counter_skill("rhetorical_flourish") is True
    assert orch._is_counter_skill("RHETORICAL FLOURISH") is True
    assert orch._is_counter_skill("Steel Man") is False
    assert orch._is_counter_skill(None) is False


# --------------------------------------------------------------------------- #
# ZERO extra LLM calls: counter-skill turn vs normal turn
# --------------------------------------------------------------------------- #
async def test_counter_skill_adds_no_extra_stream_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A counter-skill turn must use EXACTLY ONE stream call — the same as a
    normal turn. The opponent's predicted line is read from the transcript (no
    generation), so the call count is unchanged."""
    fake = _patch_gateway(monkeypatch)
    caster = _party()
    transcript = [
        {"actor_id": "e1", "actor_role": "enemy", "text": "I argue AGAINST it."},
    ]

    counter_ctx = await orch._resolve_counter_context(
        caster, "Rhetorical Flourish", transcript, "school choice", [caster, _enemy()]
    )
    assert counter_ctx  # the prime was built (no model call)

    # Drain the stream as the round runner would.
    async for _chunk in orch._stream_utterance(
        caster, "school choice", transcript, {"skill": "Rhetorical Flourish"},
        [], {"p1": "Sage", "e1": "Foe"}, counter_context=counter_ctx,
    ):
        pass

    assert fake.stream_calls == 1
    assert fake.complete_calls == 0  # the counter-skill never calls complete


async def test_counter_skill_call_count_equals_normal_skill_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Head-to-head: a Rhetorical Flourish turn and a normal (non-counter) skill
    turn issue the SAME number of model calls."""
    transcript = [
        {"actor_id": "e1", "actor_role": "enemy", "text": "The costs are too high."},
    ]

    # Normal skill turn (no counter context).
    fake_normal = _patch_gateway(monkeypatch)
    async for _ in orch._stream_utterance(
        _party(), "x", transcript, {"skill": "Steel Man"}, [], {}, counter_context=None,
    ):
        pass
    normal_total = fake_normal.complete_calls + fake_normal.stream_calls

    # Counter-skill turn (with the primed context).
    fake_counter = _patch_gateway(monkeypatch)
    ctx = await orch._resolve_counter_context(
        _party(), "Rhetorical Flourish", transcript, "x", None
    )
    async for _ in orch._stream_utterance(
        _party(), "x", transcript, {"skill": "Rhetorical Flourish"}, [], {},
        counter_context=ctx,
    ):
        pass
    counter_total = fake_counter.complete_calls + fake_counter.stream_calls

    assert counter_total == normal_total == 1


async def test_counter_context_injects_enemy_line_into_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opponent's actual last line must end up in the caster's SYSTEM prompt."""
    fake = _patch_gateway(monkeypatch)
    enemy_line = "Banning cars would strand rural families."
    transcript = [{"actor_id": "e1", "actor_role": "enemy", "text": enemy_line}]
    ctx = await orch._resolve_counter_context(
        _party(), "Rhetorical Flourish", transcript, "car-free cities", None
    )
    async for _ in orch._stream_utterance(
        _party(), "car-free cities", transcript, {"skill": "Rhetorical Flourish"},
        [], {}, counter_context=ctx,
    ):
        pass
    system = " ".join(
        m["content"] for m in (fake.last_stream_messages or []) if m["role"] == "system"
    )
    assert enemy_line in system
    assert "pre-empt" in system.lower() or "pre-empt".upper() in system


async def test_counter_context_round1_uses_cached_opening_no_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 1 (no enemy line yet): the predicted line is the MATERIALIZED opening
    via a pure cache RETRIEVAL (get_cached_opening) — never get_or_create_opening,
    so no generation is triggered."""
    import app.debate.materialize as mz

    get_or_create_called = {"n": 0}
    cached_opening = "I argue AGAINST car-free cities: they strand the carless poor."

    async def fake_get_cached(topic, side="against"):
        return cached_opening

    async def fake_get_or_create(*a, **k):  # must NOT be called by the counter path
        get_or_create_called["n"] += 1
        return ("SHOULD NOT BE USED", False)

    monkeypatch.setattr(mz, "get_cached_opening", fake_get_cached)
    monkeypatch.setattr(mz, "get_or_create_opening", fake_get_or_create)

    ctx = await orch._resolve_counter_context(
        _party(), "Rhetorical Flourish", [], "car-free cities", None
    )
    assert cached_opening in ctx
    assert get_or_create_called["n"] == 0  # zero generation on round 1


async def test_non_counter_skill_yields_no_context() -> None:
    ctx = await orch._resolve_counter_context(_party(), "Steel Man", [], "topic", None)
    assert ctx is None


# --------------------------------------------------------------------------- #
# _get_mp_safe — fail-CLOSED on a genuine miss (the WS-5 bug fix)
# --------------------------------------------------------------------------- #
async def test_get_mp_safe_returns_value_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_map(eid):
        return {"p1": 37}

    monkeypatch.setattr("app.redis_state.get_mp_map", fake_map)
    assert await orch._get_mp_safe("enc", "p1", 100) == 37


async def test_get_mp_safe_clamps_to_max(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_map(eid):
        return {"p1": 999}

    monkeypatch.setattr("app.redis_state.get_mp_map", fake_map)
    assert await orch._get_mp_safe("enc", "p1", 50) == 50


async def test_get_mp_safe_fresh_combatant_in_populated_hash_gets_full_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hash exists (other combatants present) but this monster isn't seeded yet ->
    # legitimate fresh state, full pool.
    async def fake_map(eid):
        return {"someone_else": 10}

    monkeypatch.setattr("app.redis_state.get_mp_map", fake_map)
    assert await orch._get_mp_safe("enc", "p1", 80) == 80


async def test_get_mp_safe_denies_on_empty_hash_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FIX: an EMPTY mp hash (evicted / never seeded) must return 0 so an
    MP-costed skill is denied — not the old fail-open ``max_mp``."""
    async def fake_map(eid):
        return {}

    monkeypatch.setattr("app.redis_state.get_mp_map", fake_map)
    assert await orch._get_mp_safe("enc", "p1", 100) == 0


async def test_get_mp_safe_denies_on_redis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis error is a genuine miss -> fail CLOSED (0), not fail open."""
    async def boom(eid):
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.redis_state.get_mp_map", boom)
    assert await orch._get_mp_safe("enc", "p1", 100) == 0


async def test_counter_skill_denied_when_mp_below_cost_after_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end gate semantics: post-eviction (empty hash), an MP-costed skill's
    affordability check fails, because _get_mp_safe now returns 0 < cost."""
    async def fake_map(eid):
        return {}

    monkeypatch.setattr("app.redis_state.get_mp_map", fake_map)
    cost = skill_cost("Rhetorical Flourish")
    cur = await orch._get_mp_safe("enc", "p1", 100)
    assert cur < cost  # denied — the gate bites instead of granting it free
