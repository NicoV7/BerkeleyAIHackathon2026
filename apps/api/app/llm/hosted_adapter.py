"""hosted_adapter.py — One async ``complete()`` over four free LLM providers.

Wave 4 living layer needs NPC dialogue, historical-figure voicing, and dynamic
quest copy. The single-slot CPU Ollama is reserved for the judge (the "fight
runs offline" claim), so world dialogue uses a hosted free-tier LLM behind ONE
adapter with automatic failover across:

  1. Groq        (30K tok/min, sub-100ms, no card)
  2. Cerebras    (1M tok/day, 2000 tok/sec, no card)
  3. Gemini      (Google AI Studio, ~15 RPM, no card)
  4. OpenRouter  (:free models — DeepSeek-V3, Llama 3.3 70B Instruct, no card)

Priority order is configurable; default is fastest-first. On any failure (HTTP
error, timeout, rate limit) the adapter tries the next provider. When NO
providers are configured (no keys in env), the adapter returns a static stub so
offline dev / tests never block.

All keys read from ``app.config.settings`` (env-driven, never logged).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Stub used when no keys are configured. Short, recognisable, and harmless —
# unit tests assert on this exact string to verify the no-key code path.
STUB_RESPONSE = "[the path is silent]"

# Per-call timeout (seconds) for ANY single provider attempt. The adapter
# overall may take up to ~timeout * len(providers) on a worst-case failover.
PROVIDER_TIMEOUT_S = 10

# Default ordering — fastest-first. Tests may override via ``order=``.
DEFAULT_ORDER: tuple[str, ...] = ("groq", "cerebras", "gemini", "openrouter")


@dataclass
class ProviderResult:
    """Outcome of a single provider attempt — used by the adapter telemetry."""

    provider: str
    ok: bool
    status: int | None = None
    text: str = ""
    error: str = ""


@dataclass
class HostedAdapter:
    """Stateless thin adapter over the four free tiers (httpx async client)."""

    order: tuple[str, ...] = DEFAULT_ORDER
    client_factory: Callable[[], httpx.AsyncClient] = field(
        default_factory=lambda: lambda: httpx.AsyncClient(timeout=PROVIDER_TIMEOUT_S)
    )

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 256,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> str:
        """Try each configured provider in order; return the first non-empty text.

        Returns ``STUB_RESPONSE`` if no provider is configured OR every provider
        failed. NEVER raises — callers can safely use the result as a string.
        """
        attempts = await self.complete_with_trace(
            prompt, max_tokens=max_tokens, temperature=temperature, system=system
        )
        for attempt in attempts:
            if attempt.ok and attempt.text:
                return attempt.text
        return STUB_RESPONSE

    async def complete_with_trace(
        self,
        prompt: str,
        *,
        max_tokens: int = 256,
        temperature: float = 0.7,
        system: str | None = None,
    ) -> list[ProviderResult]:
        """Like ``complete`` but returns per-provider attempts for telemetry."""
        results: list[ProviderResult] = []
        async with self.client_factory() as client:
            for provider in self.order:
                if not _has_key(provider):
                    continue
                try:
                    result = await _call_provider(
                        client, provider, prompt, max_tokens, temperature, system
                    )
                except Exception as exc:  # noqa: BLE001
                    result = ProviderResult(provider=provider, ok=False, error=str(exc))
                results.append(result)
                if result.ok and result.text:
                    return results
        return results


# --------------------------------------------------------------------------- #
# Provider keys (read from settings; ALSO check os.environ as a safety net so
# .env.local edits between processes are picked up without a restart)
# --------------------------------------------------------------------------- #


def _has_key(provider: str) -> bool:
    return bool(_key_for(provider))


def _key_for(provider: str) -> str:
    if provider == "groq":
        return settings.groq_api_key or os.environ.get("GROQ_API_KEY", "")
    if provider == "cerebras":
        return settings.cerebras_api_key or os.environ.get("CEREBRAS_API_KEY", "")
    if provider == "gemini":
        return settings.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
    if provider == "openrouter":
        return settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
    return ""


# --------------------------------------------------------------------------- #
# Provider call implementations
# --------------------------------------------------------------------------- #


async def _call_provider(
    client: httpx.AsyncClient,
    provider: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    system: str | None,
) -> ProviderResult:
    if provider == "groq":
        return await _call_openai_compat(
            client,
            provider="groq",
            base_url="https://api.groq.com/openai/v1",
            model="llama-3.3-70b-versatile",
            api_key=_key_for("groq"),
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
        )
    if provider == "cerebras":
        return await _call_openai_compat(
            client,
            provider="cerebras",
            base_url="https://api.cerebras.ai/v1",
            model="llama-3.3-70b",
            api_key=_key_for("cerebras"),
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
        )
    if provider == "openrouter":
        return await _call_openai_compat(
            client,
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="deepseek/deepseek-chat-v3:free",
            api_key=_key_for("openrouter"),
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
        )
    if provider == "gemini":
        return await _call_gemini(
            client,
            api_key=_key_for("gemini"),
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
        )
    return ProviderResult(provider=provider, ok=False, error="unknown provider")


async def _call_openai_compat(
    client: httpx.AsyncClient,
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    system: str | None,
) -> ProviderResult:
    """OpenAI-compatible /chat/completions call (Groq, Cerebras, OpenRouter)."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(
            f"{base_url}/chat/completions", json=payload, headers=headers
        )
    except httpx.HTTPError as exc:
        return ProviderResult(provider=provider, ok=False, error=f"http: {exc}")
    if resp.status_code != 200:
        return ProviderResult(
            provider=provider,
            ok=False,
            status=resp.status_code,
            error=resp.text[:200],
        )
    data = resp.json()
    choices = (data.get("choices") or [])
    if not choices:
        return ProviderResult(
            provider=provider, ok=False, status=200, error="empty choices"
        )
    text = (choices[0].get("message") or {}).get("content") or ""
    return ProviderResult(
        provider=provider, ok=bool(text.strip()), status=200, text=text.strip()
    )


async def _call_gemini(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    system: str | None,
) -> ProviderResult:
    """Google AI Studio Gemini API (different shape from OpenAI)."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )
    parts: list[dict[str, str]] = []
    if system:
        parts.append({"text": f"System: {system}\n\nUser: {prompt}"})
    else:
        parts.append({"text": prompt})
    payload: dict[str, Any] = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }
    try:
        resp = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        return ProviderResult(provider="gemini", ok=False, error=f"http: {exc}")
    if resp.status_code != 200:
        return ProviderResult(
            provider="gemini", ok=False, status=resp.status_code, error=resp.text[:200]
        )
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return ProviderResult(
            provider="gemini", ok=False, status=200, error="empty candidates"
        )
    content = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in content)
    return ProviderResult(
        provider="gemini", ok=bool(text.strip()), status=200, text=text.strip()
    )


# Default singleton — callers import `adapter` and just call ``await adapter.complete(...)``.
adapter = HostedAdapter()
