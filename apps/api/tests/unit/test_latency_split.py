"""Unit tests for the LATENCY + STANCE fast-path (Fast & Fun Battles).

Scope (pure, no live model / no DB):
  * The timeout knob is SPLIT: a SMALL `first_token_timeout_s` guards the live
    streaming first token, while the larger `llm_call_timeout_s` budgets the
    non-streaming actor `complete` (and judge).
  * The streaming first-token guard actually USES `first_token_timeout_s`.
  * The non-streaming actor `complete` uses the larger `llm_call_timeout_s`.
  * Actor turn length is config-driven via `actor_max_tokens`.
  * The actor side instruction is unmistakable: it names the assigned side
    (FOR / AGAINST) AND the topic, for BOTH sides — covering the playtest bug
    where an AGAINST enemy argued FOR.

The gateway is mocked: we replace `complete`/`stream` with recording stubs so we
can assert on the kwargs each path passes (timeout, max_tokens) without any
Ollama/Anthropic/OpenAI call.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.config import settings
from app.debate import orchestrator as orch
from app.debate.orchestrator import Combatant


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _combatant(side: str, role: str = "party") -> Combatant:
    return Combatant(
        monster_id=f"m-{side}",
        name="Tester",
        type="LOGOS",
        role=role,
        hp=100,
        max_hp=100,
        side=side,
    )


def _system_text(actor: Combatant, topic: str) -> str:
    msgs = orch._build_actor_messages(
        actor,
        topic,
        transcript=[],
        action={"behavior": None, "skill": None, "target": None, "tone": None},
        memories=[],
        name_lookup={},
    )
    return next(m["content"] for m in msgs if m["role"] == "system")


# --------------------------------------------------------------------------- #
# 1. The two knobs exist and are split (small vs large)
# --------------------------------------------------------------------------- #


def test_first_token_timeout_is_small_and_split_from_call_timeout() -> None:
    # first_token_timeout_s is the SMALL streaming first-token guard.
    assert isinstance(settings.first_token_timeout_s, int)
    assert settings.first_token_timeout_s <= 16, "first-token guard must stay small (~15s)"
    # llm_call_timeout_s is the LARGER non-streaming budget; the two are distinct.
    assert settings.llm_call_timeout_s > settings.first_token_timeout_s
    # actor_max_tokens is a small, punchy cap.
    assert isinstance(settings.actor_max_tokens, int)
    assert settings.actor_max_tokens <= 96
    assert settings.enemy_rebuttal_completion_timeout_s <= 5
    assert settings.enemy_rebuttal_max_tokens <= 96


def test_worst_case_complete_round_under_round_timeout() -> None:
    """N=4 actor non-streaming completes must stay under ROUND_TIMEOUT_S (120s)."""
    from app.routers.debate import ROUND_TIMEOUT_S

    assert orch._actor_timeout() * 4 < ROUND_TIMEOUT_S


# --------------------------------------------------------------------------- #
# 2. The streaming first-token guard USES first_token_timeout_s
# --------------------------------------------------------------------------- #


def test_stream_first_token_guard_uses_small_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "first_token_timeout_s", 3)
    assert orch._first_token_timeout() == 3.0

    captured: dict[str, Any] = {}
    real_wait_for = asyncio.wait_for

    async def _spy_wait_for(aw: Any, timeout: float | None = None) -> Any:
        # Record the FIRST wait_for timeout (the first-token guard) only.
        captured.setdefault("timeout", timeout)
        return await real_wait_for(aw, timeout=timeout)

    monkeypatch.setattr(orch.asyncio, "wait_for", _spy_wait_for)

    async def _fake_stream(messages, model=None, temperature=0.7, max_tokens=512):
        for tok in ("Hello ", "world"):
            yield tok

    monkeypatch.setattr(orch.gateway, "stream", _fake_stream)

    actor = _combatant("against", role="enemy")

    async def _drive() -> None:
        async for _ in orch._stream_utterance(
            actor, "Topic X", [], {"behavior": None, "skill": None}, [], {}
        ):
            pass

    asyncio.run(_drive())
    assert captured["timeout"] == 3.0, "stream guard must use first_token_timeout_s"


def test_action_first_token_override_caps_enemy_rebuttal() -> None:
    assert orch._action_first_token_timeout(
        {"first_token_timeout_s": 10},
        "any-model",
    ) == 10.0


# --------------------------------------------------------------------------- #
# 3. The non-streaming actor complete uses the LARGER budget + actor_max_tokens
# --------------------------------------------------------------------------- #


def test_actor_complete_uses_large_budget_and_config_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_call_timeout_s", 31)
    monkeypatch.setattr(settings, "actor_max_tokens", 48)

    calls: list[dict[str, Any]] = []

    async def _fake_complete(messages, **kwargs):
        calls.append(kwargs)
        return "I argue FOR Topic X with a concrete claim."

    monkeypatch.setattr(orch.gateway, "complete", _fake_complete)

    actor = _combatant("for", role="party")

    async def _drive() -> str:
        return await orch._generate_utterance(
            actor, "Topic X", [], {"behavior": None, "skill": None}, [], {}
        )

    text = asyncio.run(_drive())
    assert text  # produced a real utterance
    assert len(calls) == 1
    assert calls[0]["timeout"] == 31.0, "actor complete must use llm_call_timeout_s"
    assert calls[0]["max_tokens"] == 48, "actor turn tokens must be config-driven"
    # The small first-token guard is NOT the budget for the non-streaming complete.
    assert calls[0]["timeout"] != settings.first_token_timeout_s


def test_actor_max_tokens_is_config_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "actor_max_tokens", 17)
    assert orch._actor_max_tokens() == 17


# --------------------------------------------------------------------------- #
# 4. Side anchoring: instruction names the assigned side + topic, both sides
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "side,verb,other",
    [("for", "FOR", "AGAINST"), ("against", "AGAINST", "FOR")],
)
def test_side_instruction_names_side_and_topic(side: str, verb: str, other: str) -> None:
    topic = "Pineapple belongs on pizza"
    actor = _combatant(side, role="party" if side == "for" else "enemy")
    sys = _system_text(actor, topic)

    # The assigned side verb and the concrete topic both appear.
    assert verb in sys
    assert topic in sys
    # And it explicitly forbids flipping to the other side (the playtest bug).
    lowered = sys.lower()
    assert "do not switch sides" in lowered
    assert "only" in lowered  # "argue ONLY for the <side> side"


def test_side_instruction_distinguishes_for_from_against() -> None:
    topic = "Remote work is better"
    for_actor = _combatant("for", role="party")
    against_actor = _combatant("against", role="enemy")
    for_sys = _system_text(for_actor, topic)
    against_sys = _system_text(against_actor, topic)
    # The two prompts must commit to opposite sides.
    assert "ASSIGNED SIDE: FOR" in for_sys
    assert "ASSIGNED SIDE: AGAINST" in against_sys
    assert for_sys != against_sys


def test_persona_prompt_includes_generated_and_gacha_fields() -> None:
    actor = _combatant("for", role="party")
    actor.persona = {
        "voice": "I know that I know nothing.",
        "tagline": "Question everything.",
        "backstory": "A market philosopher with a patient trap.",
        "tone": "calmly relentless",
        "quirks": "answers with questions",
    }

    sys = _system_text(actor, "AI tutors should be free")

    assert "I know that I know nothing" in sys
    assert "Question everything" in sys
    assert "market philosopher" in sys
    assert "calmly relentless" in sys
    assert "answers with questions" in sys
