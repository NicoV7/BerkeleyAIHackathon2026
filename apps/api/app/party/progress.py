"""Party progression — XP, levelling, evolution, skill unlocks.

Consumed by:
  - WS-B encounter finalize: `from app.party.progress import award_xp`  (guarded import)
  - WS-E capture router on post-capture progression
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.party import balance

if TYPE_CHECKING:
    from app.db.models import Monster

log = logging.getLogger(__name__)

# ---- Tunable constants ----
#
# All tunables now live in `app.party.balance` (the single balance module). The
# names below are kept as thin re-exports so existing imports / tests that read
# `progress.XP_PER_LEVEL` etc. keep working unchanged.

XP_PER_LEVEL = balance.XP_PER_LEVEL          # xp needed = XP_PER_LEVEL * current_level
HP_PER_LEVEL = balance.HP_PER_LEVEL          # max_hp bonus on level-up
EVOLVE_STAGES = balance.EVOLVE_STAGES        # level threshold -> evolution_stage value
SKILL_UNLOCK_LEVELS = set(balance.SKILL_UNLOCK_LEVELS)  # levels unlocking a skill slot


def xp_needed(level: int) -> int:
    """XP needed to reach next level from current level."""
    return balance.xp_to_next(level)


def award_xp(session, monster: "Monster", amount: int) -> dict:
    """Add XP to a party monster; level up if threshold reached.

    Returns a dict describing what happened so callers can surface it. Works
    synchronously — caller must flush the session (add/commit). Does NOT
    commit; the caller owns the transaction.

    Gacha-wave additions: every level-up also applies +ATK/+DEF/+MP gains (see
    :mod:`app.party.balance`) and fully refills the monster's current MP. The
    returned dict carries a ``stat_gains`` sub-dict — the sum of per-level
    bonuses across the entire dump — so the WS layer / frontend cinematic can
    surface "+N ATK / +N DEF / +N MP / +N HP" without re-deriving from the
    diff. The atk/def_/max_mp/mp writes are tolerant of objects that don't
    declare those attributes (older test fakes) so the legacy contract is
    preserved.
    """
    if monster is None:
        return {"levelled": False}

    levelled_up = False
    skill_unlocked: list[str] = []
    evolved = False
    # Per-level bonuses accumulate across a multi-level dump (e.g. 300 XP from
    # level 1 banks two level-ups → +2*HP_PER_LEVEL, +2*ATK_PER_LEVEL, etc).
    stat_gains = {"atk": 0, "def": 0, "mp": 0, "hp": 0}

    monster.xp += amount

    # Level-up loop (could gain multiple levels from a big XP dump)
    while monster.xp >= xp_needed(monster.level):
        monster.xp -= xp_needed(monster.level)
        monster.level += 1
        monster.max_hp += balance.HP_PER_LEVEL
        stat_gains["hp"] += balance.HP_PER_LEVEL

        # ---- Gacha-wave stat gains ----
        # ATK / DEF / max_mp are Monster columns added in Wave 0. Guard with
        # hasattr so unit-test fakes (FakeMonster in test_progress.py) that
        # only model the original HP/XP/level fields keep working unchanged.
        if hasattr(monster, "atk"):
            monster.atk = int(getattr(monster, "atk", 0) or 0) + balance.ATK_PER_LEVEL
            stat_gains["atk"] += balance.ATK_PER_LEVEL
        if hasattr(monster, "def_"):
            monster.def_ = int(getattr(monster, "def_", 0) or 0) + balance.DEF_PER_LEVEL
            stat_gains["def"] += balance.DEF_PER_LEVEL
        if hasattr(monster, "max_mp"):
            monster.max_mp = int(getattr(monster, "max_mp", 0) or 0) + balance.MP_PER_LEVEL
            stat_gains["mp"] += balance.MP_PER_LEVEL
            # Full refill on level-up — the player just earned a moment of
            # power, so abilities should be usable right away.
            if hasattr(monster, "mp"):
                monster.mp = monster.max_mp

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
        # Sum across all level-ups in this dump. Zero for a no-op (sub-threshold
        # XP gain) so the WS layer can `if any(stat_gains.values())` cheaply.
        "stat_gains": stat_gains,
    }


def maybe_evolve(monster: "Monster") -> bool:
    """Evolve the monster if it has out-grown its current evolution stage.

    Evolution changes BOTH stats and BEHAVIOR:
      * stats   — bump ``evolution_stage`` and add the evolution HP bonus.
      * genome  — APPEND an "evolved" persona note + skill fragment so the
        monster actually debates differently after evolving.

    CRITICAL: the genome edit is append-only. It never overwrites a trained
    ``system_prompt`` or ``gambit_rules`` (those may carry GEPA/GRPO training);
    it only layers a new fragment/note on top via
    :func:`app.training.genome.append_fragment`. ``genome_version`` is bumped so
    downstream caches know the behavior changed.

    Called automatically inside award_xp; can also be called standalone.
    Returns True if the monster evolved.
    """
    target_stage = balance.evolution_stage_for_level(monster.level)

    if target_stage > monster.evolution_stage:
        monster.evolution_stage = target_stage
        monster.max_hp += balance.evolution_hp_bonus()  # bonus HP on evolution
        _mutate_genome_on_evolution(monster, target_stage)
        log.info(
            "Monster %s evolved to stage %d", monster.id, monster.evolution_stage
        )
        return True
    return False


def _mutate_genome_on_evolution(monster: "Monster", stage: int) -> None:
    """Append an evolved descriptor to the monster's genome (behavior change).

    Read -> append-only merge -> write-back, never clobbering trained fields.
    Tolerant of monster-like objects that lack genome attributes (e.g. test
    fakes): if there is nothing to read/write, it is a safe no-op.
    """
    # Local imports: keep this module importable without the training/db stack
    # and avoid import cycles.
    try:
        from app.training import genome as G
    except Exception:  # noqa: BLE001 - training stack optional in some contexts
        return

    # Only operate on objects that actually carry a genome (real Monster rows).
    if not hasattr(monster, "harness") and not hasattr(monster, "persona"):
        return

    fragment = (
        f"You have EVOLVED (stage {stage}): press your advantages harder and "
        "open with a bolder, more commanding frame than before."
    )
    persona_note = f"Evolved to stage {stage}: sharper, more assertive presence."

    current = G.read_genome(monster)
    evolved = G.append_fragment(current, fragment, persona_note=persona_note)

    # Write back ONLY the additive pieces. Fold fragments back into harness the
    # same way apply_genome does, but leave trained system_prompt / gambit_rules
    # untouched.
    new_harness = dict(getattr(monster, "harness", {}) or {})
    new_harness["skill_prompt_fragments"] = list(evolved.get("skill_prompt_fragments", []))
    monster.harness = new_harness
    monster.persona = dict(evolved.get("persona", {}))

    monster.genome_version = int(getattr(monster, "genome_version", 1) or 1) + 1
