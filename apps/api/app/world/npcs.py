"""npcs.py - NPC dialogue generation (Wave 4 living layer).

NPCs are anchored on FEATURE tiles inside canonical interiors (see schemas:
``POI.npc_anchors``). When the player triggers a talk action, the runtime:

  1. Reads the recent world events from ``event_log``.
  2. Builds a dialogue prompt blending: anchor archetype + region lore +
     recent events + the player's recruited-figure roster.
  3. Calls a hosted free-tier provider, then falls back to the app's local-first
     gateway when no hosted key is configured.
  4. Caches one-shot greetings in Redis for ``CACHE_TTL_S`` seconds, keyed by a
     hash of (npc_id, archetype, recent-event-tail, figure-count).

Conversation turns use a short-lived Redis list keyed by conversation id. If the
hosted provider is unavailable or returns the configured stub, NPCs still answer
with deterministic personality-aware dialogue instead of a silent path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.gateway.gateway import gateway
from app.llm.hosted_adapter import STUB_RESPONSE, HostedAdapter
from app.llm.hosted_adapter import adapter as default_adapter
from app.redis_state import get_redis
from app.schemas import NPCAnchor, Region
from app.world import event_log, figures

CACHE_TTL_S = 30  # seconds; refreshed by event_tail invalidation, not just TTL
EVENT_TAIL = 6  # recent events included in the prompt + cache key
CONVERSATION_TTL_S = 15 * 60
MAX_CONVERSATION_TURNS = 8
NPC_SYSTEM_PROMPT = (
    "You are the dialogue engine for a pixel-art debate RPG. Answer as the NPC, "
    "stay in-world, and keep the response concise enough for a game chatbox."
)

_PERSONALITY_MODES = (
    "warm",
    "wry",
    "guarded",
    "earnest",
    "brisk",
    "curious",
)


def cache_key(npc_id: str, archetype: str, events_tail_hash: str, figure_count: int) -> str:
    """Stable Redis key. Tail-hash means a new event invalidates the entry."""
    return f"npctalk:{npc_id}:{archetype}:{events_tail_hash}:{figure_count}"


def conversation_key(run_id: str, npc_id: str, conversation_id: str) -> str:
    """Stable Redis key for one player/NPC conversation thread."""
    return f"npcchat:{run_id}:{npc_id}:{conversation_id}"


def events_tail_hash(events: list[event_log.Event]) -> str:
    """Hash the kind+filtered-data of the recent events tail, ignoring timestamps."""
    h = hashlib.md5()
    for evt in events[-EVENT_TAIL:]:
        h.update(evt.kind.encode("utf-8"))
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
            "Speak as a shopkeeper. Mention wares, prices, road supplies, or "
            "local rumors if appropriate. Do not offer dungeon-clearing quests."
        ),
        "quest_giver": (
            "Speak as a guard or captain. Offer or update a nearby "
            "dungeon-clearing quest if the player asks for work."
        ),
        "innkeeper": (
            "Speak warmly, like an innkeeper who knows everyone. Mention rest "
            "or making camp if appropriate."
        ),
        "figure": (
            "Speak in the voice of a famous historical figure. The player has "
            "not yet recruited you."
        ),
    }
    archetype_hint = archetype_hints.get(
        anchor.archetype, "Speak briefly and in character."
    )

    return (
        f"You are {anchor.name or 'an NPC'} ({anchor.archetype}). {region_blurb}\n\n"
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


def scripted_greeting(
    anchor: NPCAnchor,
    region: Region | None,
    events: list[event_log.Event] | None = None,
    recruited: list[figures.Figure] | None = None,
) -> str:
    """Generate a deterministic in-character greeting without network access."""
    events = events or []
    recruited = recruited or []
    name = anchor.name or "traveller"
    place = region.name if region is not None else "these roads"
    personality = _personality_for(anchor)
    roster_line = ""
    if recruited:
        roster_line = f" I see {recruited[0].name} travels in your company."
    event_line = _event_greeting_fragment(events)

    templates = {
        "villager": (
            f"{name} gives you a {personality} nod. Welcome to {place}; "
            f"keep your eyes open and your claims sharper than your boots.{event_line}"
        ),
        "merchant": (
            f"{name} greets you with a {personality} smile. My counter is open "
            f"if you need potions, camp tokens, or a sharper edge for the road.{event_line}"
        ),
        "quest_giver": (
            f"{name} studies your stance with a {personality} look. If you can "
            f"carry an argument, I have work that needs doing near {place}.{event_line}"
        ),
        "innkeeper": (
            f"{name} waves you toward the hearth with a {personality} welcome. "
            f"Make camp here and I will see your party rested before dawn.{event_line}"
        ),
        "figure": (
            f"{name} regards you with a {personality} patience. Speak carefully; "
            f"a mind is recruited by reasons, not noise.{roster_line}{event_line}"
        ),
    }
    return templates.get(anchor.archetype, templates["villager"])


def _personality_for(anchor: NPCAnchor) -> str:
    basis = f"{anchor.npc_id}:{anchor.name}:{anchor.archetype}"
    idx = int(hashlib.md5(basis.encode("utf-8")).hexdigest(), 16)
    return _PERSONALITY_MODES[idx % len(_PERSONALITY_MODES)]


def _event_greeting_fragment(events: list[event_log.Event]) -> str:
    for evt in reversed(events[-EVENT_TAIL:]):
        if evt.kind == "dungeon_cleared":
            name = evt.data.get("name") or evt.data.get("poi") or "the old den"
            return f" Word of {name} being cleared has already reached us."
        if evt.kind == "boss_defeated":
            boss = evt.data.get("boss_id") or "that tyrant"
            return f" People are still repeating how {boss} fell."
        if evt.kind == "battle_won":
            return " The town has heard you can win a debate when it counts."
    return ""


def _fallback_dialogue(
    anchor: NPCAnchor,
    region: Region | None,
    events: list[event_log.Event],
    player_message: str = "",
    recruited: list[figures.Figure] | None = None,
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
    return scripted_greeting(anchor, region, events, recruited)


def _is_stub_dialogue(text: str) -> bool:
    """True for adapter/cache placeholders that should become scripted lines."""
    normalized = text.strip().lower()
    return not normalized or normalized == STUB_RESPONSE.lower()


def _normalize_dialogue_text(
    text: str,
    anchor: NPCAnchor,
    region: Region | None,
    events: list[event_log.Event],
    player_message: str = "",
    recruited: list[figures.Figure] | None = None,
) -> str:
    """Replace empty/static LLM output with deterministic world-aware text."""
    stripped = (text or "").strip()
    if _is_stub_dialogue(stripped):
        return _fallback_dialogue(anchor, region, events, player_message, recruited)
    return stripped


def _dialogue_messages(prompt: str) -> list[dict[str, str]]:
    """Build gateway chat messages for NPC dialogue completions."""
    return [
        {"role": "system", "content": NPC_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


async def _complete_dialogue(
    prompt: str,
    *,
    adapter: HostedAdapter,
    max_tokens: int,
    temperature: float,
) -> str:
    """Run NPC text through hosted LLMs, then the local-first model gateway.

    The hosted adapter returns ``STUB_RESPONSE`` when no hosted keys are present.
    Treat that as a miss and ask the normal app gateway so local Ollama/Pareto
    setups still produce genuine model dialogue. Callers keep the deterministic
    fallback for the final no-model/no-network case.
    """
    try:
        hosted = await adapter.complete(
            prompt, max_tokens=max_tokens, temperature=temperature
        )
        if not _is_stub_dialogue(hosted):
            return hosted.strip()
    except Exception:  # noqa: BLE001 - gateway fallback handles hosted failures
        pass

    return (
        await gateway.complete(
            _dialogue_messages(prompt),
            model=settings.actor_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.llm_call_timeout_s,
        )
    ).strip()


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
    try:
        events = await event_log.recent(run_id, limit=event_log.MAX_EVENTS)
    except Exception:  # noqa: BLE001 - dialogue must always have a fallback
        events = []
    try:
        recruited = await figures.recruited_list(run_id)
    except Exception:  # noqa: BLE001 - roster flavor is optional
        recruited = []
    tail = events_tail_hash(events)
    key = cache_key(anchor.npc_id, anchor.archetype, tail, len(recruited))

    r = get_redis()
    message = (player_message or "").strip()
    if conversation_id is not None or message:
        conv_id = conversation_id or f"{anchor.npc_id}:{tail}"
        conv_key = conversation_key(run_id, anchor.npc_id, conv_id)
        try:
            history = await _load_history(r, conv_key)
        except Exception:  # noqa: BLE001 - conversations should degrade to stateless
            history = []
        prompt = build_prompt(
            anchor,
            region,
            events,
            recruited,
            history=history,
            player_message=message,
        )
        if message:
            try:
                await _append_turn(r, conv_key, "player", message)
            except Exception:  # noqa: BLE001
                pass
            history = [*history, {"role": "player", "text": message}]
        try:
            text = await _complete_dialogue(
                prompt,
                adapter=adapter,
                max_tokens=220,
                temperature=0.82,
            )
            text = _normalize_dialogue_text(
                text,
                anchor,
                region,
                events,
                message,
                recruited,
            )
        except Exception:  # noqa: BLE001 - scripted fallback is part of the contract
            text = _fallback_dialogue(anchor, region, events, message, recruited)
        try:
            await _append_turn(r, conv_key, "npc", text)
        except Exception:  # noqa: BLE001
            pass
        history = [*history, {"role": "npc", "text": text}]
        return DialogueResult(
            text=text,
            cached=False,
            cache_key=conv_key,
            conversation_id=conv_id,
            history=history[-MAX_CONVERSATION_TURNS * 2:],
        )

    cached = None
    try:
        cached = await r.get(key)
    except Exception:  # noqa: BLE001 - cache misses should degrade to dialogue
        cached = None
    if cached is not None:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        if not _is_stub_dialogue(cached):
            return DialogueResult(text=cached, cached=True, cache_key=key)

    prompt = build_prompt(anchor, region, events, recruited)
    try:
        text = await _complete_dialogue(
            prompt,
            adapter=adapter,
            max_tokens=160,
            temperature=0.8,
        )
        text = _normalize_dialogue_text(text, anchor, region, events, recruited=recruited)
    except Exception:  # noqa: BLE001 - scripted fallback is part of the contract
        text = scripted_greeting(anchor, region, events, recruited)
    try:
        await r.set(key, text, ex=CACHE_TTL_S)
    except Exception:  # noqa: BLE001 - returning dialogue matters more than cache
        pass
    return DialogueResult(text=text, cached=False, cache_key=key)
