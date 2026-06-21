from __future__ import annotations

from typing import Any

import pytest

from app.db.models import DebateType, Monster, MonsterOwner
from app.party.generator import apply_avatar_traits, roll_starter_party


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.refreshed: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)


@pytest.mark.asyncio
async def test_valid_avatar_type_forces_first_starter_to_selected_type_and_moves() -> None:
    session = _FakeSession()

    monsters = await roll_starter_party(
        session,
        "run-avatar-pathos",
        seed=123,
        avatar_type="PATHOS",
    )

    avatar = monsters[0]
    assert avatar.is_avatar is True
    assert avatar.type == DebateType.pathos
    assert avatar.name == "PathosDrake"

    skill_names = {skill["name"] for skill in avatar.skills}
    skill_types = {skill["type"] for skill in avatar.skills}
    assert len(avatar.skills) == 2
    assert len(skill_names) == 2
    assert skill_types == {"PATHOS"}

    assert all(monster.is_avatar is False for monster in monsters[1:])
    assert session.added == monsters
    assert session.commits == 1
    assert session.refreshed == monsters


@pytest.mark.asyncio
async def test_invalid_avatar_type_keeps_random_starter_behavior_unmarked() -> None:
    session = _FakeSession()

    monsters = await roll_starter_party(
        session,
        "run-invalid-avatar",
        seed=123,
        avatar_type="NOT_A_TYPE",
    )

    assert len(monsters) in {2, 3}
    assert all(monster.is_avatar is False for monster in monsters)


def test_apply_avatar_traits_forces_existing_first_pull_to_avatar_type() -> None:
    monster = Monster(
        run_id="run-empty-start",
        owner=MonsterOwner.player,
        name="Gacha Persona",
        type=DebateType.logos,
        persona={"voice": "A precise scientist.", "tagline": "Evidence first."},
        skills=[],
    )

    applied = apply_avatar_traits(monster, "PATHOS")

    assert applied is True
    assert monster.is_avatar is True
    assert monster.type == DebateType.pathos
    assert len(monster.skills) == 2
    assert len({skill["name"] for skill in monster.skills}) == 2
    assert {skill["type"] for skill in monster.skills} == {"PATHOS"}
    assert "type PATHOS" in monster.harness["system_prompt"]
