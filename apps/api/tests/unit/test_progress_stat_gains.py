"""Gacha Wave D — per-level stat gains in ``app.party.progress.award_xp``.

Companion to ``tests/unit/test_progress.py`` covering the Wave-D additions:
every level-up must bump ATK / DEF / MP / max_HP by the constants in
``app.party.balance`` AND surface those gains via the returned ``stat_gains``
dict so the WS layer can forward a LevelUp event to the frontend cinematic.

Uses a dedicated ``FakeMonsterWithStats`` so the legacy ``FakeMonster`` in
``test_progress.py`` (which only models HP/XP/level) keeps its tests untouched
— the gacha attributes are optional on the writeback path (guarded by
``hasattr``) so both fakes coexist forever.

Collection is import-safe: ``app.party.progress`` is imported via
``pytest.importorskip`` so the file SKIPs cleanly on a bare host without the
full stack installed.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

progress = pytest.importorskip("app.party.progress")
balance = pytest.importorskip("app.party.balance")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeMonsterWithStats:
    """Stand-in Monster carrying the gacha-wave stat columns.

    Mirrors the real ``app.db.models.Monster`` fields touched by award_xp's
    level-up loop. Defaults match the seed catalog mean so a fresh monster is
    a sensible test subject.
    """

    def __init__(
        self,
        *,
        level: int = 1,
        xp: int = 0,
        max_hp: int = 100,
        atk: int = 10,
        def_: int = 10,
        mp: int = 30,  # deliberately less than max_mp to assert refill
        max_mp: int = 50,
        evolution_stage: int = 0,
        skills: Optional[list] = None,
        id: str = "mon-stats",
    ) -> None:
        self.level = level
        self.xp = xp
        self.max_hp = max_hp
        self.atk = atk
        self.def_ = def_
        self.mp = mp
        self.max_mp = max_mp
        self.evolution_stage = evolution_stage
        self.skills = [] if skills is None else skills
        self.id = id


class FakeSession:
    """Mirror of test_progress.FakeSession — captures .add(), never commits."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


# --------------------------------------------------------------------------- #
# Single level-up applies every stat gain AND a full MP refill
# --------------------------------------------------------------------------- #
def test_single_level_up_applies_per_level_stat_gains() -> None:
    # Arrange — exactly 100 XP crosses one level boundary (L1 -> L2).
    monster = FakeMonsterWithStats(level=1, xp=0, atk=10, def_=10, mp=30, max_mp=50)
    # Act
    result = progress.award_xp(FakeSession(), monster, 100)
    # Assert: levelled, stats bumped by the per-level constants.
    assert result["levelled"] is True
    assert result["new_level"] == 2
    assert monster.atk == 10 + balance.ATK_PER_LEVEL
    assert monster.def_ == 10 + balance.DEF_PER_LEVEL
    assert monster.max_mp == 50 + balance.MP_PER_LEVEL
    # Full MP refill on level-up — the player just earned a moment of power.
    assert monster.mp == monster.max_mp
    # max_hp still bumps via the legacy HP_PER_LEVEL invariant.
    assert monster.max_hp == 100 + balance.HP_PER_LEVEL


def test_stat_gains_dict_carries_per_level_totals_for_single_level_up() -> None:
    # Arrange
    monster = FakeMonsterWithStats(level=1, xp=0)
    # Act
    result = progress.award_xp(FakeSession(), monster, 100)
    # Assert — the return-dict surface used by the WS LevelUp event.
    gains = result["stat_gains"]
    assert gains["atk"] == balance.ATK_PER_LEVEL
    assert gains["def"] == balance.DEF_PER_LEVEL
    assert gains["mp"] == balance.MP_PER_LEVEL
    assert gains["hp"] == balance.HP_PER_LEVEL


# --------------------------------------------------------------------------- #
# Multi-level dumps: stat_gains is the SUM across every level gained
# --------------------------------------------------------------------------- #
def test_multi_level_dump_sums_stat_gains_across_all_level_ups() -> None:
    # Arrange — 300 XP from L1 banks TWO level-ups (L1->L2 costs 100, L2->L3
    # costs 200). Every stat gain must accrue twice in the returned dict and
    # twice on the monster itself.
    monster = FakeMonsterWithStats(level=1, xp=0, atk=10, def_=10, mp=20, max_mp=50)
    # Act
    result = progress.award_xp(FakeSession(), monster, 300)
    # Assert level + bumps applied cumulatively
    assert monster.level == 3
    assert monster.atk == 10 + 2 * balance.ATK_PER_LEVEL
    assert monster.def_ == 10 + 2 * balance.DEF_PER_LEVEL
    assert monster.max_mp == 50 + 2 * balance.MP_PER_LEVEL
    assert monster.max_hp == 100 + 2 * balance.HP_PER_LEVEL
    # MP always tops to max_mp at the end of the level-up loop.
    assert monster.mp == monster.max_mp
    # stat_gains dict is the SUM across both level-ups.
    gains = result["stat_gains"]
    assert gains["atk"] == 2 * balance.ATK_PER_LEVEL
    assert gains["def"] == 2 * balance.DEF_PER_LEVEL
    assert gains["mp"] == 2 * balance.MP_PER_LEVEL
    assert gains["hp"] == 2 * balance.HP_PER_LEVEL


# --------------------------------------------------------------------------- #
# Sub-threshold XP gains stay no-ops on stats too
# --------------------------------------------------------------------------- #
def test_below_threshold_xp_does_not_touch_stats() -> None:
    # Arrange — 50 XP at L1 does not cross the 100-XP threshold.
    monster = FakeMonsterWithStats(level=1, xp=0, atk=10, def_=10, mp=30, max_mp=50)
    # Act
    result = progress.award_xp(FakeSession(), monster, 50)
    # Assert
    assert result["levelled"] is False
    assert monster.atk == 10
    assert monster.def_ == 10
    assert monster.max_mp == 50
    assert monster.mp == 30  # no refill when nothing levelled
    # stat_gains is still present (all zero) for a uniform consumer contract.
    assert result["stat_gains"] == {"atk": 0, "def": 0, "mp": 0, "hp": 0}


# --------------------------------------------------------------------------- #
# Legacy FakeMonster (no atk/def_/max_mp/mp attrs) keeps working unchanged
# --------------------------------------------------------------------------- #
class _LegacyFakeMonster:
    """A monster missing the gacha-wave fields — must not crash award_xp."""

    def __init__(self) -> None:
        self.level = 1
        self.xp = 0
        self.max_hp = 100
        self.evolution_stage = 0
        self.skills: list[Any] = []
        self.id = "legacy"


def test_legacy_fake_monster_without_gacha_stats_still_levels_safely() -> None:
    # Arrange
    monster = _LegacyFakeMonster()
    # Act — same 100 XP threshold; should NOT raise AttributeError.
    result = progress.award_xp(FakeSession(), monster, 100)
    # Assert
    assert result["levelled"] is True
    assert monster.max_hp == 100 + balance.HP_PER_LEVEL
    # The stat_gains dict still surfaces — HP bumped, the others stay zero
    # because the monster doesn't declare those attributes.
    gains = result["stat_gains"]
    assert gains["hp"] == balance.HP_PER_LEVEL
    assert gains["atk"] == 0
    assert gains["def"] == 0
    assert gains["mp"] == 0
