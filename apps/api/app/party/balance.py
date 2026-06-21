"""Central balance / tunables for the Debate RPG (Agent 3: BALANCE + PROGRESSION).

ONE place to tune the numbers that make type / skill / level choices visibly
change battle outcomes. Everything here is a **pure function** of integers/floats
with NO I/O — so it is trivially unit-testable and safe to import anywhere
(damage engine, progression, seed scripts, balancing notebooks).

Three families of knobs live here:

  * Level -> stat curve      : ``hp_for_level`` / ``hp_bonus_for_level``
  * XP economy               : ``xp_to_next`` / ``xp_reward`` / ``total_xp_for_level``
  * Evolution thresholds     : ``should_evolve`` / ``evolution_stage_for_level``
                               / ``evolution_hp_bonus``

The defaults are chosen to be **behaviorally identical** to the constants that
``app.party.progress`` shipped with (and that the existing
``tests/unit/test_capture_progress.py`` pins):

    base HP at level 1   = 100      (Monster.max_hp default)
    +10 max_hp per level
    xp needed for level L = 100 * L
    evolution at level 5 -> stage 1, level 10 -> stage 2
    +20 max_hp bonus on each evolution

Keeping these as the documented defaults means importing/using ``balance`` does
not change battle math; rebalancing is a one-line edit here.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# Tunable constants (edit these to rebalance the whole game)
# --------------------------------------------------------------------------- #

#: max_hp a freshly-spawned (level 1, stage 0) monster starts with.
BASE_HP: int = 100

#: max_hp gained on every single level-up.
HP_PER_LEVEL: int = 10

#: ATK gained on every single level-up (gacha wave).
#: Feeds the ``attacker_atk`` term in ``compute_damage`` so each level visibly
#: hits harder. Mirrors :data:`HP_PER_LEVEL`'s additive curve.
ATK_PER_LEVEL: int = 2

#: DEF gained on every single level-up (gacha wave).
#: Feeds the ``defender_def`` term in ``compute_damage`` so each level absorbs
#: a little more incoming damage.
DEF_PER_LEVEL: int = 2

#: max_mp (and a full MP refill) gained on every single level-up (gacha wave).
#: Gates ability use, so an extra slice of MP per level keeps Memory Recall
#: (60 MP) reachable as monsters grow.
MP_PER_LEVEL: int = 5

#: max_hp gained each time the monster crosses an evolution threshold.
EVOLUTION_HP_BONUS: int = 20

#: XP needed to clear a given level scales linearly: XP_PER_LEVEL * level.
XP_PER_LEVEL: int = 100

#: Level -> evolution_stage thresholds. A monster at/above the level gets the
#: stage; the highest satisfied threshold wins (e.g. spawning at L10 -> stage 2).
EVOLVE_STAGES: dict[int, int] = {5: 1, 10: 2}

#: Levels at which a generic "new skill" slot unlocks during level-up.
SKILL_UNLOCK_LEVELS: frozenset[int] = frozenset({3, 6, 9})

#: XP reward tuning for finishing an encounter.
XP_REWARD_BASE: int = 50          # flat reward for any decisive outcome
XP_REWARD_PER_ENEMY_LEVEL: int = 10  # bonus scaled by the defeated enemy's level
XP_WIN_MULTIPLIER: float = 1.0    # full reward on a win
XP_LOSS_MULTIPLIER: float = 0.25  # consolation XP on a loss


# --------------------------------------------------------------------------- #
# Level -> stat curve
# --------------------------------------------------------------------------- #


def hp_bonus_for_level(level: int) -> int:
    """Cumulative max_hp gained from levelling *up to* ``level`` (excludes base).

    Level 1 has gained nothing yet (you start there); each level after adds
    :data:`HP_PER_LEVEL`. Monotonic non-decreasing in ``level``.
    """
    return max(0, int(level) - 1) * HP_PER_LEVEL


def hp_for_level(level: int, *, evolution_stage: int = 0) -> int:
    """Total max_hp for a monster at ``level`` (and optional evolution stage).

    This is the *curve* — useful for sanity-checking, spawning enemies at a
    target level, or balancing. It composes the base HP, the per-level bonus,
    and any evolution bonuses already earned by that stage.

    Monotonic non-decreasing in both ``level`` and ``evolution_stage``.
    """
    return BASE_HP + hp_bonus_for_level(level) + EVOLUTION_HP_BONUS * max(0, int(evolution_stage))


# --------------------------------------------------------------------------- #
# XP economy
# --------------------------------------------------------------------------- #


def xp_to_next(level: int) -> int:
    """XP required to advance from ``level`` to ``level + 1``.

    Linear curve: ``XP_PER_LEVEL * level`` (100, 200, 300, ...). Strictly
    increasing in ``level``.
    """
    return XP_PER_LEVEL * max(1, int(level))


def total_xp_for_level(level: int) -> int:
    """Total cumulative XP needed to *reach* ``level`` from level 1.

    Sum of ``xp_to_next`` over 1..level-1. Level 1 needs 0. Monotonic.
    """
    lvl = max(1, int(level))
    return sum(xp_to_next(l) for l in range(1, lvl))


def xp_reward(enemy_level: int, *, won: bool = True) -> int:
    """XP a player monster earns for an encounter against ``enemy_level``.

    Scales with the enemy's level so beating tougher foes is worth more, and a
    loss still yields a small consolation amount so progression never stalls.
    """
    raw = XP_REWARD_BASE + XP_REWARD_PER_ENEMY_LEVEL * max(0, int(enemy_level))
    mult = XP_WIN_MULTIPLIER if won else XP_LOSS_MULTIPLIER
    return max(0, int(round(raw * mult)))


# --------------------------------------------------------------------------- #
# Evolution thresholds
# --------------------------------------------------------------------------- #


def evolution_stage_for_level(level: int) -> int:
    """Highest evolution stage a monster at ``level`` is entitled to.

    Returns 0 below the first threshold. Monotonic non-decreasing in ``level``.
    """
    stage = 0
    for lvl_threshold, target_stage in sorted(EVOLVE_STAGES.items()):
        if int(level) >= lvl_threshold:
            stage = target_stage
    return stage


def should_evolve(level: int, current_stage: int) -> bool:
    """True if a monster at ``level`` has out-grown its ``current_stage``.

    Used by progression to decide whether to trigger an evolution (which both
    bumps stats AND mutates the genome's behavior).
    """
    return evolution_stage_for_level(level) > int(current_stage)


def evolution_hp_bonus() -> int:
    """max_hp granted by a single evolution step (one stage advance)."""
    return EVOLUTION_HP_BONUS
