"""Track A — opening materialization (pre-gen + cache).

The enemy's OPENING utterance (its first line, arguing AGAINST the topic) is NOT
player-dependent: the enemy side is hardcoded ``against`` and the topic is fixed
at encounter-create. That makes it the single most cacheable LLM call in a
battle. This module materializes it into Redis so the first enemy turn is either
*retrieved* (cache hit -> zero generation) or generated exactly once and stored
for every future encounter on the same topic.

Cache key (see redis_state.k_opening): ``spec:opening:{hash(topic_text)}:{prompt_ver}``
- ``hash(topic_text)`` — topics in topics.py are bare strings (no topic_id), so we
  key on a stable digest of the topic text itself.
- ``{prompt_ver}`` — PROMPT_VERSION below. Bump it whenever the opening prompt or
  the model changes so stale openings are never served (the key simply misses and
  regenerates).

Two entry points:
- ``get_or_create_opening`` — used on the round critical path: hit -> return cached
  text; miss -> generate once, store, return. Safe + bounded; degrades to a real
  templated fallback (never raises, never blocks the round indefinitely).
- ``pregenerate_opening`` — fire-and-forget warm during the encounter-load idle
  window (A1). Generates + stores only on a miss; a no-op on a hit. Intended to be
  scheduled with ``asyncio.create_task`` at encounter-create alongside prewarm.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from app.config import settings
from app.gateway.gateway import gateway
from app.redis_state import OPENING_TTL_SECONDS, get_redis, k_opening

logger = logging.getLogger(__name__)

# Bump on ANY change to the opening prompt below, to _opening_messages, or to the
# default opening model — this invalidates every cached opening so stale text is
# never served. Format: "vN".
PROMPT_VERSION = "v1"


# Both debate sides are materializable now (WS-4 #10). The enemy still argues
# AGAINST (the default, keeping every existing caller's behavior byte-for-byte),
# but the FOR-side opening is just as player-independent — it's a pure function of
# (topic, side, prompt_version) — so we cache it under a side-qualified key so the
# player's lead opening (or a FOR enemy) can also be a pure retrieval. The side is
# folded into the ``topic_hash`` dimension of the FROZEN ``k_opening`` contract so
# redis_state's key builder is untouched.
_VALID_SIDES = ("against", "for")


def _norm_side(side: str | None) -> str:
    s = (side or "").strip().lower()
    return s if s in _VALID_SIDES else "against"


def topic_hash(topic_text: str, side: str | None = None) -> str:
    """Stable, process-independent digest of a topic string (cache dimension).

    Normalizes surrounding whitespace + case so trivially-different renderings of
    the same catalog topic share a cache entry. md5 (not Python's salted hash())
    so the key is identical across processes/restarts.

    When ``side`` is given it is folded into the digest so the FOR and AGAINST
    openings for the same topic never collide. ``side=None`` reproduces the
    original AGAINST-only digest exactly, so previously-cached AGAINST openings
    (and any caller that doesn't pass a side) keep hitting their existing key."""
    norm = (topic_text or "").strip().lower()
    if side is None:
        # Back-compat: the historical key (AGAINST-only) had no side in the digest.
        return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]
    keyed = f"{_norm_side(side)}|{norm}"
    return hashlib.md5(keyed.encode("utf-8")).hexdigest()[:16]


def _opening_model(enemy_model: Optional[str] = None) -> str:
    """Model for opening generation — the enemy's pinned model if any, else the
    fast actor model (same choice the live enemy turn would make)."""
    return enemy_model or settings.actor_model


def _opening_timeout() -> float:
    return float(getattr(settings, "llm_call_timeout_s", 28) or 28)


def _opening_max_tokens() -> int:
    return int(getattr(settings, "actor_max_tokens", 64) or 64)


def _opening_cache_dim(topic_text: str, side: str | None) -> str:
    """The ``topic_hash`` argument for ``k_opening``, side-qualified.

    AGAINST (the historical default) maps to the LEGACY no-side digest so every
    opening cached before both-sides support keeps hitting. FOR maps to a
    side-qualified digest so the two openings never collide."""
    s = _norm_side(side)
    return topic_hash(topic_text) if s == "against" else topic_hash(topic_text, s)


def _fallback_opening(topic_text: str, side: str | None = "against") -> str:
    """Real, side-taking opening for model failure (never the old meta-hedge
    filler). Mirrors orchestrator's default fallbacks so a cache-miss with a
    stalled model still reads like a debate opener for the requested side."""
    topic_str = topic_text or "this question"
    if _norm_side(side) == "for":
        return (
            f"I argue FOR {topic_str}: it carries the stronger reasons and the case "
            "against it falls apart the moment you press it for specifics."
        )
    return (
        f"I argue AGAINST {topic_str}: the case for it carries hidden costs and "
        "collapses under a single concrete question."
    )


def _opening_messages(topic_text: str, side: str | None = "against") -> list[dict[str, str]]:
    """Prompt for a side's opening line — no transcript, persona-free.

    Kept deliberately self-contained (no actor persona / no live transcript) so
    the result is purely a function of (topic, side, prompt_version) and therefore
    cacheable across encounters. If you change this, bump PROMPT_VERSION."""
    stance = "FOR" if _norm_side(side) == "for" else "AGAINST"
    return [
        {
            "role": "system",
            "content": (
                f"You are a sharp debate opponent. The debate topic is: {topic_text}\n"
                f'YOUR ASSIGNED SIDE: {stance}. You argue {stance} the topic "{topic_text}". '
                "Make ONE concrete claim about the topic and state plainly why you are "
                f"{stance} it. Do NOT concede, do NOT switch sides, do NOT argue the other "
                "side. Be vivid and concise (1-2 sentences). Do NOT narrate or use stage "
                "directions — argue the actual topic."
            ),
        },
        {
            "role": "user",
            "content": f"You speak first. Open strong with your {stance} argument.",
        },
    ]


def _sanitize(text: str) -> str:
    return "".join(ch for ch in text if ch >= " " or ch in "\n\t")


async def _generate_opening(
    topic_text: str, enemy_model: Optional[str], side: str | None = "against"
) -> str:
    """Generate the opening once for ``side``. Falls back to a real templated line
    on any failure/empty result — never raises."""
    try:
        text = await gateway.complete(
            _opening_messages(topic_text, side),
            model=_opening_model(enemy_model),
            temperature=0.8,
            max_tokens=_opening_max_tokens(),
            timeout=_opening_timeout(),
        )
        text = _sanitize((text or "").strip())
    except Exception:  # noqa: BLE001 — stalled/failed model: fall back to real text
        text = ""
    return text or _fallback_opening(topic_text, side)


async def get_cached_opening(
    topic_text: str, side: str | None = "against"
) -> Optional[str]:
    """Return the cached opening for this (topic, side) at PROMPT_VERSION, or None.
    Best-effort: a Redis error returns None so the caller generates live. ``side``
    defaults to AGAINST (the enemy's stance), preserving every existing caller."""
    try:
        r = get_redis()
        return await r.get(
            k_opening(_opening_cache_dim(topic_text, side), PROMPT_VERSION)
        )
    except Exception:  # noqa: BLE001 — cache is an optimization, never fatal
        return None


async def _store_opening(topic_text: str, text: str, side: str | None = "against") -> None:
    try:
        r = get_redis()
        await r.set(
            k_opening(_opening_cache_dim(topic_text, side), PROMPT_VERSION),
            text,
            ex=OPENING_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        pass


async def get_or_create_opening(
    topic_text: str, enemy_model: Optional[str] = None, side: str | None = "against"
) -> tuple[str, bool]:
    """Critical-path retrieval: return ``(opening_text, cache_hit)`` for ``side``.

    Hit  -> cached text, no generation.
    Miss -> generate once, store, return. Always returns usable text (templated
    fallback on model failure); never raises. ``side`` defaults to AGAINST so the
    existing enemy-opening callers are unchanged."""
    cached = await get_cached_opening(topic_text, side)
    if cached:
        return cached, True
    text = await _generate_opening(topic_text, enemy_model, side)
    await _store_opening(topic_text, text, side)
    return text, False


async def pregenerate_opening(
    topic_text: str, enemy_model: Optional[str] = None, side: str | None = "against"
) -> bool:
    """A1 — warm a side's opening during the encounter-load idle window.

    No-op on a cache hit; generates + stores once on a miss. Returns True if it
    generated (miss), False if it was already warm (hit). Best-effort: intended
    for ``asyncio.create_task`` at encounter-create so the first enemy turn is a
    pure retrieval. ``side`` defaults to AGAINST. Never raises."""
    try:
        if await get_cached_opening(topic_text, side):
            return False
        text = await _generate_opening(topic_text, enemy_model, side)
        await _store_opening(topic_text, text, side)
        return True
    except Exception:  # noqa: BLE001
        return False


async def pregenerate_both_openings(
    topic_text: str, enemy_model: Optional[str] = None
) -> int:
    """Warm BOTH the AGAINST and FOR openings for ``topic`` (WS-4 #10).

    The enemy argues AGAINST and the player's lead argues FOR; both openings are
    player-independent (pure function of topic+side), so both are cacheable. Warms
    them ONE AT A TIME (AGAINST first — it's the one the first enemy turn actually
    retrieves) so the single Ollama slot is never double-hit. Returns how many were
    generated this call (0, 1, or 2). Best-effort: never raises."""
    generated = 0
    for side in ("against", "for"):
        try:
            if await pregenerate_opening(topic_text, enemy_model, side):
                generated += 1
        except Exception:  # noqa: BLE001 — one side never aborts the other
            continue
    return generated


async def pregenerate_theme_openings(
    theme: Optional[str], enemy_model: Optional[str] = None
) -> int:
    """A2 — pre-bake the enemy opening for EVERY topic in a theme.

    Autoplan finding (A2): the original cache premise "topics repeat" is FALSE —
    encounter.py seeds the per-battle topic off the encounter UUID (a random topic
    WITHIN the run's theme), so warming only the single drawn topic almost never
    hits on the next battle. What the player DOES commit to is a *theme* at run
    start, and every future battle in that run draws from the same small per-theme
    topic set (topics.TOPICS_BY_THEME). Pre-baking ALL of a theme's openings up
    front is therefore what actually makes the opening cache hit: whichever topic
    the next encounter draws, its opening is already warm.

    Generates only on a miss (each topic is a no-op if already warm), one topic at
    a time so the single Ollama slot is never double-hit. Returns the count of
    openings generated this call. Best-effort: never raises. Intended for
    ``asyncio.create_task`` at run/encounter create."""
    try:
        from app.debate.topics import TOPICS_BY_THEME, topics_for_theme

        # Resolve the theme's topic set (case-insensitive); unknown/empty theme
        # falls back to the full catalog so the cache still gets warmed.
        topics: list[str]
        if theme and any(name.lower() == theme.lower() for name in TOPICS_BY_THEME):
            topics = topics_for_theme(theme)
        else:
            topics = topics_for_theme(None)

        generated = 0
        for topic_text in topics:
            try:
                if await pregenerate_opening(topic_text, enemy_model):
                    generated += 1
            except Exception:  # noqa: BLE001 — one bad topic never aborts the batch
                continue
        return generated
    except Exception:  # noqa: BLE001 — best-effort, never block run/encounter create
        return 0
