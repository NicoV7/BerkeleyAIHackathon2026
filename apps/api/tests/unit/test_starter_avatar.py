from __future__ import annotations

from typing import Any

import pytest

from app.db.models import DebateType
from app.party import archetypes
from app.party.generator import roll_starter_party


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

    expected_names = set(archetypes.signature_skills_for_type(DebateType.pathos))
    skill_names = {skill["name"] for skill in avatar.skills}
    skill_types = {skill["type"] for skill in avatar.skills}
    assert skill_names == expected_names
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
