"""Unit tests for the BATTLE LATENCY fast-path (Fix Agent A).

The live-playtest P0: every round took 2+ minutes or timed out at the gateway's
120s ceiling, and every utterance rendered as the useless fallback stub
"(NAME presses the point on TOPIC.)". These tests lock in the fixes WITHOUT a
real Ollama:

  * config exposes the fast-path knobs (actor_model / judge_model_fast /
    llm_call_timeout_s) with short, fast defaults;
  * gateway.complete honors a per-call `timeout` (and forwards it to the HTTP
    client), falling back to settings.llm_call_timeout_s — NOT the old 120s;
  * actor turns + the autonomous enemy rebuttal use the FAST model and a SHORT
    timeout;
  * on model failure the fallback is a REAL argument that takes a side on the
    topic — never the old "presses the point" stub;
  * the coach (/assist) uses the fast model + short timeout.

All gateway calls are faked; no network, no DB.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import Settings, settings
from app.debate import orchestrator as orch
from app.debate.orchestrator import Combatant
from app.gateway.gateway import LLMGateway, _resolve_timeout


# --------------------------------------------------------------------------- #
# config knobs
# --------------------------------------------------------------------------- #


def test_config_exposes_fast_path_knobs_with_generous_defaults() -> None:
    s = Settings()
    # Fast small models, not the slow gemma3:4b.
    assert s.actor_model and s.actor_model != "gemma3:4b"
    assert s.judge_model_fast
    # A per-call timeout that gives the local model real room to respond, while
    # still well under the old 120s ceiling so a stuck call fails reasonably fast.
    assert 0 < s.llm_call_timeout_s <= 90
    assert hasattr(s, "prewarm_enabled")


# --------------------------------------------------------------------------- #
# gateway: per-call timeout
# --------------------------------------------------------------------------- #


def test_resolve_timeout_priority() -> None:
    # explicit kwarg wins
    assert _resolve_timeout(5).read == 5.0
    # falls back to the configured per-call timeout (not the old 120s ceiling)
    cfg = _resolve_timeout(None)
    assert cfg.read == float(settings.llm_call_timeout_s)
    assert cfg.read <= 90


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"message": {"content": "ok"}}


class _TimeoutRecordingClient:
    """Captures the `timeout` kwarg each POST receives."""

    def __init__(self) -> None:
        self.timeouts: list[Any] = []

    async def post(self, url: str, *, json: dict[str, Any] | None = None,
                   timeout: Any = None, **_: Any) -> _FakeResponse:
        self.timeouts.append(timeout)
        return _FakeResponse()

    async def aclose(self) -> None:
        return None


async def test_complete_forwards_explicit_per_call_timeout() -> None:
    gw = LLMGateway()
    fake = _TimeoutRecordingClient()
    gw._client = fake  # type: ignore[attr-defined]

    await gw.complete([{"role": "user", "content": "hi"}], model="gemma", timeout=7)

    assert len(fake.timeouts) == 1
    tmo = fake.timeouts[0]
    assert isinstance(tmo, httpx.Timeout)
    assert tmo.read == 7.0  # the per-call budget, far below 120s


async def test_complete_defaults_to_short_config_timeout_not_120s() -> None:
    gw = LLMGateway()
    fake = _TimeoutRecordingClient()
    gw._client = fake  # type: ignore[attr-defined]

    await gw.complete([{"role": "user", "content": "hi"}], model="gemma")

    tmo = fake.timeouts[0]
    assert tmo.read == float(settings.llm_call_timeout_s)
    assert tmo.read < 120.0


# --------------------------------------------------------------------------- #
# orchestrator: fast model + short timeout + REAL fallback
# --------------------------------------------------------------------------- #


def _combatant(ctype: str = "LOGOS", model: str | None = None) -> Combatant:
    return Combatant(
        monster_id="m1", name="Sage", type=ctype, role="party",
        hp=100, max_hp=100, model=model,
    )


def test_actor_model_prefers_fast_config_when_unpinned() -> None:
    c = _combatant(model=None)
    assert orch._actor_model(c) == settings.actor_model
    assert orch._actor_model(c) != "gemma3:4b"
    # An explicit pin is still honored.
    c2 = _combatant(model="my-trained-model")
    assert orch._actor_model(c2) == "my-trained-model"


async def test_generate_utterance_uses_fast_model_and_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_complete(messages, model=None, temperature=0.7,
                            max_tokens=512, json_mode=False, timeout=None):
        captured["model"] = model
        captured["timeout"] = timeout
        captured["max_tokens"] = max_tokens
        return "A crisp real argument."

    monkeypatch.setattr(orch.gateway, "complete", fake_complete)

    text = await orch._generate_utterance(
        _combatant(), "school uniforms", [], {"behavior": "argue"}, [], {"m1": "Sage"}
    )

    assert text == "A crisp real argument."
    assert captured["model"] == settings.actor_model
    assert captured["timeout"] == settings.llm_call_timeout_s
    assert captured["max_tokens"] <= 128


async def test_fallback_is_real_argument_not_the_old_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*a, **k):
        raise RuntimeError("model stalled")

    monkeypatch.setattr(orch.gateway, "complete", boom)

    topic = "universal basic income"
    text = await orch._generate_utterance(
        _combatant(), topic, [], {}, [], {"m1": "Sage"}
    )

    # NOT the old filler stub.
    assert "presses the point" not in text
    assert text != f"(Sage presses the point on {topic}.)"
    # A real, substantive, side-taking argument referencing the topic.
    assert topic in text
    assert len(text.split()) >= 12
    # Reads like a stance, not a stage direction.
    assert not text.startswith("(")


def test_fallback_varies_by_type_and_seed() -> None:
    topic = "remote work"
    logos = orch._fallback_argument(Combatant("a", "A", "LOGOS", "party", 100, 100), topic)
    pathos = orch._fallback_argument(Combatant("b", "B", "PATHOS", "party", 100, 100), topic)
    # Different debate types yield different framings (not one identical line).
    assert logos != pathos
    # Both still take a side on the topic.
    assert topic in logos and topic in pathos
    assert "presses the point" not in logos


async def test_enemy_rebuttal_path_uses_fast_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # The human-path enemy rebuttal flows through _generate_utterance, so it
    # inherits the fast model + short timeout. Verify via an enemy combatant.
    captured: dict[str, Any] = {}

    async def fake_complete(messages, model=None, timeout=None, **k):
        captured["model"] = model
        captured["timeout"] = timeout
        return "Enemy rebuts sharply."

    monkeypatch.setattr(orch.gateway, "complete", fake_complete)

    enemy = Combatant("e1", "Foe", "ETHOS", "enemy", 100, 100, model=None)
    out = await orch._generate_utterance(enemy, "ai safety", [], {}, [], {"e1": "Foe"})

    assert out == "Enemy rebuts sharply."
    assert captured["model"] == settings.actor_model
    assert captured["timeout"] == settings.llm_call_timeout_s


# --------------------------------------------------------------------------- #
# coach (/assist): fast model + short timeout
# --------------------------------------------------------------------------- #


async def test_coach_uses_fast_model_and_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.debate import coach as coach_mod
    from app.gateway.gateway import gateway as real_gw

    captured: dict[str, Any] = {}

    async def fake_complete(messages, model=None, temperature=0.7,
                            max_tokens=512, json_mode=False, timeout=None):
        captured["model"] = model
        captured["timeout"] = timeout
        return "Improved.\nRATIONALE: stronger\nANGLE: direct"

    monkeypatch.setattr(real_gw, "complete", fake_complete)

    # No live encounter state: coach load fails gracefully, model defaults to
    # the fast actor model. We just need a session-shaped object (unused).
    res = await coach_mod.coach_argument(session=None, eid="x", draft="my point")

    assert res.suggestions  # always returns a usable suggestion
    assert captured["model"] == settings.actor_model
    assert captured["timeout"] == float(settings.llm_call_timeout_s)


# --------------------------------------------------------------------------- #
# prewarm: best-effort, never raises, off-switch respected
# --------------------------------------------------------------------------- #


async def test_prewarm_is_best_effort_and_respects_off_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_complete(messages, model=None, **k):
        calls.append(model)
        raise RuntimeError("cold model")  # even on failure, prewarm must not raise

    monkeypatch.setattr(orch.gateway, "complete", fake_complete)

    monkeypatch.setattr(orch.settings, "prewarm_enabled", True)
    await orch.prewarm_models(["fast-a", "fast-a", "fast-b"])  # dedups
    assert calls == ["fast-a", "fast-b"]

    calls.clear()
    monkeypatch.setattr(orch.settings, "prewarm_enabled", False)
    await orch.prewarm_models(["fast-a"])
    assert calls == []  # off-switch honored
