"""Unit tests for the WS-0-LAT gateway failover candidate resolver/runner.

Scope (pure, no live model / no DB / no keys):
  * resolve_candidate picks the RIGHT backend+provider for ``ollama/...`` (local
    via the gateway) vs ``groq/...`` (hosted via the hosted_adapter), and for the
    other recognized providers + bare names.
  * parse_candidates preserves order and resolves each entry of the configured
    comma-list (gateway_actor_candidates / gateway_judge_candidates).
  * run_candidate routes a LOCAL spec through the gateway with the resolved
    provider/model string, and a HOSTED spec through the hosted adapter.
  * run_candidates respects ``gateway_fallback_enabled``: ON -> tries the chain
    until a success; OFF -> first candidate only (no fan-out), which is the
    demo-critical guard that keeps battles from silently spraying hosted providers.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.gateway import candidates as cands


# --------------------------------------------------------------------------- #
# 1. resolve_candidate: ollama vs groq vs others
# --------------------------------------------------------------------------- #


def test_resolve_ollama_spec_is_local() -> None:
    c = cands.resolve_candidate("ollama/gemma3:1b")
    assert c.backend == "local"
    assert c.provider == "ollama"
    assert c.model == "gemma3:1b"


def test_resolve_groq_spec_is_hosted() -> None:
    c = cands.resolve_candidate("groq/llama-3.1-8b-instant")
    assert c.backend == "hosted"
    assert c.provider == "groq"
    assert c.model == "llama-3.1-8b-instant"


@pytest.mark.parametrize(
    "spec,provider,backend",
    [
        ("cerebras/llama-3.3-70b", "cerebras", "hosted"),
        ("gemini/gemini-2.5-flash", "gemini", "hosted"),
        ("openrouter/deepseek/deepseek-chat-v3:free", "openrouter", "hosted"),
        ("anthropic/claude-sonnet-4-6", "anthropic", "local"),
        ("openai/gpt-4o-mini", "openai", "local"),
    ],
)
def test_resolve_known_providers(spec: str, provider: str, backend: str) -> None:
    c = cands.resolve_candidate(spec)
    assert c.provider == provider
    assert c.backend == backend


def test_resolve_bare_name_defaults_local() -> None:
    c = cands.resolve_candidate("gemma3:1b")
    assert c.backend == "local"
    assert c.provider == settings.llm_provider
    assert c.model == "gemma3:1b"


def test_parse_candidates_preserves_order_and_backends() -> None:
    parsed = cands.parse_candidates(
        "groq/llama-3.1-8b-instant, ollama/gemma3:1b , gemini/gemini-2.5-flash"
    )
    assert [c.spec for c in parsed] == [
        "groq/llama-3.1-8b-instant",
        "ollama/gemma3:1b",
        "gemini/gemini-2.5-flash",
    ]
    assert [c.backend for c in parsed] == ["hosted", "local", "hosted"]


def test_actor_and_judge_candidates_resolve_from_settings() -> None:
    actor = cands.actor_candidates()
    judge = cands.judge_candidates()
    assert actor and judge
    # The configured default actor chain ends with a local ollama fallback.
    assert actor[-1].backend == "local"
    assert actor[-1].provider == "ollama"
    # And starts with a hosted candidate (groq) in the shipped default.
    assert actor[0].backend == "hosted"


# --------------------------------------------------------------------------- #
# 2. run_candidate routing (local -> gateway, hosted -> hosted_adapter)
# --------------------------------------------------------------------------- #


async def test_run_candidate_local_routes_through_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_complete(messages, model=None, **kwargs):
        seen["model"] = model
        seen["kwargs"] = kwargs
        return "local says hi"

    import app.gateway.gateway as gw

    monkeypatch.setattr(gw.gateway, "complete", fake_complete)

    res = await cands.run_candidate(
        cands.resolve_candidate("ollama/gemma3:1b"),
        [{"role": "user", "content": "hello"}],
        max_tokens=32,
        timeout=9,
    )
    assert res.ok
    assert res.text == "local says hi"
    assert res.candidate is not None and res.candidate.backend == "local"
    # The resolved provider/model string is what the gateway receives.
    assert seen["model"] == "ollama/gemma3:1b"
    assert seen["kwargs"]["max_tokens"] == 32
    assert seen["kwargs"]["timeout"] == 9


async def test_run_candidate_hosted_routes_through_hosted_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.gateway.candidates as cmod

    captured: dict[str, Any] = {}

    class _FakeAdapter:
        def __init__(self, order=(), **k):
            captured["order"] = order

        async def complete(self, prompt, *, max_tokens=256, temperature=0.7, system=None):
            captured["prompt"] = prompt
            captured["system"] = system
            return "hosted says hi"

    # Patch the symbol the resolver imports lazily inside _run_hosted.
    import app.llm.hosted_adapter as ha

    monkeypatch.setattr(ha, "HostedAdapter", _FakeAdapter)

    res = await cmod.run_candidate(
        cmod.resolve_candidate("groq/llama-3.1-8b-instant"),
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ],
        max_tokens=64,
    )
    assert res.ok
    assert res.text == "hosted says hi"
    assert res.candidate is not None and res.candidate.backend == "hosted"
    # The adapter order is pinned to JUST this candidate's provider (groq), so a
    # groq/ spec doesn't fan out across the adapter's default chain.
    assert captured["order"] == ("groq",)
    assert captured["system"] == "be terse"


async def test_run_candidate_hosted_stub_is_treated_as_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the hosted provider has no key the adapter returns its STUB; the
    candidate must report a MISS so a chain can fall through."""
    import app.gateway.candidates as cmod
    import app.llm.hosted_adapter as ha

    class _StubAdapter:
        def __init__(self, order=(), **k):
            pass

        async def complete(self, prompt, **k):
            return ha.STUB_RESPONSE

    monkeypatch.setattr(ha, "HostedAdapter", _StubAdapter)

    res = await cmod.run_candidate(
        cmod.resolve_candidate("groq/whatever"),
        [{"role": "user", "content": "x"}],
    )
    assert not res.ok
    assert res.text == ""


# --------------------------------------------------------------------------- #
# 3. run_candidates fallback gating (demo-critical)
# --------------------------------------------------------------------------- #


async def test_run_candidates_failover_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gateway_fallback_enabled", True)

    calls: list[str] = []

    async def fake_run_candidate(candidate, messages, **kwargs):
        calls.append(candidate.spec)
        ok = candidate.provider == "ollama"  # only the local fallback "works"
        return cands.CandidateResult(
            text="ok" if ok else "", ok=ok, candidate=candidate
        )

    monkeypatch.setattr(cands, "run_candidate", fake_run_candidate)

    res = await cands.run_candidates(
        ["groq/llama-3.1-8b-instant", "cerebras/llama-3.3-70b", "ollama/gemma3:1b"],
        [{"role": "user", "content": "hi"}],
    )
    assert res.ok
    assert res.candidate is not None and res.candidate.provider == "ollama"
    # Tried the whole chain until the local fallback succeeded.
    assert calls == [
        "groq/llama-3.1-8b-instant",
        "cerebras/llama-3.3-70b",
        "ollama/gemma3:1b",
    ]
    assert res.attempts == 3


async def test_run_candidates_no_failover_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With fallback OFF only the FIRST candidate is attempted — the guard that
    stops an opt-in caller from silently spraying hosted providers."""
    monkeypatch.setattr(settings, "gateway_fallback_enabled", False)

    calls: list[str] = []

    async def fake_run_candidate(candidate, messages, **kwargs):
        calls.append(candidate.spec)
        return cands.CandidateResult(text="", ok=False, candidate=candidate)

    monkeypatch.setattr(cands, "run_candidate", fake_run_candidate)

    res = await cands.run_candidates(
        ["groq/llama-3.1-8b-instant", "cerebras/llama-3.3-70b", "ollama/gemma3:1b"],
        [{"role": "user", "content": "hi"}],
    )
    assert calls == ["groq/llama-3.1-8b-instant"]
    assert res.attempts == 1
    assert not res.ok


async def test_run_candidates_empty_chain_returns_miss() -> None:
    res = await cands.run_candidates([], [{"role": "user", "content": "hi"}])
    assert not res.ok
    assert res.attempts == 0
