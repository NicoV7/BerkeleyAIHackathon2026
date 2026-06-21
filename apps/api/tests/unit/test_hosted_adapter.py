"""Tests for the four-provider free-LLM adapter (Groq → Cerebras → Gemini → OpenRouter)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.llm.hosted_adapter import (
    DEFAULT_ORDER,
    STUB_RESPONSE,
    HostedAdapter,
    ProviderResult,
)


# --------------------------------------------------------------------------- #
# httpx mock infrastructure
# --------------------------------------------------------------------------- #

class _FakeClient:
    """Drop-in httpx.AsyncClient stand-in driven by a route map.

    routes: { (url_prefix): (status, json_or_text) }
        - status: int — HTTP status
        - body: dict for json, or str for raw text (used on non-200)
    Records every POST as (url, payload) so tests can assert provider order.
    """

    def __init__(self, routes: dict[str, tuple[int, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url: str, *, json: dict | None = None, headers: dict | None = None):
        self.calls.append((url, json or {}))
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                return _FakeResponse(status, body)
        return _FakeResponse(404, {"error": "no route"})


class _FakeResponse:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else str(body)

    def json(self) -> Any:
        return self._body


def _openai_chat_response(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def _gemini_response(text: str) -> dict[str, Any]:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

@pytest.fixture
def configure_all_keys(monkeypatch: pytest.MonkeyPatch):
    """All four providers configured."""
    monkeypatch.setenv("GROQ_API_KEY", "groq-test")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cere-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")


@pytest.fixture
def configure_no_keys(monkeypatch: pytest.MonkeyPatch):
    """Wipe every provider key — exercises the offline-dev stub path."""
    for k in ("GROQ_API_KEY", "CEREBRAS_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    # Settings is cached; clear the cached fields that fall back to env.
    from app.config import settings
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "cerebras_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")


async def test_no_keys_returns_stub(configure_no_keys):
    """With no keys configured the adapter returns the recognisable stub."""
    fake = _FakeClient({})
    a = HostedAdapter(client_factory=lambda: fake)
    out = await a.complete("hello")
    assert out == STUB_RESPONSE
    assert fake.calls == [], "must not hit any provider when there are no keys"


async def test_first_provider_success_short_circuits(configure_all_keys):
    """When Groq returns text the adapter doesn't call Cerebras/Gemini/OpenRouter."""
    fake = _FakeClient({
        "https://api.groq.com": (200, _openai_chat_response("hi from groq")),
    })
    a = HostedAdapter(client_factory=lambda: fake)
    out = await a.complete("hello")
    assert "groq" in out
    assert len(fake.calls) == 1


async def test_failover_skips_to_next_provider_on_429(configure_all_keys):
    """Groq 429 → Cerebras called next; Cerebras 200 → return its text."""
    fake = _FakeClient({
        "https://api.groq.com": (429, "rate limited"),
        "https://api.cerebras.ai": (200, _openai_chat_response("hi from cerebras")),
    })
    a = HostedAdapter(client_factory=lambda: fake)
    trace = await a.complete_with_trace("hello")
    providers = [r.provider for r in trace]
    assert providers == ["groq", "cerebras"], f"failover order broken: {providers}"
    assert trace[-1].ok and "cerebras" in trace[-1].text


async def test_all_providers_fail_returns_stub(configure_all_keys):
    """If every provider 5xxs the adapter returns the stub, never raises."""
    fake = _FakeClient({
        "https://api.groq.com": (500, "boom"),
        "https://api.cerebras.ai": (500, "boom"),
        "https://generativelanguage.googleapis.com": (500, "boom"),
        "https://openrouter.ai": (500, "boom"),
    })
    a = HostedAdapter(client_factory=lambda: fake)
    out = await a.complete("hello")
    assert out == STUB_RESPONSE


async def test_gemini_response_shape_parsed_correctly(configure_all_keys):
    """Gemini's candidates/parts payload extracts to the text field."""
    fake = _FakeClient({
        "https://api.groq.com": (429, "rate"),
        "https://api.cerebras.ai": (429, "rate"),
        "https://generativelanguage.googleapis.com": (200, _gemini_response("hi from gemini")),
    })
    a = HostedAdapter(client_factory=lambda: fake)
    out = await a.complete("hello")
    assert out == "hi from gemini"


async def test_only_configured_providers_are_tried(monkeypatch):
    """A partially configured environment only attempts the keys that exist."""
    # Only OpenRouter configured.
    for k in ("GROQ_API_KEY", "CEREBRAS_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    from app.config import settings
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "cerebras_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-test")

    fake = _FakeClient({
        "https://openrouter.ai": (200, _openai_chat_response("hi from openrouter")),
    })
    a = HostedAdapter(client_factory=lambda: fake)
    trace = await a.complete_with_trace("hello")
    providers = [r.provider for r in trace]
    assert providers == ["openrouter"]


async def test_order_is_configurable(configure_all_keys):
    """A custom order tries providers in the specified sequence."""
    fake = _FakeClient({
        "https://api.cerebras.ai": (200, _openai_chat_response("hi from cerebras first")),
    })
    a = HostedAdapter(
        order=("cerebras", "groq", "gemini", "openrouter"),
        client_factory=lambda: fake,
    )
    out = await a.complete("hello")
    assert "cerebras" in out
    assert len(fake.calls) == 1


async def test_network_error_skips_to_next_provider(configure_all_keys):
    """An httpx.HTTPError on Groq fails over to Cerebras."""
    class _BoomClient(_FakeClient):
        async def post(self, url, *, json=None, headers=None):
            self.calls.append((url, json or {}))
            if "groq" in url:
                raise httpx.ConnectError("boom")
            return await super().post(url, json=json, headers=headers)

    fake = _BoomClient({
        "https://api.cerebras.ai": (200, _openai_chat_response("rescued by cerebras")),
    })
    a = HostedAdapter(client_factory=lambda: fake)
    out = await a.complete("hello")
    assert "cerebras" in out


async def test_default_order_is_fastest_first():
    """Sanity: default order matches the plan's priority (Groq → Cerebras → Gemini → OpenRouter)."""
    assert DEFAULT_ORDER == ("groq", "cerebras", "gemini", "openrouter")
