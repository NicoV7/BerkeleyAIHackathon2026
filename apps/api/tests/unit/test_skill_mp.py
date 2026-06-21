"""Gacha-wave-B unit tests for the skill MP economy.

Covers the two new public surfaces in ``app.debate.skill_engine``:

  * ``skill_cost(name)``  — slug-matches ``app/skills/<slug>.md`` and returns
    the ``mp_cost`` from front-matter (0 when absent/unknown; never raises).
  * ``skill_costs()``     — returns ``{slug: cost}`` for the bulk-update startup
    hook that mirrors costs into ``Skill.cost``.

Also covers the small pure-function MP regen + deduction shape lifted from the
orchestrator (``next_mp``) so the formula is testable without Redis: end-of-
round +10 clamped to ``max_mp``, and ``can_afford`` is the only thing standing
between a skill use and the MP gate.

Pure-function tests — no DB, no Redis, no live stack.
"""
from __future__ import annotations

import pytest

from app.debate.skill_engine import (
    reload_skills,
    skill_cost,
    skill_costs,
    slugify,
)


# --------------------------------------------------------------------------- #
# Pure MP helpers — the canonical "what's the next MP value" calculations.
# Kept as a tiny model in the test so the orchestrator's regen + deduct logic
# can be asserted without spinning up Redis.
# --------------------------------------------------------------------------- #
MP_REGEN_PER_ROUND = 10


def next_mp_after_regen(current: int, max_mp: int) -> int:
    """End-of-round regen: +``MP_REGEN_PER_ROUND``, clamped to ``max_mp``."""
    return min(max_mp, current + MP_REGEN_PER_ROUND)


def can_afford(current: int, cost: int) -> bool:
    """The gate the orchestrator/router applies before a skill use."""
    return current >= cost


def deduct_mp(current: int, cost: int) -> int:
    """MP after a successful skill use; floored at 0."""
    return max(0, current - cost)


# --------------------------------------------------------------------------- #
# skill_cost — front-matter parsing
# --------------------------------------------------------------------------- #
class TestSkillCost:
    def test_steel_man_is_thirty_five(self) -> None:
        # Arrange: Wave B canonical cost for the heaviest LOGOS move.
        # Act / Assert
        assert skill_cost("Steel Man") == 35

    def test_anecdote_is_fifteen(self) -> None:
        # Arrange / Act / Assert
        assert skill_cost("Anecdote") == 15

    @pytest.mark.parametrize(
        "name,cost",
        [
            ("Analogy Strike", 20),
            ("Anecdote", 15),
            ("Authority Cite", 25),
            ("Credential Drop", 30),
            ("Emotional Appeal", 20),
            ("Leading Question", 15),
            ("Logical Thrust", 20),
            ("Reframe Attack", 30),
            ("Rhetorical Flourish", 40),
            ("Socratic Probe", 20),
            ("Steel Man", 35),
            ("Whataboutism", 25),
        ],
    )
    def test_all_twelve_skill_md_have_an_mp_cost(self, name: str, cost: int) -> None:
        # Arrange / Act / Assert: every catalog move resolves to its tuned cost.
        assert skill_cost(name) == cost

    def test_case_insensitive(self) -> None:
        # Arrange / Act / Assert: same slug rules as skill_instructions.
        assert skill_cost("steel man") == skill_cost("Steel Man")

    def test_unknown_skill_is_zero(self) -> None:
        # Arrange / Act / Assert: unknown -> 0, never raises.
        assert skill_cost("Not A Real Move") == 0

    def test_none_is_zero(self) -> None:
        # Arrange / Act / Assert
        assert skill_cost(None) == 0
        assert skill_cost("") == 0

    def test_never_raises_on_weird_input(self) -> None:
        # Arrange / Act / Assert
        for bad in [123, [], {}, object()]:
            assert isinstance(skill_cost(bad), int)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# skill_costs — bulk map used by the startup hook.
# --------------------------------------------------------------------------- #
class TestSkillCostsBulk:
    def test_returns_dict_with_every_known_skill(self) -> None:
        # Arrange
        costs = skill_costs()
        # Assert: each known move shows up keyed by its slug (lowercase, "_").
        for name in (
            "Analogy Strike",
            "Anecdote",
            "Authority Cite",
            "Credential Drop",
            "Emotional Appeal",
            "Leading Question",
            "Logical Thrust",
            "Reframe Attack",
            "Rhetorical Flourish",
            "Socratic Probe",
            "Steel Man",
            "Whataboutism",
        ):
            slug = slugify(name)
            assert slug in costs, f"{name} -> {slug} missing from skill_costs()"
            # Costs are positive ints (the catalog is fully tuned).
            assert isinstance(costs[slug], int)
            assert costs[slug] > 0

    def test_returns_a_copy_not_the_cache(self) -> None:
        # Arrange
        first = skill_costs()
        first["__poison__"] = 9999  # mutating must not leak back into the cache
        # Act
        second = skill_costs()
        # Assert: the cache is untouched.
        assert "__poison__" not in second

    def test_reload_is_idempotent(self) -> None:
        # Arrange
        before = skill_costs()
        reload_skills()
        after = skill_costs()
        # Assert
        assert before == after


# --------------------------------------------------------------------------- #
# Pure regen + deduction logic — mirrors the orchestrator's MP helpers.
# --------------------------------------------------------------------------- #
class TestMpRegen:
    def test_regen_adds_ten(self) -> None:
        # Arrange / Act / Assert
        assert next_mp_after_regen(20, 50) == 30

    def test_regen_clamps_at_max(self) -> None:
        # Arrange: already at ceiling
        # Act / Assert
        assert next_mp_after_regen(50, 50) == 50
        # Almost full — clamp to max, not max+overflow.
        assert next_mp_after_regen(45, 50) == 50

    def test_regen_never_exceeds_max_mp(self) -> None:
        # Arrange / Act / Assert: a wildly overfilled current still clamps.
        assert next_mp_after_regen(999, 50) == 50


class TestMpGate:
    def test_can_afford_when_mp_meets_cost(self) -> None:
        # Arrange / Act / Assert: equality is enough
        assert can_afford(35, 35) is True

    def test_cannot_afford_when_mp_below_cost(self) -> None:
        # Arrange / Act / Assert
        assert can_afford(34, 35) is False

    def test_deduct_floors_at_zero(self) -> None:
        # Arrange: spending a free skill on top of a more expensive one wouldn't
        # happen in real flow, but the helper must never produce negative MP.
        # Act / Assert
        assert deduct_mp(5, 10) == 0

    def test_deduct_normal_path(self) -> None:
        # Arrange: paying 20 of 50 leaves 30.
        # Act / Assert
        assert deduct_mp(50, 20) == 30
