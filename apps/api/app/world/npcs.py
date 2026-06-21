"""npcs.py — NPC dialogue generation (Wave 4 living layer).

NPCs are anchored on FEATURE tiles inside canonical interiors (see schemas:
``POI.npc_anchors``). When the player triggers a talk action, the runtime:

  1. Reads the recent world events from ``event_log``.
  2. Builds a dialogue prompt blending: anchor archetype + region lore +
     recent events + the player's recruited-figure roster.
  3. Calls ``hosted_adapter.complete()`` to get text from a free-tier provider.
  4. Caches the result in Redis for ``CACHE_TTL_S`` seconds, keyed by a hash
     of (npc_id, archetype, recent-event-tail, figure-count). Cache invalidates
     when the event tail changes, so a freshly cleared dungeon updates the
     greeting without a manual purge.

Pure-function helpers (``build_prompt``, ``cache_key``) are extracted so they
can be unit-tested without Redis or HTTP.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.llm.hosted_adapter import HostedAdapter
from app.llm.hosted_adapter import adapter as default_adapter
from app.redis_state import get_redis
from app.schemas import NPCAnchor, Region
from app.world import event_log, figures

CACHE_TTL_S = 30  # seconds; refreshed by event_tail invalidation, not just TTL
EVENT_TAIL = 6  # recent events included in the prompt + cache key


def cache_key(npc_id: str, archetype: str, events_tail_hash: str, figure_count: int) -> str:
    """Stable Redis key. Tail-hash means a new event invalidates the entry."""
    return f"npctalk:{npc_id}:{archetype}:{events_tail_hash}:{figure_count}"


def events_tail_hash(events: list[event_log.Event]) -> str:
    """Hash the kind+filtered-data of the recent events tail (ignoring timestamps).

    Timestamps would defeat caching across requests — we WANT two talks within
    30s to hit the cache if the event history hasn't materially changed.
    """
    h = hashlib.md5()
    for evt in events[-EVENT_TAIL:]:
        h.update(evt.kind.encode("utf-8"))
        # Sort data keys for determinism across dict orderings.
        for k in sorted(evt.data):
            h.update(k.encode("utf-8"))
            h.update(str(evt.data[k]).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:12]


def build_prompt(
    anchor: NPCAnchor,
    region: Region | None,
    events: list[event_log.Event],
    recruited: list[figures.Figure],
) -> str:
    """Assemble the NPC's dialogue prompt. Pure; no I/O."""
    region_blurb = ""
    if region is not None:
        region_blurb = f"You are in {region.name}."
        if region.lore:
            region_blurb += f" {region.lore}"

    event_lines = []
    for evt in events[-EVENT_TAIL:]:
        bits = ", ".join(f"{k}={v}" for k, v in evt.data.items())
        event_lines.append(f"- {evt.kind}({bits})")
    events_blurb = (
        "Recent events the NPC has heard about:\n" + "\n".join(event_lines)
        if event_lines
        else "Nothing notable has happened recently."
    )

    roster_blurb = ""
    if recruited:
        names = ", ".join(f.name for f in recruited)
        roster_blurb = f"The player travels with: {names}."

    archetype_hints = {
        "villager": "Speak briefly, like a townsfolk. Comment on the recent events naturally.",
        "merchant": "Speak as a shopkeeper. Mention wares if appropriate; reference quests you might offer.",
        "quest_giver": "Speak as a guard or captain. Offer or update a quest if the events suggest one.",
        "innkeeper": "Speak warmly, like an innkeeper who knows everyone. Reference returning travellers.",
        "figure": "Speak in the voice of a famous historical figure. The player has not yet recruited you.",
    }
    archetype = anchor.archetype
    archetype_hint = archetype_hints.get(archetype, "Speak briefly and in character.")

    return (
        f"You are {anchor.name or 'an NPC'} ({archetype}). {region_blurb}\n\n"
        f"{events_blurb}\n\n"
        f"{roster_blurb}\n\n"
        f"{archetype_hint}\n"
        "Respond in 1-3 short sentences, in character. No quotation marks, no narration, just dialogue."
    )


@dataclass
class DialogueResult:
    text: str
    cached: bool
    cache_key: str


async def generate_dialogue(
    run_id: str,
    anchor: NPCAnchor,
    region: Region | None = None,
    *,
    adapter: HostedAdapter = default_adapter,
) -> DialogueResult:
    """Top-level entry: cache-first NPC dialogue. Never raises."""
    events = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
    recruited = await figures.recruited_list(run_id)
    tail = events_tail_hash(events)
    key = cache_key(anchor.npc_id, anchor.archetype, tail, len(recruited))

    r = get_redis()
    cached = await r.get(key)
    if cached is not None:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        return DialogueResult(text=cached, cached=True, cache_key=key)

    prompt = build_prompt(anchor, region, events, recruited)
    text = await adapter.complete(prompt, max_tokens=160, temperature=0.8)
    await r.set(key, text, ex=CACHE_TTL_S)
    return DialogueResult(text=text, cached=False, cache_key=key)
