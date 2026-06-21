"""Unit tests for the WS-4 warm-path latency work + side-specific fallbacks.

No live Ollama. We assert the LOGIC the latency win rests on:
  * the first-token budget WIDENS only once a model is marked warm, and a cold /
    never-prewarmed model keeps the small guard (so a stalled model still fails
    fast);
  * prewarm marks the model warm and is best-effort (never raises) even when the
    keep_alive ping and the gateway throwaway both fail;
  * BOTH debate sides are materializable + cached under distinct keys, with the
    AGAINST default preserving the legacy (no-side) cache key;
  * a fallback argument is SIDE- and TOPIC-specific — a FOR monster and an AGAINST
    monster never produce the same line, and each names the topic + its stance.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.debate import materialize as mz
from app.debate import orchestrator as orch
from app.debate.orchestrator import Combatant


@pytest.fixture(autouse=True)
def _clean_warm_state():
    orch._reset_warm_state()
    yield
    orch._reset_warm_state()


# --------------------------------------------------------------------------- #
# warm-aware first-token budget
# --------------------------------------------------------------------------- #
def test_cold_model_uses_small_first_token_budget() -> None:
    # Nothing prewarmed -> the small (cold) guard applies.
    assert orch._first_token_timeout("gemma3:1b") == float(settings.first_token_timeout_s)


def test_warm_model_widens_first_token_budget() -> None:
    orch._mark_warm("gemma3:1b")
    assert orch.is_model_warm("gemma3:1b") is True
    widened = orch._first_token_timeout("gemma3:1b")
    assert widened == float(settings.first_token_timeout_warm_s)
    # The widened budget is strictly larger than the cold guard.
    assert widened > float(settings.first_token_timeout_s)


def test_warm_widening_does_not_leak_to_other_models() -> None:
    orch._mark_warm("gemma3:1b")
    # A different, un-warmed model still gets the cold guard.
    assert orch._first_token_timeout("some-other-model") == float(
        settings.first_token_timeout_s
    )


def test_first_token_budget_never_below_cold_when_warm_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the warm budget is configured <= cold, we never shrink below cold.
    monkeypatch.setattr(orch.settings, "first_token_timeout_warm_s", 1)
    monkeypatch.setattr(orch.settings, "first_token_timeout_s", 15)
    orch._mark_warm("m")
    assert orch._first_token_timeout("m") == 15.0


# --------------------------------------------------------------------------- #
# prewarm marks warm + is best-effort
# --------------------------------------------------------------------------- #
async def test_prewarm_marks_model_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_complete(messages, model=None, **k):
        calls.append(model)
        return "ok"

    async def fake_keep_alive(model):
        return False  # force the gateway-throwaway fallback path

    monkeypatch.setattr(orch.gateway, "complete", fake_complete)
    monkeypatch.setattr(orch, "_ollama_keep_alive", fake_keep_alive)
    monkeypatch.setattr(orch.settings, "prewarm_enabled", True)

    await orch.prewarm_models(["fast-a"])
    assert orch.is_model_warm("fast-a") is True


async def test_prewarm_uses_keep_alive_ping_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keep_alive_models: list[str] = []
    complete_models: list[str] = []

    async def fake_keep_alive(model):
        keep_alive_models.append(model)
        return True  # keep_alive succeeded -> no gateway throwaway needed

    async def fake_complete(messages, model=None, **k):
        complete_models.append(model)
        return "ok"

    monkeypatch.setattr(orch, "_ollama_keep_alive", fake_keep_alive)
    monkeypatch.setattr(orch.gateway, "complete", fake_complete)
    monkeypatch.setattr(orch.settings, "prewarm_enabled", True)

    await orch.prewarm_models(["fast-a"])
    assert keep_alive_models == ["fast-a"]
    assert complete_models == []  # throwaway skipped when keep_alive worked
    assert orch.is_model_warm("fast-a") is True


async def test_prewarm_best_effort_when_everything_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_keep_alive(model):
        raise RuntimeError("no ollama")

    async def boom_complete(messages, model=None, **k):
        raise RuntimeError("cold model")

    monkeypatch.setattr(orch, "_ollama_keep_alive", boom_keep_alive)
    monkeypatch.setattr(orch.gateway, "complete", boom_complete)
    monkeypatch.setattr(orch.settings, "prewarm_enabled", True)

    # Must not raise; the model simply stays cold.
    await orch.prewarm_models(["fast-a"])
    assert orch.is_model_warm("fast-a") is False


# --------------------------------------------------------------------------- #
# both-sides opening materialization
# --------------------------------------------------------------------------- #
def test_against_default_preserves_legacy_cache_key() -> None:
    # AGAINST default must hit the legacy (no-side) digest so old cached openings
    # keep hitting.
    assert mz._opening_cache_dim("school uniforms", "against") == mz.topic_hash(
        "school uniforms"
    )
    assert mz.topic_hash("school uniforms", None) == mz.topic_hash("school uniforms")


def test_for_and_against_keys_do_not_collide() -> None:
    assert mz._opening_cache_dim("school uniforms", "for") != mz._opening_cache_dim(
        "school uniforms", "against"
    )


def test_for_opening_prompt_and_fallback_take_for_side() -> None:
    msgs = mz._opening_messages("school uniforms", "for")
    system = msgs[0]["content"]
    assert "ASSIGNED SIDE: FOR" in system
    # The AGAINST opening's stance anchor is the mirror image.
    against_system = mz._opening_messages("school uniforms", "against")[0]["content"]
    assert "ASSIGNED SIDE: AGAINST" in against_system
    assert "FOR" in mz._fallback_opening("school uniforms", "for")
    assert "AGAINST" in mz._fallback_opening("school uniforms", "against")


async def test_pregenerate_both_openings_warms_two_sides_no_double_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_sides: list[str] = []

    async def fake_pregen(topic, model=None, side="against"):
        generated_sides.append(side)
        return True

    monkeypatch.setattr(mz, "pregenerate_opening", fake_pregen)
    n = await mz.pregenerate_both_openings("school uniforms", None)
    assert n == 2
    assert set(generated_sides) == {"for", "against"}
    # AGAINST is warmed FIRST (it's the one the first enemy turn retrieves).
    assert generated_sides[0] == "against"


# --------------------------------------------------------------------------- #
# fallback quality — side + topic specific (WS-4 #9)
# --------------------------------------------------------------------------- #
def _c(side: str, ctype: str = "LOGOS", mid: str = "m") -> Combatant:
    c = Combatant(mid, "Name", ctype, "party", 100, 100)
    c.side = side
    return c


def test_fallback_for_and_against_differ_and_name_topic_and_stance() -> None:
    topic = "universal basic income"
    for_text = orch._fallback_argument(_c("for", mid="a"), topic)
    against_text = orch._fallback_argument(_c("against", mid="b"), topic)

    assert for_text != against_text
    assert topic in for_text and topic in against_text
    assert "FOR" in for_text
    assert "AGAINST" in against_text
    # Never the old generic stage-direction stub.
    assert "presses the point" not in for_text
    assert not for_text.startswith("(")


def test_fallback_is_not_the_generic_meta_line() -> None:
    # Regression for #9: a fallback must read as a real stance, not the generic
    # "I argue AGAINST {topic}" meta line for a FOR monster.
    topic = "remote work"
    for_text = orch._fallback_argument(_c("for"), topic)
    assert "AGAINST" not in for_text  # a FOR monster never argues against
    assert len(for_text.split()) >= 12  # substantive, not a one-liner stub
