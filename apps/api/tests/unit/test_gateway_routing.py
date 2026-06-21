"""Unit tests for LLMGateway provider routing (Agent 4: claude-judge wiring).

Scope: prove that `LLMGateway.complete()` dispatches each model id to the right
provider adapter, with the judge-on-Claude path as the headline case. We never
touch a live model server: the gateway's httpx client is replaced with a fake
that records the URL it was POSTed to and returns a provider-shaped JSON body.

Routing under test (gateway.py + models.py):
  * ``anthropic/claude-...`` and the ``claude`` alias  -> Anthropic adapter
    (POST {anthropic_base_url}/v1/messages, x-api-key header).
  * ``ollama/gemma...``, the ``gemma`` alias, and a bare name on the default
    (ollama) provider                                   -> Ollama adapter
    (POST {ollama_base_url}/api/chat).
  * ``openai/gpt...`` / the ``gpt`` alias               -> OpenAI adapter.
  * An anthropic model with NO api key                  -> clear RuntimeError,
    so local-only operation (no key) is never silently broken.

Plus a config-level check that the additive `judge_provider` switch yields a
routable, anthropic-pinned judge model id while leaving the local default intact.

These are pure unit tests: no Ollama / Anthropic / OpenAI, no DB. They collect
and run on a bare host.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.gateway.gateway import LLMGateway
from app.gateway.models import resolve


# --------------------------------------------------------------------------- #
# Fake httpx client: records the POST URL + headers, returns a canned body.
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # always OK in these tests
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingClient:
    """Stands in for httpx.AsyncClient.

    Captures the (url, headers, json) of each POST and returns a provider-shaped
    response so the adapter's body-parsing succeeds. Which canned body to return
    is chosen by the URL path, so one fake serves all three adapters.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None, **_: Any) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        if "/api/chat" in url:  # Ollama
            return _FakeResponse({"message": {"content": "ollama-says-hi"}})
        if "/v1/messages" in url:  # Anthropic
            return _FakeResponse({"content": [{"type": "text", "text": "claude-says-hi"}]})
        if "/chat/completions" in url:  # OpenAI
            return _FakeResponse({"choices": [{"message": {"content": "openai-says-hi"}}]})
        raise AssertionError(f"unexpected POST url: {url}")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def gw_and_client(monkeypatch: pytest.MonkeyPatch):
    """A fresh gateway whose httpx client is the recording fake."""
    gw = LLMGateway()
    fake = _RecordingClient()
    gw._client = fake  # type: ignore[attr-defined]
    return gw, fake


_MSGS = [
    {"role": "system", "content": "You are a judge."},
    {"role": "user", "content": "Score this argument."},
]


# --------------------------------------------------------------------------- #
# resolve(): static mapping of ids -> (provider, model). No network.
# --------------------------------------------------------------------------- #


def test_resolve_maps_anthropic_prefixed_id_to_anthropic_provider() -> None:
    ref = resolve("anthropic/claude-sonnet-4-6")
    assert ref.provider == "anthropic"
    assert ref.model == "claude-sonnet-4-6"


def test_resolve_claude_alias_is_anthropic() -> None:
    ref = resolve("claude")
    assert ref.provider == "anthropic"
    assert ref.model.startswith("claude")


def test_resolve_ollama_prefixed_and_gemma_alias_are_ollama() -> None:
    assert resolve("ollama/gemma3:4b") == type(resolve("ollama/gemma3:4b"))(
        "ollama", "gemma3:4b"
    )
    gemma = resolve("gemma")
    assert gemma.provider == "ollama"
    assert gemma.model == "gemma3:4b"


def test_resolve_bare_name_uses_default_ollama_provider() -> None:
    # Bare model names fall through to the default provider (ollama by default).
    ref = resolve("some-local-model")
    assert ref.provider == "ollama"
    assert ref.model == "some-local-model"


# --------------------------------------------------------------------------- #
# complete(): end-to-end dispatch to the right adapter URL.
# --------------------------------------------------------------------------- #


async def test_anthropic_model_id_routes_to_anthropic_endpoint(
    gw_and_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: an api key must be present for the anthropic path to proceed.
    from app.gateway import gateway as gw_mod

    monkeypatch.setattr(gw_mod.settings, "anthropic_api_key", "sk-test-123")
    gw, fake = gw_and_client

    # Act: a judge pinned to Claude.
    out = await gw.complete(_MSGS, model="anthropic/claude-sonnet-4-6")

    # Assert: hit the Anthropic messages endpoint with the key + correct model.
    assert out == "claude-says-hi"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"].endswith("/v1/messages")
    assert call["headers"].get("x-api-key") == "sk-test-123"
    assert call["json"]["model"] == "claude-sonnet-4-6"


async def test_claude_alias_also_routes_to_anthropic(
    gw_and_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway import gateway as gw_mod

    monkeypatch.setattr(gw_mod.settings, "anthropic_api_key", "sk-test-123")
    gw, fake = gw_and_client

    out = await gw.complete(_MSGS, model="claude")

    assert out == "claude-says-hi"
    assert fake.calls[0]["url"].endswith("/v1/messages")


async def test_ollama_model_id_routes_to_ollama_endpoint(gw_and_client) -> None:
    # No api key needed: local default path stays key-free.
    gw, fake = gw_and_client

    out = await gw.complete(_MSGS, model="ollama/gemma3:4b")

    assert out == "ollama-says-hi"
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith("/api/chat")
    assert fake.calls[0]["json"]["model"] == "gemma3:4b"


async def test_gemma_alias_routes_to_ollama(gw_and_client) -> None:
    gw, fake = gw_and_client

    out = await gw.complete(_MSGS, model="gemma")

    assert out == "ollama-says-hi"
    assert fake.calls[0]["url"].endswith("/api/chat")


async def test_default_model_is_ollama_and_needs_no_api_key(gw_and_client) -> None:
    # model=None -> "default" alias -> ollama. Proves local-only works with no key.
    gw, fake = gw_and_client

    out = await gw.complete(_MSGS, model=None)

    assert out == "ollama-says-hi"
    assert fake.calls[0]["url"].endswith("/api/chat")


async def test_openai_model_id_routes_to_openai_endpoint(gw_and_client) -> None:
    gw, fake = gw_and_client

    out = await gw.complete(_MSGS, model="openai/gpt-4o-mini")

    assert out == "openai-says-hi"
    assert fake.calls[0]["url"].endswith("/chat/completions")


# --------------------------------------------------------------------------- #
# Graceful local-only operation: anthropic requested without a key.
# --------------------------------------------------------------------------- #


async def test_anthropic_without_api_key_raises_clear_error(
    gw_and_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: explicitly clear the key (host env / .env may have set one).
    from app.gateway import gateway as gw_mod

    monkeypatch.setattr(gw_mod.settings, "anthropic_api_key", "")
    gw, fake = gw_and_client

    # Act / Assert: a clear, actionable error — not a network call, not a 401.
    with pytest.raises(RuntimeError) as ei:
        await gw.complete(_MSGS, model="claude")

    msg = str(ei.value).lower()
    assert "api key" in msg
    assert "anthropic" in msg
    # No HTTP request was attempted.
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Config switch: pin the judge to Claude without touching debater models.
# --------------------------------------------------------------------------- #


def test_judge_provider_default_keeps_local_model_id() -> None:
    from app.config import Settings

    s = Settings(judge_provider="", llm_judge_model="gemma3:4b")
    # Default: judge id is the local model, unchanged.
    assert s.judge_model_id == "gemma3:4b"
    assert resolve(s.judge_model_id).provider == "ollama"


def test_judge_provider_anthropic_yields_routable_claude_id() -> None:
    from app.config import Settings

    s = Settings(judge_provider="anthropic", llm_judge_model="claude-sonnet-4-6")
    # The switch composes a routable provider-prefixed id...
    assert s.judge_model_id == "anthropic/claude-sonnet-4-6"
    # ...which the gateway routes to the anthropic adapter.
    ref = resolve(s.judge_model_id)
    assert ref.provider == "anthropic"
    assert ref.model == "claude-sonnet-4-6"
