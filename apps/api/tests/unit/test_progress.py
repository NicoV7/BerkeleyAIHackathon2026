"""T1 backend unit — party progression math.

Pure-logic coverage for ``app.party.progress`` with NO live DB / Redis:

  * ``award_xp`` / ``maybe_evolve``:
      level curve (xp_needed = 100 * level), +10 max_hp per level, skill-slot
      unlocks at levels 3/6/9, and evolution_stage bumps at levels 5/10.

The capture probability tests that used to live here were removed when the
capture acquisition flow was replaced by the gacha system (see the gacha-wave
design doc and ``app.routers.gacha``); ``attempt_capture`` and its module no
longer exist.

Everything here runs against a ``FakeMonster`` plain object and a ``FakeSession``
that records ``.add()`` calls. Collection is ALWAYS safe on a bare host.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

# Import-time only touches pure Python; if the impl fleet has not landed the
# module yet, skip the whole file rather than erroring collection.
progress = pytest.importorskip("app.party.progress")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeMonster:
    """Minimal stand-in for app.db.models.Monster for synchronous progress math.

    Only the attributes touched by award_xp / maybe_evolve are modelled.
    """

    def __init__(
        self,
        *,
        level: int = 1,
        xp: int = 0,
        max_hp: int = 100,
        evolution_stage: int = 0,
        skills: Optional[list] = None,
        id: str = "mon-test",
    ) -> None:
        self.level = level
        self.xp = xp
        self.max_hp = max_hp
        self.evolution_stage = evolution_stage
        self.skills = [] if skills is None else skills
        self.id = id


class FakeSession:
    """Captures .add() calls; award_xp must NOT commit (caller owns the txn)."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commit_called = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:  # pragma: no cover - presence is the assertion
        self.commit_called = True


# --------------------------------------------------------------------------- #
# progress.xp_needed — the level curve
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("level", "expected"),
    [(1, 100), (2, 200), (3, 300), (10, 1000)],
)
def test_xp_needed_is_one_hundred_times_current_level(level: int, expected: int) -> None:
    # Arrange / Act
    needed = progress.xp_needed(level)
    # Assert
    assert needed == expected
    assert needed == progress.XP_PER_LEVEL * level


# --------------------------------------------------------------------------- #
# progress.award_xp — basic accrual & single level-up
# --------------------------------------------------------------------------- #
def test_award_xp_below_threshold_accrues_without_levelling() -> None:
    # Arrange
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    session = FakeSession()
    # Act
    result = progress.award_xp(session, monster, 50)
    # Assert
    assert result["levelled"] is False
    assert monster.xp == 50
    assert monster.level == 1
    assert monster.max_hp == 100
    assert session.commit_called is False  # caller owns the transaction


def test_award_xp_at_exact_threshold_levels_up_once_and_adds_hp() -> None:
    # Arrange — exactly 100 XP is enough to clear level 1's curve.
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    session = FakeSession()
    # Act
    result = progress.award_xp(session, monster, 100)
    # Assert
    assert result["levelled"] is True
    assert result["new_level"] == 2
    assert monster.level == 2
    assert monster.xp == 0  # 100 awarded - 100 spent
    assert monster.max_hp == 100 + progress.HP_PER_LEVEL  # +10 hp/level
    assert monster in session.added


def test_award_xp_carries_remainder_after_level_up() -> None:
    # Arrange — 150 clears level 1 (100) leaving 50 toward level 2.
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    # Act
    result = progress.award_xp(FakeSession(), monster, 150)
    # Assert
    assert result["new_level"] == 2
    assert monster.xp == 50


def test_award_xp_none_monster_is_a_noop() -> None:
    # Arrange / Act
    result = progress.award_xp(FakeSession(), None, 999)
    # Assert
    assert result == {"levelled": False}


# --------------------------------------------------------------------------- #
# progress.award_xp — multi-level dumps and the +10 hp/level invariant
# --------------------------------------------------------------------------- #
def test_award_xp_large_dump_gains_multiple_levels_with_cumulative_hp() -> None:
    # Arrange — 100 + 200 = 300 XP spans level 1 -> 2 -> 3 (300 carries to L3 cost).
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    # Act — need 100 (L1) + 200 (L2) = 300 to reach level 3.
    result = progress.award_xp(FakeSession(), monster, 300)
    # Assert
    assert monster.level == 3
    assert result["new_level"] == 3
    assert monster.xp == 0
    # +10 per level gained (two level-ups: 1->2, 2->3) = +20.
    assert monster.max_hp == 100 + 2 * progress.HP_PER_LEVEL


# --------------------------------------------------------------------------- #
# progress.award_xp — skill unlocks at levels 3 / 6 / 9
# --------------------------------------------------------------------------- #
def test_award_xp_unlocks_skill_when_reaching_level_three() -> None:
    # Arrange — start at level 2 so a single level-up lands exactly on 3.
    monster = FakeMonster(level=2, xp=0, max_hp=110, skills=[])
    # Act — level 2 needs 200 XP to reach level 3.
    result = progress.award_xp(FakeSession(), monster, 200)
    # Assert
    assert monster.level == 3
    assert "skill_L3" in monster.skills
    assert "skill_L3" in result["skills_unlocked"]


def test_award_xp_does_not_unlock_skill_at_non_milestone_level() -> None:
    # Arrange — reaching level 2 is not a milestone (only 3/6/9).
    monster = FakeMonster(level=1, xp=0, skills=[])
    # Act
    result = progress.award_xp(FakeSession(), monster, 100)
    # Assert
    assert monster.level == 2
    assert monster.skills == []
    assert result["skills_unlocked"] == []


def test_award_xp_skill_unlock_is_idempotent_for_existing_tag() -> None:
    # Arrange — monster already carries the L3 tag; reaching L3 must not dupe it.
    monster = FakeMonster(level=2, xp=0, skills=["skill_L3"])
    # Act
    result = progress.award_xp(FakeSession(), monster, 200)
    # Assert
    assert monster.skills.count("skill_L3") == 1
    assert result["skills_unlocked"] == []  # nothing newly unlocked


# --------------------------------------------------------------------------- #
# progress.maybe_evolve — evolution_stage thresholds at level 5 and 10
# --------------------------------------------------------------------------- #
def test_maybe_evolve_below_first_threshold_does_not_evolve() -> None:
    # Arrange
    monster = FakeMonster(level=4, evolution_stage=0, max_hp=130)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is False
    assert monster.evolution_stage == 0
    assert monster.max_hp == 130  # no evolution HP bonus applied


def test_maybe_evolve_at_level_five_advances_to_stage_one_with_hp_bonus() -> None:
    # Arrange
    monster = FakeMonster(level=5, evolution_stage=0, max_hp=140)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is True
    assert monster.evolution_stage == 1
    assert monster.max_hp == 140 + 20  # +20 evolution bonus


def test_maybe_evolve_at_level_ten_advances_to_stage_two() -> None:
    # Arrange — a monster at level 10 still on stage 0 jumps straight to stage 2.
    monster = FakeMonster(level=10, evolution_stage=0, max_hp=200)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is True
    assert monster.evolution_stage == 2
    assert monster.max_hp == 200 + 20


def test_maybe_evolve_is_idempotent_once_at_target_stage() -> None:
    # Arrange — already stage 1 at level 6; re-running must not re-bump or re-heal.
    monster = FakeMonster(level=6, evolution_stage=1, max_hp=160)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is False
    assert monster.evolution_stage == 1
    assert monster.max_hp == 160


def test_award_xp_reports_evolved_when_crossing_level_five() -> None:
    # Arrange — start at level 4; one level-up to 5 should trip evolution.
    monster = FakeMonster(level=4, xp=0, evolution_stage=0, max_hp=130)
    # Act — level 4 needs 400 XP to reach level 5.
    result = progress.award_xp(FakeSession(), monster, 400)
    # Assert
    assert monster.level == 5
    assert result["evolved"] is True
    assert monster.evolution_stage == 1

