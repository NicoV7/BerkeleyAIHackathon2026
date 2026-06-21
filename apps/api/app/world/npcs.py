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

import json
import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.llm.hosted_adapter import STUB_RESPONSE, HostedAdapter
from app.llm.hosted_adapter import adapter as default_adapter
from app.redis_state import get_redis
from app.schemas import NPCAnchor, Region
from app.world import event_log, figures

CACHE_TTL_S = 30  # seconds; refreshed by event_tail invalidation, not just TTL
EVENT_TAIL = 6  # recent events included in the prompt + cache key
CONVERSATION_TTL_S = 15 * 60
MAX_CONVERSATION_TURNS = 8


def cache_key(npc_id: str, archetype: str, events_tail_hash: str, figure_count: int) -> str:
    """Stable Redis key. Tail-hash means a new event invalidates the entry."""
    return f"npctalk:{npc_id}:{archetype}:{events_tail_hash}:{figure_count}"


def conversation_key(run_id: str, npc_id: str, conversation_id: str) -> str:
    """Stable Redis key for one player/NPC conversation thread."""
    return f"npcchat:{run_id}:{npc_id}:{conversation_id}"


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
    *,
    history: list[dict[str, str]] | None = None,
    player_message: str = "",
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

    history_lines = []
    for turn in (history or [])[-MAX_CONVERSATION_TURNS * 2:]:
        role = "Player" if turn.get("role") == "player" else (anchor.name or "NPC")
        text = str(turn.get("text") or "").strip()
        if text:
            history_lines.append(f"{role}: {text}")
    history_blurb = (
        "Conversation so far:\n" + "\n".join(history_lines)
        if history_lines
        else "This is the start of the conversation."
    )
    player_blurb = (
        f"The player says: {player_message.strip()}"
        if player_message.strip()
        else "The player has just greeted you."
    )

    archetype_hints = {
        "villager": (
            "Speak briefly, like a townsfolk. Comment on the recent events naturally."
        ),
        "merchant": (
            "Speak as a shopkeeper. Mention wares if appropriate; reference "
            "quests you might offer."
        ),
        "quest_giver": (
            "Speak as a guard or captain. Offer or update a quest if the events "
            "suggest one."
        ),
        "innkeeper": (
            "Speak warmly, like an innkeeper who knows everyone. Reference "
            "returning travellers."
        ),
        "figure": (
            "Speak in the voice of a famous historical figure. The player has "
            "not yet recruited you."
        ),
    }
    archetype = anchor.archetype
    archetype_hint = archetype_hints.get(archetype, "Speak briefly and in character.")

    return (
        f"You are {anchor.name or 'an NPC'} ({archetype}). {region_blurb}\n\n"
        f"{events_blurb}\n\n"
        f"{roster_blurb}\n\n"
        f"{history_blurb}\n\n"
        f"{player_blurb}\n\n"
        f"{archetype_hint}\n"
        "Respond directly to the player in 1-3 short sentences, in character. "
        "Ground the answer in this game world, current quests, nearby places, "
        "or recent events when relevant. "
        "No quotation marks, no narration, just dialogue."
    )


def _event_label(evt: event_log.Event) -> str:
    """Human-friendly event label for deterministic local dialogue fallback."""
    data = evt.data
    if evt.kind == "dungeon_cleared":
        return str(data.get("name") or data.get("poi") or "a nearby den")
    if evt.kind == "boss_defeated":
        return str(data.get("name") or data.get("boss_id") or "a local threat")
    if evt.kind == "figure_recruited":
        return str(data.get("figure_id") or "a traveling figure")
    return evt.kind.replace("_", " ")


def _fallback_dialogue(
    anchor: NPCAnchor,
    region: Region | None,
    events: list[event_log.Event],
    player_message: str = "",
) -> str:
    """Event-aware local dialogue when no hosted NPC provider is configured."""
    place = region.name if region is not None else "this village"
    message = player_message.lower()
    latest = events[-1] if events else None
    if latest is not None:
        label = _event_label(latest)
        if latest.kind == "dungeon_cleared":
            return (
                f"I heard {label} was cleared. {place} will breathe easier, "
                "and the notice board should have fresher work soon."
            )
        if latest.kind == "boss_defeated":
            return f"Word travels fast: {label} has fallen. Even the roads feel lighter."
        if latest.kind == "figure_recruited":
            return f"So {label} walks with you now. That will turn heads around {place}."
        return f"Everyone in {place} is talking about {label}. You changed the road today."

    if "quest" in message or "work" in message or anchor.archetype == "quest_giver":
        return f"Start close to {place}: clear one nearby den, then come back for your reward."
    if anchor.archetype == "merchant":
        return f"Keep a potion handy before you leave {place}; the nearest dens test more than courage."
    if anchor.archetype == "innkeeper":
        return f"Welcome to {place}. Rest, listen, and ask the guards what has been stirring outside."
    return f"{place} has been waiting for someone willing to listen before charging off."


def _normalize_dialogue_text(
    text: str,
    anchor: NPCAnchor,
    region: Region | None,
    events: list[event_log.Event],
    player_message: str = "",
) -> str:
    """Replace empty/static provider output with deterministic world-aware text."""
    stripped = (text or "").strip()
    if not stripped or stripped == STUB_RESPONSE:
        return _fallback_dialogue(anchor, region, events, player_message)
    return stripped


@dataclass
class DialogueResult:
    text: str
    cached: bool
    cache_key: str
    conversation_id: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)


def _decode_history(raw_items: list[Any]) -> list[dict[str, str]]:
    """Decode Redis list items into bounded chat turns."""
    turns: list[dict[str, str]] = []
    for item in raw_items:
        if isinstance(item, bytes):
            item = item.decode("utf-8")
        try:
            data = json.loads(str(item))
        except Exception:  # noqa: BLE001
            continue
        role = data.get("role")
        text = str(data.get("text") or "").strip()
        if role in {"player", "npc"} and text:
            turns.append({"role": role, "text": text[:1200]})
    return turns[-MAX_CONVERSATION_TURNS * 2:]


async def _load_history(redis: Any, key: str) -> list[dict[str, str]]:
    """Read recent chat turns, tolerating empty/missing Redis lists."""
    raw = await redis.lrange(key, -MAX_CONVERSATION_TURNS * 2, -1)
    return _decode_history(raw)


async def _append_turn(redis: Any, key: str, role: str, text: str) -> None:
    """Append a bounded chat turn and keep the thread short-lived."""
    await redis.rpush(key, json.dumps({"role": role, "text": text[:1200]}))
    await redis.ltrim(key, -MAX_CONVERSATION_TURNS * 2, -1)
    expire = getattr(redis, "expire", None)
    if expire is not None:
        await expire(key, CONVERSATION_TTL_S)


async def generate_dialogue(
    run_id: str,
    anchor: NPCAnchor,
    region: Region | None = None,
    *,
    player_message: str = "",
    conversation_id: str | None = None,
    adapter: HostedAdapter = default_adapter,
) -> DialogueResult:
    """Top-level entry: cache-first NPC dialogue. Never raises."""
    events = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
    recruited = await figures.recruited_list(run_id)
    tail = events_tail_hash(events)
    key = cache_key(anchor.npc_id, anchor.archetype, tail, len(recruited))

    r = get_redis()
    message = (player_message or "").strip()
    if conversation_id is not None or message:
        conv_id = conversation_id or f"{anchor.npc_id}:{tail}"
        conv_key = conversation_key(run_id, anchor.npc_id, conv_id)
        history = await _load_history(r, conv_key)
        prompt = build_prompt(
            anchor,
            region,
            events,
            recruited,
            history=history,
            player_message=message,
        )
        if message:
            await _append_turn(r, conv_key, "player", message)
            history = [*history, {"role": "player", "text": message}]
        text = _normalize_dialogue_text(
            await adapter.complete(prompt, max_tokens=220, temperature=0.82),
            anchor,
            region,
            events,
            message,
        )
        await _append_turn(r, conv_key, "npc", text)
        history = [*history, {"role": "npc", "text": text}]
        return DialogueResult(
            text=text,
            cached=False,
            cache_key=conv_key,
            conversation_id=conv_id,
            history=history[-MAX_CONVERSATION_TURNS * 2:],
        )

    cached = await r.get(key)
    if cached is not None:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        return DialogueResult(text=cached, cached=True, cache_key=key)

    prompt = build_prompt(anchor, region, events, recruited)
    text = _normalize_dialogue_text(
        await adapter.complete(prompt, max_tokens=160, temperature=0.8),
        anchor,
        region,
        events,
    )
    await r.set(key, text, ex=CACHE_TTL_S)
    return DialogueResult(text=text, cached=False, cache_key=key)
