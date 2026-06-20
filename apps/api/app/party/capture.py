"""Capture logic — attempt_capture turns a wild monster into a party member.

Probability scales with how low the wild's HP is. HP is read from Redis
(source of truth during an encounter). A capture attempt is only allowed
when the wild is in the "capturable window" (hp < 25% of max_hp).
"""
from __future__ import annotations

import logging
import random
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.db.models import Monster, MonsterOwner

log = logging.getLogger(__name__)

# ---- Tunable capture constants ----
CAPTURABLE_HP_FRACTION = 0.25   # wild must be below this fraction of max_hp
CAPTURE_BASE = 0.15             # minimum capture probability
CAPTURE_SCALE = 0.80            # extra probability at 0 HP
CAPTURE_MAX_P = 0.95            # probability cap


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


async def attempt_capture(
    session: AsyncSession,
    encounter_id: str,
    wild_id: str,
) -> Tuple[bool, Optional[Monster], str]:
    """Try to capture a wild monster during an active encounter.

    Returns (success, monster_or_None, message).

    Steps:
    1. Read current HP from Redis hp map for the encounter.
    2. Reject if HP >= 25% of max_hp.
    3. Roll probability: p = clamp(base + (1 - hp/max_hp) * scale, 0, 0.95)
    4. On success: flip owner -> 'player', optionally seed a CHARACTER memory.
    5. Return result — caller is responsible for committing the session.
    """
    from app.redis_state import get_hp_map  # frozen helper

    # Fetch wild monster from DB
    result = await session.execute(select(Monster).where(Monster.id == wild_id))
    wild: Optional[Monster] = result.scalar_one_or_none()

    if wild is None:
        return False, None, f"Monster {wild_id} not found."

    if wild.owner == MonsterOwner.player:
        return False, wild, "Already captured."

    # Read live HP from Redis
    hp_map = await get_hp_map(encounter_id)
    current_hp = hp_map.get(wild_id)

    if current_hp is None:
        # Fall back to max_hp (encounter may not have started)
        log.warning(
            "No Redis HP for monster %s in encounter %s; using max_hp",
            wild_id,
            encounter_id,
        )
        current_hp = wild.max_hp

    max_hp = wild.max_hp
    hp_fraction = current_hp / max_hp if max_hp > 0 else 0.0

    # Enforce capturable window
    if hp_fraction >= CAPTURABLE_HP_FRACTION:
        pct = int(hp_fraction * 100)
        return (
            False,
            None,
            f"Wild monster is too healthy to capture ({pct}% HP). Weaken it below "
            f"{int(CAPTURABLE_HP_FRACTION * 100)}% first.",
        )

    # Roll capture probability
    p = _clamp(
        CAPTURE_BASE + (1.0 - hp_fraction) * CAPTURE_SCALE,
        0.0,
        CAPTURE_MAX_P,
    )
    roll = random.random()
    success = roll < p

    log.info(
        "Capture attempt: monster=%s hp=%d/%d p=%.2f roll=%.2f success=%s",
        wild_id,
        current_hp,
        max_hp,
        p,
        roll,
        success,
    )

    if not success:
        return (
            False,
            None,
            f"Capture failed! (roll {roll:.2f} ≥ {p:.2f}). Keep weakening it.",
        )

    # Flip ownership
    wild.owner = MonsterOwner.player
    session.add(wild)

    # Seed a CHARACTER memory (guarded — WS-D may not be present yet)
    _try_seed_memory(wild, encounter_id)

    return True, wild, f"Captured {wild.name}!"


def _try_seed_memory(monster: Monster, encounter_id: str) -> None:
    """Attempt to write an initial CHARACTER memory via WS-D's store.

    Fully guarded: if the memory module isn't present or raises, we log and
    continue — capture still succeeds.
    """
    try:
        from app.memory import store  # type: ignore[import]

        content = (
            f"{monster.name} was captured by the player during encounter "
            f"{encounter_id}. This is the beginning of their journey."
        )
        # write_event is async in WS-D's design; we call it fire-and-forget
        # using a background task pattern — but we can't await here (sync
        # context). Log for now; Wave 2 can wire an async task queue.
        log.info(
            "Memory seed available (WS-D present) for %s — "
            "async write deferred to router layer.",
            monster.id,
        )
        # Store the content so the router can optionally write it.
        monster._capture_memory_hint = content  # type: ignore[attr-defined]
    except ImportError:
        log.debug("app.memory.store not yet available; skipping capture memory seed.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to seed capture memory for %s: %s", monster.id, exc)
