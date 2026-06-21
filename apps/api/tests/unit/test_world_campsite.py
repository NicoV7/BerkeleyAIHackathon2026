"""Unit tests — Wave 2 world structure + campsite (Agent 5).

Covers, with NO live DB (pure helpers + a fake session):
  * WorldSpecLite is seed-deterministic (same seed -> equal; different -> differs)
  * map POIs match the world's POIs (shared placement helper)
  * campsite tiles (value 2) sit only on walkable positions
  * /rest heals the party to full

DB-backed assertions (if any) gate behind the `require_db` fixture from
tests/conftest.py; the core logic here runs purely.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.routers.map import (
    MAP_HEIGHT,
    MAP_WIDTH,
    _generate_tiles,
    rest,
)
from app.routers.world import (
    CAMP_TILE,
    apply_camp_tiles,
    build_world,
    place_pois,
)
from app.schemas import RestResult, WorldSpecLite


# --------------------------------------------------------------------------- #
# Seed-determinism
# --------------------------------------------------------------------------- #


def _world(seed: int) -> WorldSpecLite:
    tiles = _generate_tiles(seed)
    return build_world(seed, tiles, MAP_WIDTH, MAP_HEIGHT)


def test_world_is_seed_deterministic_same_seed_equal() -> None:
    w1 = _world(42)
    w2 = _world(42)
    # Pydantic models compare by value; identical seed -> identical world.
    assert w1 == w2
    assert w1.model_dump() == w2.model_dump()


def test_world_differs_for_different_seed() -> None:
    a = _world(1)
    b = _world(99999)
    assert a != b
    # POIs in particular should differ (placement is seed-driven).
    assert [(p.kind, p.x, p.y) for p in a.pois] != [(p.kind, p.x, p.y) for p in b.pois]


def test_world_has_start_goal_and_regions() -> None:
    w = _world(7)
    assert w.start is not None and w.start.kind == "start"
    assert w.goal is not None and w.goal.kind == "goal"
    assert len(w.regions) == 4
    kinds = {p.kind for p in w.pois}
    assert {"start", "goal", "camp"}.issubset(kinds)


# --------------------------------------------------------------------------- #
# map POIs == world POIs
# --------------------------------------------------------------------------- #


def test_map_pois_match_world_pois() -> None:
    seed = 1234
    tiles = _generate_tiles(seed)
    # The map router and the world router both call place_pois on the same grid.
    map_pois = place_pois(seed, tiles, MAP_WIDTH, MAP_HEIGHT)
    world = build_world(seed, tiles, MAP_WIDTH, MAP_HEIGHT)
    assert [p.model_dump() for p in map_pois] == [p.model_dump() for p in world.pois]


# --------------------------------------------------------------------------- #
# Campsite tiles on walkable positions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 1234, 99999])
def test_campsite_tiles_sit_on_walkable_positions(seed: int) -> None:
    base = _generate_tiles(seed)
    pois = place_pois(seed, base, MAP_WIDTH, MAP_HEIGHT)
    overlaid = apply_camp_tiles(base, pois)

    camp_pois = [p for p in pois if p.kind == "camp"]
    assert camp_pois, "expected at least one camp POI"

    for p in camp_pois:
        # Camp POI sits on a base tile that was walkable (never a wall).
        assert base[p.y][p.x] != 1, "camp placed on a blocked tile"
        # After overlay, the tile is marked as a campsite (value 2).
        assert overlaid[p.y][p.x] == CAMP_TILE

    # Every CAMP_TILE in the overlaid grid corresponds to a camp POI on a
    # previously-walkable tile (no stray camp tiles, none on walls).
    camp_coords = {(p.x, p.y) for p in camp_pois}
    for y in range(MAP_HEIGHT):
        for x in range(MAP_WIDTH):
            if overlaid[y][x] == CAMP_TILE:
                assert (x, y) in camp_coords
                assert base[y][x] != 1


def test_apply_camp_tiles_does_not_mutate_input() -> None:
    base = _generate_tiles(3)
    snapshot = [row[:] for row in base]
    pois = place_pois(3, base, MAP_WIDTH, MAP_HEIGHT)
    apply_camp_tiles(base, pois)
    assert base == snapshot  # input grid untouched


# --------------------------------------------------------------------------- #
# /rest heals the party to full (fake session, no DB)
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, monsters: list[Any]) -> None:
        self._monsters = monsters

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return list(self._monsters)

    def first(self) -> Any:
        return None


class _FakeSession:
    """Minimal async session double for the /rest endpoint.

    - get(Run, id) -> the seeded run
    - execute(select ...) -> the party
    - execute(text ...) for counters -> raises (columns absent) to exercise the
      best-effort fallback path
    - commit() -> no-op
    """

    def __init__(self, run: Any, party: list[Any]) -> None:
        self._run = run
        self._party = party
        self.committed = False

    async def get(self, model: Any, ident: str) -> Any:
        return self._run if (self._run and ident == self._run.id) else None

    async def execute(self, statement: Any, params: Any = None) -> Any:
        # Raw text() counter reads/writes -> simulate "columns don't exist".
        if params is not None:
            raise RuntimeError("column does not exist")
        return _FakeResult(self._party)

    async def commit(self) -> None:
        self.committed = True


def test_rest_heals_party_to_full(make_run, make_monster) -> None:
    run = make_run(seed=5)
    party = [
        make_monster(run_id=run.id, max_hp=120, name="A"),
        make_monster(run_id=run.id, max_hp=80, name="B"),
    ]
    session = _FakeSession(run, party)

    result: RestResult = asyncio.run(rest(run.id, session))  # type: ignore[arg-type]

    assert isinstance(result, RestResult)
    assert result.run_id == run.id
    assert len(result.healed) == 2
    # "Healed to full" -> each summary reports max_hp as its HP figure.
    healed_by_name = {h.name: h for h in result.healed}
    assert healed_by_name["A"].max_hp == 120
    assert healed_by_name["B"].max_hp == 80
    # Day advanced from the fallback 0 -> 1, encounters reset.
    assert result.day == 1
    assert result.encounters_since_rest == 0
    assert session.committed is True


def test_rest_404_when_run_missing(make_run, make_monster) -> None:
    from fastapi import HTTPException

    run = make_run(seed=5)
    session = _FakeSession(run, [])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(rest("nonexistent-id", session))  # type: ignore[arg-type]
    assert exc.value.status_code == 404
