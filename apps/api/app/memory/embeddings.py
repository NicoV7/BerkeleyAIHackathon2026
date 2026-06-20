"""Embedding helper — wraps the gateway's embed method.

Returns 768-dim float vectors via nomic-embed-text (Ollama).
Import-safe: if gateway is unavailable, raises at call time, not import time.
"""
from __future__ import annotations

from app.gateway.gateway import gateway


async def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts using nomic-embed-text (dim=768).

    Args:
        texts: List of strings to embed.

    Returns:
        List of float vectors, one per input string.
    """
    return await gateway.embed(texts, model="nomic-embed-text")
