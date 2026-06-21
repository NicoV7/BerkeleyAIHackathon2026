"""WS-A — Map / Run / Move router.

Endpoints:
    POST /api/runs           (CreateRunRequest -> RunState)
    GET  /api/runs/{id}/map  (-> MapState)
    POST /api/runs/{id}/move (MoveRequest -> MoveResult)
    POST /api/runs/{id}/rest (-> RestResult)   # campsite rest hub

Tile legend: 0 = walkable, 1 = blocked, 2 = campsite (a ``camp`` POI tile).
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Monster, MonsterOwner, Run, RunStatus
from app.db.session import get_session
from app.party.generator import generate_wild, roll_starter_party
# Procedural POI placement is owned by the world router — import the SINGLE
# shared helper so /map and /world never disagree about POIs / camp tiles.
from app.routers.world import apply_camp_tiles, place_pois
from app.schemas import (
    CreateRunRequest,
    MapState,
    MonsterSummary,
    MoveRequest,
    MoveResult,
    RestResult,
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
    """Create a new run row, roll a starter party, return RunState.

    THEME topics: the player picks a ``theme`` at run start; each battle draws a
    random topic within it (resolved at encounter creation). ``debate_topic``
    stays populated (NOT NULL) — when a theme is given but no explicit topic, we
    label it with the theme so existing readers (runs.py, RunState) never break.
    """
    debate_topic = body.topic or body.theme or ""
    run = Run(
        debate_topic=debate_topic,
        theme=body.theme,
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
        theme=run.theme,
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

    base_tiles = _generate_tiles(run.seed)
    enemies = _place_wild_on_map(wilds, run.seed)

    # Structured POIs (start/goal/camp/town/den/landmark) + campsite tiles (2).
    # Same helper the /world router uses, so the two endpoints always agree.
    pois = place_pois(run.seed, base_tiles, MAP_WIDTH, MAP_HEIGHT)
    tiles = apply_camp_tiles(base_tiles, pois)

    return MapState(
        width=MAP_WIDTH,
        height=MAP_HEIGHT,
        tiles=tiles,
        player_x=run.player_x,
        player_y=run.player_y,
        enemies=enemies,
        pois=pois,
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


# ---------------------------------------------------------------------------
# Position sync (Track B — client-authoritative movement)
# ---------------------------------------------------------------------------
#
# With client-side WorldSim owning per-frame movement, the per-step POST /move
# round-trip is gone. The client instead pushes its ABSOLUTE tile position here,
# debounced (~1-2s) and on every scene transition, so a page refresh resumes
# where the player actually is. The server validates walkability/bounds and is
# the persistence authority only — it is no longer the per-step gatekeeper.
#
# These request/response models are defined locally (rather than in the frozen
# schemas.py) to keep this additive endpoint self-contained to the map router.


# Last accepted client sync sequence per run. The client attaches a
# monotonically increasing ``seq`` to each /sync; we drop any sync whose seq is
# <= the last accepted one so an out-of-order / stale request (refresh +
# reconnect race, retried debounce) can't roll a newer position back to an older
# one. Kept in-process (not on the frozen Run model) — sequence ordering is a
# transient integrity guard, not durable run state, and resets harmlessly on
# restart (the next sync simply re-establishes the high-water mark).
_LAST_SYNC_SEQ: dict[str, int] = {}


class SyncPositionRequest(BaseModel):
    """Absolute player tile position pushed by the client WorldSim."""

    x: int
    y: int
    # Monotonic client sequence number. Optional for backward-compat; when
    # omitted, ordering enforcement is skipped (treated as always-newest).
    seq: int | None = None


class SyncPositionResult(BaseModel):
    """Persisted position (clamped/validated server-side)."""

    player_x: int
    player_y: int
    # True when the sync was dropped as stale/out-of-order (seq <= last seen).
    stale: bool = False


@router.post("/runs/{run_id}/sync", response_model=SyncPositionResult)
async def sync_position(
    run_id: str,
    body: SyncPositionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SyncPositionResult:
    """Persist an ABSOLUTE player position from the client WorldSim.

    Called debounced (~1-2s) and on scene transitions — NOT per step. The server
    is the remaining integrity gate now that movement is client-authoritative:

      (a) CLAMP x,y into map bounds,
      (b) REJECT a blocked landing tile (tiles[y][x] == 1) — snap back to the
          stored position instead of corrupting it,
      (c) DROP stale / out-of-order syncs via a monotonic client ``seq`` so a
          refresh/reconnect race can't overwrite a newer position with an older
          one (lost-write rollback).

    World-layout determinism is unaffected: tiles stay seed-derived; only the
    persisted player coordinate changes.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # (c) Reject out-of-order / stale syncs. Strictly-increasing seq wins; an
    # equal or lower seq is a duplicate or a late-arriving older write — drop it
    # and return the currently-persisted position unchanged.
    if body.seq is not None:
        last = _LAST_SYNC_SEQ.get(run_id)
        if last is not None and body.seq <= last:
            return SyncPositionResult(
                player_x=run.player_x, player_y=run.player_y, stale=True
            )

    # (a) Clamp to map bounds.
    new_x = max(0, min(MAP_WIDTH - 1, body.x))
    new_y = max(0, min(MAP_HEIGHT - 1, body.y))

    # (b) Validate walkability — if the client reports a blocked tile (desync /
    # tampering), keep the last good persisted position instead of corrupting it.
    # Mirrors the collision check in move_player.
    tiles = _generate_tiles(run.seed)
    if tiles[new_y][new_x] == 1:
        # The seq is still valid/newest — advance the high-water mark so a later
        # in-order sync to a good tile isn't itself dropped as stale.
        if body.seq is not None:
            _LAST_SYNC_SEQ[run_id] = body.seq
        return SyncPositionResult(player_x=run.player_x, player_y=run.player_y)

    if body.seq is not None:
        _LAST_SYNC_SEQ[run_id] = body.seq

    run.player_x = new_x
    run.player_y = new_y
    session.add(run)
    await session.commit()

    return SyncPositionResult(player_x=new_x, player_y=new_y)


# ---------------------------------------------------------------------------
# Campsite — rest hub
# ---------------------------------------------------------------------------


async def _read_rest_counters(session: AsyncSession, run_id: str) -> tuple[int, int]:
    """Best-effort read of (day, encounters_since_rest) from optional columns.

    These columns are NOT in the frozen ``Run`` ORM model and may not exist (we
    are not allowed to add the idempotent ALTER to db/session.py). So we read via
    raw SQL guarded in try/except: if the columns are missing the DB raises and
    we fall back to (0, 0). Fully backward-compatible.
    """
    from sqlalchemy import text

    try:
        result = await session.execute(
            text("SELECT day, encounters_since_rest FROM runs WHERE id = :id"),
            {"id": run_id},
        )
        row = result.first()
    except Exception:  # noqa: BLE001 — columns absent / unsupported backend
        return (0, 0)
    if row is None:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


async def _write_rest_counters(session: AsyncSession, run_id: str, day: int) -> None:
    """Best-effort write: advance the day and zero encounters_since_rest.

    Silently no-ops if the optional columns don't exist (see _read_rest_counters).
    """
    from sqlalchemy import text

    try:
        await session.execute(
            text(
                "UPDATE runs SET day = :day, encounters_since_rest = 0 "
                "WHERE id = :id"
            ),
            {"day": day, "id": run_id},
        )
    except Exception:  # noqa: BLE001 — columns absent; counters stay best-effort
        pass


@router.post("/runs/{run_id}/rest", response_model=RestResult)
async def rest(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RestResult:
    """Campsite rest: fully heal the party, advance the day, reset encounters.

    Healing sets every player-owned monster's effective HP to ``max_hp``. Live HP
    is not a column on the frozen ``Monster`` model (battles track HP elsewhere),
    so "healed to full" is reflected by returning each party member at ``max_hp``
    via MonsterSummary. The day / encounters_since_rest counters are persisted
    best-effort (optional columns) and otherwise computed.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    result = await session.execute(
        select(Monster).where(
            Monster.run_id == run_id,
            Monster.owner == MonsterOwner.player,
        )
    )
    party = list(result.scalars().all())

    day, _encounters = await _read_rest_counters(session, run_id)
    new_day = day + 1
    await _write_rest_counters(session, run_id, new_day)
    await session.commit()

    healed = [_monster_to_summary(m) for m in party]
    return RestResult(
        run_id=run_id,
        healed=healed,
        day=new_day,
        encounters_since_rest=0,
        message=f"Rested at camp. {len(healed)} party member(s) healed to full.",
    )
