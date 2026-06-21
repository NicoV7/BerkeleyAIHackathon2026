"""Wave 3 — agent-generated world.

A "thin harness / fat skill" level generator: one gateway LLM call turns a seed
into a richer structured ``WorldSpecLite``, validated + cached, with a total
fallback to the Wave-2 procedural world so it can NEVER break the game.

Public surface:
    await generate_world(seed, width, height, *, model="default") -> WorldSpecLite | None
"""
from __future__ import annotations

from app.world.generator import generate_world

__all__ = ["generate_world"]
