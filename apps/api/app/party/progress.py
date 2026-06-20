"""Party progression — XP, levelling, evolution, skill unlocks.

Consumed by:
  - WS-B encounter finalize: `from app.party.progress import award_xp`  (guarded import)
  - WS-E capture router on post-capture progression
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models import Monster

log = logging.getLogger(__name__)

# ---- Tunable constants ----

XP_PER_LEVEL = 100          # xp needed = XP_PER_LEVEL * current_level
HP_PER_LEVEL = 10           # max_hp bonus on level-up
EVOLVE_STAGES = {5: 1, 10: 2}  # level threshold -> evolution_stage value
SKILL_UNLOCK_LEVELS = {3, 6, 9}  # levels at which a generic "new skill" slot unlocks


def xp_needed(level: int) -> int:
    """XP needed to reach next level from current level."""
    return XP_PER_LEVEL * level


def award_xp(session, monster: "Monster", amount: int) -> dict:
    """Add XP to a party monster; level up if threshold reached.

    Returns a dict describing what happened so callers can surface it.
    Works synchronously — caller must flush the session (add/commit).
    Does NOT commit; the caller owns the transaction.
    """
    if monster is None:
        return {"levelled": False}

    levelled_up = False
    skill_unlocked: list[str] = []
    evolved = False

    monster.xp += amount

    # Level-up loop (could gain multiple levels from a big XP dump)
    while monster.xp >= xp_needed(monster.level):
        monster.xp -= xp_needed(monster.level)
        monster.level += 1
        monster.max_hp += HP_PER_LEVEL
        levelled_up = True
        log.info("Monster %s levelled up to %d", monster.id, monster.level)

        if monster.level in SKILL_UNLOCK_LEVELS:
            tag = f"skill_L{monster.level}"
            # Append to the JSONB skills list if not already present
            if isinstance(monster.skills, list) and tag not in monster.skills:
                monster.skills = list(monster.skills) + [tag]
                skill_unlocked.append(tag)

        maybe_evolve(monster)
        if evolved is False and monster.evolution_stage > 0:
            evolved = True

    session.add(monster)

    return {
        "levelled": levelled_up,
        "new_level": monster.level,
        "skills_unlocked": skill_unlocked,
        "evolved": evolved,
    }


def maybe_evolve(monster: "Monster") -> bool:
    """Bump evolution_stage if monster has hit an evolution threshold.

    Called automatically inside award_xp; can also be called standalone.
    Returns True if the monster evolved.
    """
    target_stage = 0
    for lvl_threshold, stage in sorted(EVOLVE_STAGES.items()):
        if monster.level >= lvl_threshold:
            target_stage = stage

    if target_stage > monster.evolution_stage:
        monster.evolution_stage = target_stage
        monster.max_hp += 20  # bonus HP on evolution
        log.info(
            "Monster %s evolved to stage %d", monster.id, monster.evolution_stage
        )
        return True
    return False
