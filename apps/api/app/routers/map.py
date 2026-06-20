"""WS-A — Map / Run / Move router.

Endpoints:
    POST /api/runs          (CreateRunRequest -> RunState)
    GET  /api/runs/{id}/map (-> MapState)
    POST /api/runs/{id}/move (MoveRequest -> MoveResult)
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Monster, MonsterOwner, Run, RunStatus
from app.db.session import get_session
from app.party.generator import generate_wild, roll_starter_party
from app.schemas import (
    CreateRunRequest,
    MapState,
    MonsterSummary,
    MoveRequest,
    MoveResult,
    RunState,
    TileEnemy,
)

router = APIRouter(prefix="/api", tags=["map"])

# ---------------------------------------------------------------------------
# Map constants
# ---------------------------------------------------------------------------

MAP_WIDTH = 20
MAP_HEIGHT = 15
WILD_COUNT = 5  # wild enemies placed on the map per run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _monster_to_summary(m: Monster) -> MonsterSummary:
    return MonsterSummary(
        id=m.id,
        name=m.name,
        type=m.type.value,
        owner=m.owner.value,
        level=m.level,
        xp=m.xp,
        max_hp=m.max_hp,
        evolution_stage=m.evolution_stage,
        skills=m.skills or [],
    )


def _generate_tiles(seed: int) -> list[list[int]]:
    """Deterministic tile grid from seed.  0=walkable, 1=blocked."""
    rng = random.Random(seed)
    tiles = [[0] * MAP_WIDTH for _ in range(MAP_HEIGHT)]
    # Place some scattered wall tiles (~15% density)
    for y in range(MAP_HEIGHT):
        for x in range(MAP_WIDTH):
            # Keep the edges clear and start zone clear
            if x == 0 or y == 0 or x == MAP_WIDTH - 1 or y == MAP_HEIGHT - 1:
                continue
            if x <= 2 and y <= 2:
                continue  # spawn zone always walkable
            if rng.random() < 0.12:
                tiles[y][x] = 1
    return tiles


def _place_wild_on_map(wild: list[Monster], seed: int) -> list[TileEnemy]:
    """Assign tile positions to wild enemies deterministically."""
    rng = random.Random(seed ^ 0xABCD)
    tiles = _generate_tiles(seed)
    positions: list[tuple[int, int]] = []
    enemies: list[TileEnemy] = []
    for m in wild:
        attempts = 0
        while attempts < 200:
            x = rng.randint(3, MAP_WIDTH - 2)
            y = rng.randint(1, MAP_HEIGHT - 2)
            if tiles[y][x] == 0 and (x, y) not in positions:
                positions.append((x, y))
                enemies.append(TileEnemy(id=m.id, x=x, y=y, sprite="enemy"))
                break
            attempts += 1
    return enemies


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/runs", response_model=RunState)
async def create_run(
    body: CreateRunRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunState:
    """Create a new run row, roll a starter party, return RunState."""
    run = Run(
        debate_topic=body.topic,
        seed=body.seed,
        player_x=1,
        player_y=1,
        status=RunStatus.active,
        # Use naive UTC to match TIMESTAMP WITHOUT TIME ZONE column
        created_at=datetime.utcnow(),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    party = await roll_starter_party(session, run.id, seed=body.seed)
    # Also seed wild enemies (so they exist in DB for map queries)
    await generate_wild(session, run.id, n=WILD_COUNT, seed=body.seed)

    return RunState(
        id=run.id,
        debate_topic=run.debate_topic,
        player_x=run.player_x,
        player_y=run.player_y,
        status=run.status.value,
        party=[_monster_to_summary(m) for m in party],
    )


@router.get("/runs/{run_id}/map", response_model=MapState)
async def get_map(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MapState:
    """Return deterministic tile grid + current player pos + wild enemy positions."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Fetch wild enemies for this run
    result = await session.execute(
        select(Monster).where(
            Monster.run_id == run_id,
            Monster.owner == MonsterOwner.wild,
        )
    )
    wilds = list(result.scalars().all())

    tiles = _generate_tiles(run.seed)
    enemies = _place_wild_on_map(wilds, run.seed)

    return MapState(
        width=MAP_WIDTH,
        height=MAP_HEIGHT,
        tiles=tiles,
        player_x=run.player_x,
        player_y=run.player_y,
        enemies=enemies,
    )


@router.post("/runs/{run_id}/move", response_model=MoveResult)
async def move_player(
    run_id: str,
    body: MoveRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MoveResult:
    """Move player by (dx,dy); check walkability and wild-enemy collision."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    tiles = _generate_tiles(run.seed)

    new_x = run.player_x + body.dx
    new_y = run.player_y + body.dy

    # Clamp to map bounds
    new_x = max(0, min(MAP_WIDTH - 1, new_x))
    new_y = max(0, min(MAP_HEIGHT - 1, new_y))

    # Check tile walkability
    if tiles[new_y][new_x] == 1:
        # Blocked — stay in place
        return MoveResult(player_x=run.player_x, player_y=run.player_y, encounter_id=None)

    # Update position
    run.player_x = new_x
    run.player_y = new_y
    session.add(run)
    await session.commit()

    # Check collision with any wild enemy
    result = await session.execute(
        select(Monster).where(
            Monster.run_id == run_id,
            Monster.owner == MonsterOwner.wild,
        )
    )
    wilds = list(result.scalars().all())
    enemy_positions = _place_wild_on_map(wilds, run.seed)

    collided_wild_id: str | None = None
    for enemy in enemy_positions:
        if enemy.x == new_x and enemy.y == new_y:
            collided_wild_id = enemy.id
            break

    # WS-B owns actual encounter creation.
    # On collision, return the wild monster id in encounter_id field
    # (WS-B's POST /api/encounters uses wild_id to create the encounter).
    # For now we return the wild id so the frontend can call WS-B.
    encounter_id = collided_wild_id  # None if no collision

    return MoveResult(
        player_x=new_x,
        player_y=new_y,
        encounter_id=encounter_id,
    )
