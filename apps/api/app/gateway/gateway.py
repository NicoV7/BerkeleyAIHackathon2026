"""LLMGateway — the single seam every agent calls through.

Local-first: defaults route to Ollama. Anthropic and OpenAI are pluggable per
model alias (see models.py). All providers go over httpx (no provider SDKs), so
the container stays light and offline-capable.

Public surface (stable — Wave 1 builds on this):
    gateway.complete(messages, model=None, **opts) -> str
    gateway.stream(messages, model=None, **opts)    -> async iterator of str chunks
    gateway.embed(texts, model=None)                -> list[list[float]]
    gateway.health()                                -> dict
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.gateway.models import ModelRef, resolve
from app.gateway import pareto

Message = dict[str, str]  # {"role": "system|user|assistant", "content": "..."}

# The httpx client's ceiling is kept generous (embeddings / large judge calls can
# legitimately run long), but per-call completions resolve a SHORT effective
# timeout via `_resolve_timeout` so a stuck local model fails fast instead of
# hanging a whole battle round at this ceiling.
_DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _resolve_timeout(timeout: float | None) -> httpx.Timeout:
    """Per-call timeout resolution, in priority order:

        1. explicit ``timeout`` kwarg (seconds),
        2. ``settings.llm_call_timeout_s`` (the short fast-path default),
        3. the legacy 120s ceiling.

    Returns an httpx.Timeout so a stalled completion fails in ~20s, not 120s.
    """
    secs: float | None = None
    if timeout is not None and timeout > 0:
        secs = float(timeout)
    else:
        cfg = getattr(settings, "llm_call_timeout_s", None)
        if cfg:
            secs = float(cfg)
    if secs is None or secs <= 0:
        return _DEFAULT_TIMEOUT
    # Keep connect snappy but bounded by the overall budget.
    return httpx.Timeout(secs, connect=min(10.0, secs))


class LLMGateway:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- Public API ----

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Single completion. `timeout` (seconds) caps THIS call; falls back to
        settings.llm_call_timeout_s, then the legacy ceiling. A stuck call raises
        (httpx timeout) instead of hanging — callers fall back to templated text.
        """
        if model in {"pareto", "pareto-actor", "pareto-judge"}:
            return await self._complete_pareto(
                messages, model, temperature, max_tokens, json_mode, timeout
            )
        tmo = _resolve_timeout(timeout)
        ref = resolve(model)
        if ref.provider == "ollama":
            return await self._ollama_complete(ref, messages, temperature, max_tokens, json_mode, tmo)
        if ref.provider == "anthropic":
            return await self._anthropic_complete(ref, messages, temperature, max_tokens, tmo)
        if ref.provider == "openai":
            return await self._openai_complete(ref, messages, temperature, max_tokens, json_mode, tmo)
        if ref.provider == "groq":
            return await self._groq_complete(ref, messages, temperature, max_tokens, json_mode, tmo)
        if ref.provider == "cerebras":
            return await self._cerebras_complete(ref, messages, temperature, max_tokens, json_mode, tmo)
        if ref.provider == "gemini":
            return await self._gemini_complete(ref, messages, temperature, max_tokens, json_mode, tmo)
        if ref.provider == "openrouter":
            return await self._openrouter_complete(ref, messages, temperature, max_tokens, json_mode, tmo)
        raise ValueError(f"Unknown provider: {ref.provider}")

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        if model in {"pareto", "pareto-actor", "pareto-judge"}:
            role = pareto.JUDGE_ROLE if model == "pareto-judge" else pareto.ACTOR_ROLE
            async for chunk in self._stream_pareto(messages, role, temperature, max_tokens):
                yield chunk
            return
        ref = resolve(model)
        if ref.provider == "ollama":
            async for chunk in self._ollama_stream(ref, messages, temperature, max_tokens):
                yield chunk
        elif ref.provider == "openai":
            async for chunk in self._openai_stream(ref, messages, temperature, max_tokens):
                yield chunk
        elif ref.provider in {"groq", "cerebras", "openrouter"}:
            async for chunk in self._openai_compat_stream(ref, messages, temperature, max_tokens):
                yield chunk
        else:
            # Anthropic/Gemini streaming omitted for now — fall back to a single
            # completion yielded as one chunk.
            yield await self.complete(messages, model, temperature, max_tokens)

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        name = model or settings.llm_embed_model
        out: list[list[float]] = []
        for t in texts:
            r = await self._client.post(
                f"{settings.ollama_base_url}/api/embeddings",
                json={"model": name, "prompt": t},
            )
            r.raise_for_status()
            out.append(r.json()["embedding"])
        return out

    async def health(self) -> dict[str, Any]:
        """Ping the default provider so /health can report gateway status."""
        info: dict[str, Any] = {"provider": settings.llm_provider, "ok": False}
        try:
            r = await self._client.get(f"{settings.ollama_base_url}/api/tags", timeout=5.0)
            info["ok"] = r.status_code == 200
            info["models"] = [m["name"] for m in r.json().get("models", [])]
        except Exception as e:  # noqa: BLE001
            info["error"] = str(e)
        return info

    # ---- Ollama ----

    async def _ollama_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float,
        max_tokens: int, json_mode: bool, timeout: httpx.Timeout | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": ref.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        r = await self._post(f"{settings.ollama_base_url}/api/chat", payload, timeout)
        r.raise_for_status()
        return r.json()["message"]["content"]

    async def _complete_pareto(
        self,
        messages: list[Message],
        model: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        timeout: float | None,
    ) -> str:
        role = pareto.JUDGE_ROLE if model == "pareto-judge" or json_mode else pareto.ACTOR_ROLE
        last_error: Exception | None = None
        for candidate in pareto.fallback_order(role, json_mode=json_mode):
            try:
                return await self.complete(
                    messages, model=candidate, temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode, timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"No configured model candidates for {role}")

    async def _stream_pareto(
        self,
        messages: list[Message],
        role: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        last_error: Exception | None = None
        for candidate in pareto.fallback_order(role, json_mode=role == pareto.JUDGE_ROLE):
            try:
                async for chunk in self.stream(
                    messages, model=candidate, temperature=temperature, max_tokens=max_tokens
                ):
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"No configured streaming candidates for {role}")

    async def _post(self, url: str, payload: dict[str, Any], timeout: httpx.Timeout | None):
        """POST that forwards a per-call timeout when one was resolved. Kept tiny
        and tolerant of fake test clients whose .post() may not accept `timeout`.
        """
        if timeout is not None:
            try:
                return await self._client.post(url, json=payload, timeout=timeout)
            except TypeError:
                # Fake/legacy client without a `timeout` kwarg — degrade cleanly.
                return await self._client.post(url, json=payload)
        return await self._client.post(url, json=payload)

    async def _ollama_stream(
        self, ref: ModelRef, messages: list[Message], temperature: float, max_tokens: int,
    ) -> AsyncIterator[str]:
        payload = {
            "model": ref.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        async with self._client.stream(
            "POST", f"{settings.ollama_base_url}/api/chat", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                token = data.get("message", {}).get("content", "")
                if token:
                    yield token

    # ---- Anthropic ----

    async def _anthropic_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float, max_tokens: int,
        timeout: httpx.Timeout | None = None,
    ) -> str:
        # Local-first: never require an Anthropic key unless an anthropic model is
        # actually requested. If one is requested without a key, fail with a clear,
        # actionable error instead of a confusing 401 from the API.
        if not settings.anthropic_api_key:
            raise RuntimeError(
                f"Anthropic model '{ref.model}' was requested but no API key is set. "
                "Set ANTHROPIC_API_KEY (e.g. to pin the judge to Claude), or keep the "
                "default Ollama models for local-only operation."
            )
        system = " ".join(m["content"] for m in messages if m["role"] == "system")
        convo = [m for m in messages if m["role"] != "system"]
        kwargs: dict[str, Any] = {
            "headers": {
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            "json": {
                "model": ref.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": convo,
            },
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = await self._client.post(f"{settings.anthropic_base_url}/v1/messages", **kwargs)
        except TypeError:
            kwargs.pop("timeout", None)
            r = await self._client.post(f"{settings.anthropic_base_url}/v1/messages", **kwargs)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []))

    # ---- OpenAI ----

    async def _openai_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float,
        max_tokens: int, json_mode: bool, timeout: httpx.Timeout | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": ref.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {settings.openai_api_key}"},
            "json": body,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = await self._client.post(f"{settings.openai_base_url}/chat/completions", **kwargs)
        except TypeError:
            kwargs.pop("timeout", None)
            r = await self._client.post(f"{settings.openai_base_url}/chat/completions", **kwargs)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    # ---- Hosted OpenAI-compatible providers ----

    async def _groq_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float,
        max_tokens: int, json_mode: bool, timeout: httpx.Timeout | None = None,
    ) -> str:
        return await self._openai_compat_complete(
            ref, settings.groq_base_url, self._api_key("groq"),
            messages, temperature, max_tokens, json_mode, timeout,
        )

    async def _cerebras_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float,
        max_tokens: int, json_mode: bool, timeout: httpx.Timeout | None = None,
    ) -> str:
        return await self._openai_compat_complete(
            ref, settings.cerebras_base_url, self._api_key("cerebras"),
            messages, temperature, max_tokens, json_mode, timeout,
        )

    async def _openrouter_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float,
        max_tokens: int, json_mode: bool, timeout: httpx.Timeout | None = None,
    ) -> str:
        return await self._openai_compat_complete(
            ref, settings.openrouter_base_url, self._api_key("openrouter"),
            messages, temperature, max_tokens, json_mode, timeout,
        )

    async def _openai_compat_complete(
        self, ref: ModelRef, base_url: str, api_key: str, messages: list[Message],
        temperature: float, max_tokens: int, json_mode: bool,
        timeout: httpx.Timeout | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": ref.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        kwargs = {
            "headers": {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            "json": body,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = await self._client.post(f"{base_url.rstrip('/')}/chat/completions", **kwargs)
        except TypeError:
            kwargs.pop("timeout", None)
            r = await self._client.post(f"{base_url.rstrip('/')}/chat/completions", **kwargs)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    # ---- Gemini ----

    async def _gemini_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float,
        max_tokens: int, json_mode: bool, timeout: httpx.Timeout | None = None,
    ) -> str:
        api_key = self._api_key("gemini")
        url = f"{settings.gemini_base_url.rstrip('/')}/models/{ref.model}:generateContent"
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": _flatten_messages(messages)}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        kwargs: dict[str, Any] = {"params": {"key": api_key}, "json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = await self._client.post(url, **kwargs)
        except TypeError:
            kwargs.pop("timeout", None)
            r = await self._client.post(f"{url}?key={api_key}", json=body)
        r.raise_for_status()
        candidates = r.json().get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        return "".join(p.get("text", "") for p in parts)

    async def _openai_stream(
        self, ref: ModelRef, messages: list[Message], temperature: float, max_tokens: int,
    ) -> AsyncIterator[str]:
        async for chunk in self._openai_compat_stream(ref, messages, temperature, max_tokens):
            yield chunk

    async def _openai_compat_stream(
        self, ref: ModelRef, messages: list[Message], temperature: float, max_tokens: int,
    ) -> AsyncIterator[str]:
        base_url, api_key = self._chat_base_and_key(ref.provider)
        body = {
            "model": ref.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        async with self._client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                delta = json.loads(data)["choices"][0]["delta"].get("content")
                if delta:
                    yield delta

    def _chat_base_and_key(self, provider: str) -> tuple[str, str]:
        if provider == "openai":
            return settings.openai_base_url, settings.openai_api_key
        if provider == "groq":
            return settings.groq_base_url, self._api_key("groq")
        if provider == "cerebras":
            return settings.cerebras_base_url, self._api_key("cerebras")
        if provider == "openrouter":
            return settings.openrouter_base_url, self._api_key("openrouter")
        raise ValueError(f"Provider {provider} is not OpenAI-compatible")

    def _api_key(self, provider: str) -> str:
        key = getattr(settings, f"{provider}_api_key", "") or os.environ.get(
            f"{provider.upper()}_API_KEY", ""
        )
        if not key:
            raise RuntimeError(
                f"{provider} model requested but {provider.upper()}_API_KEY is not set."
            )
        return key


def _flatten_messages(messages: list[Message]) -> str:
    return "\n\n".join(f"{m.get('role', 'user').title()}: {m.get('content', '')}" for m in messages)


# Singleton used across the app.
gateway = LLMGateway()
