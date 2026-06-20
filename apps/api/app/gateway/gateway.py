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
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.gateway.models import ModelRef, resolve

Message = dict[str, str]  # {"role": "system|user|assistant", "content": "..."}

_DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


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
    ) -> str:
        ref = resolve(model)
        if ref.provider == "ollama":
            return await self._ollama_complete(ref, messages, temperature, max_tokens, json_mode)
        if ref.provider == "anthropic":
            return await self._anthropic_complete(ref, messages, temperature, max_tokens)
        if ref.provider == "openai":
            return await self._openai_complete(ref, messages, temperature, max_tokens, json_mode)
        raise ValueError(f"Unknown provider: {ref.provider}")

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        ref = resolve(model)
        if ref.provider == "ollama":
            async for chunk in self._ollama_stream(ref, messages, temperature, max_tokens):
                yield chunk
        elif ref.provider == "openai":
            async for chunk in self._openai_stream(ref, messages, temperature, max_tokens):
                yield chunk
        else:
            # Anthropic streaming omitted for Wave 0 brevity — fall back to a
            # single completion yielded as one chunk.
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
        max_tokens: int, json_mode: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": ref.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        r = await self._client.post(f"{settings.ollama_base_url}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"]

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
    ) -> str:
        system = " ".join(m["content"] for m in messages if m["role"] == "system")
        convo = [m for m in messages if m["role"] != "system"]
        r = await self._client.post(
            f"{settings.anthropic_base_url}/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ref.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": convo,
            },
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []))

    # ---- OpenAI ----

    async def _openai_complete(
        self, ref: ModelRef, messages: list[Message], temperature: float,
        max_tokens: int, json_mode: bool,
    ) -> str:
        body: dict[str, Any] = {
            "model": ref.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = await self._client.post(
            f"{settings.openai_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=body,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _openai_stream(
        self, ref: ModelRef, messages: list[Message], temperature: float, max_tokens: int,
    ) -> AsyncIterator[str]:
        body = {
            "model": ref.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        async with self._client.stream(
            "POST",
            f"{settings.openai_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
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


# Singleton used across the app.
gateway = LLMGateway()
