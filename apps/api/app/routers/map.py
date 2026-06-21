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

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.world.algorithms.base import BLOCKED_TILES

router = APIRouter(prefix="/api", tags=["map"])

# ---------------------------------------------------------------------------
# Map constants
# ---------------------------------------------------------------------------

MAP_WIDTH = 20
MAP_HEIGHT = 15
MAP_CHUNK_SIZE = 96
WILD_COUNT = 32  # wild enemies placed globally per run


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
    """Tile grid for the run.  0=walkable, 1=blocked, 2=campsite.

    Prefers the hand-curated canonical world artifact when present
    (``apps/api/data/world/canonical.json``); falls back to the seeded procgen
    so any run without a canonical bake (tests, fresh checkouts) still works.
    Canonical tiles are SEED-INDEPENDENT — the canonical bake is the shape we
    ship; the seed only matters for wild-enemy placement on top of it.
    """
    # Lazy import: avoids tugging on data files at module load (tests + the
    # bake script itself import map.py before any artifact exists).
    from app.world.canonical import get_canonical_world

    canonical = get_canonical_world()
    if canonical is not None:
        from app.world.canonical import get_canonical_tile_window

        if canonical.tiles:
            return [row[:] for row in canonical.tiles]
        window = get_canonical_tile_window(0, 0, MAP_WIDTH, MAP_HEIGHT)
        if window is not None:
            return window

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


def _tile_dims(tiles: list[list[int]]) -> tuple[int, int]:
    """Return (width, height) for a tile grid, falling back to legacy constants."""
    if tiles and tiles[0]:
        return len(tiles[0]), len(tiles)
    return MAP_WIDTH, MAP_HEIGHT


def _is_blocked_tile(tiles: list[list[int]], x: int, y: int) -> bool:
    """True when a tile is outside the grid or is not player-walkable."""
    if y < 0 or y >= len(tiles):
        return True
    if x < 0 or x >= len(tiles[y]):
        return True
    return tiles[y][x] in BLOCKED_TILES


def _world_dims_for(seed: int) -> tuple[int, int]:
    """Return full world dimensions for canonical or fallback worlds."""
    from app.world.canonical import get_canonical_world

    canonical = get_canonical_world()
    if canonical is not None:
        return canonical.spec.width, canonical.spec.height
    return _tile_dims(_generate_tiles(seed))


def _canonical_tile_window(
    center_x: int,
    center_y: int,
    size: int,
) -> tuple[list[list[int]], int, int, int, int] | None:
    """Return a centered canonical tile window plus origin/full-world dims."""
    from app.world.canonical import get_canonical_tile_window, get_canonical_world

    canonical = get_canonical_world()
    if canonical is None:
        return None

    world_width = canonical.spec.width
    world_height = canonical.spec.height
    window_w = max(1, min(size, world_width))
    window_h = max(1, min(size, world_height))
    origin_x = max(0, min(world_width - window_w, center_x - window_w // 2))
    origin_y = max(0, min(world_height - window_h, center_y - window_h // 2))
    tiles = get_canonical_tile_window(origin_x, origin_y, window_w, window_h)
    if tiles is None:
        return None
    return tiles, origin_x, origin_y, world_width, world_height


def _is_world_blocked(seed: int, x: int, y: int) -> bool:
    """True when a global world coordinate is not player-walkable."""
    from app.world.canonical import get_canonical_tile, get_canonical_world

    canonical = get_canonical_world()
    if canonical is not None:
        tile = get_canonical_tile(x, y)
        return tile is None or tile in BLOCKED_TILES

    tiles = _generate_tiles(seed)
    return _is_blocked_tile(tiles, x, y)


def _place_wild_on_map(
    wild: list[Monster],
    seed: int,
    *,
    origin_x: int = 0,
    origin_y: int = 0,
    width: int | None = None,
    height: int | None = None,
) -> list[TileEnemy]:
    """Assign tile positions to wild enemies deterministically."""
    rng = random.Random(seed ^ 0xABCD)
    world_width, world_height = _world_dims_for(seed)
    positions: list[tuple[int, int]] = []
    enemies: list[TileEnemy] = []
    for m in wild:
        attempts = 0
        while attempts < 200:
            x = rng.randint(3, max(3, world_width - 2))
            y = rng.randint(1, max(1, world_height - 2))
            if not _is_world_blocked(seed, x, y) and (x, y) not in positions:
                positions.append((x, y))
                if width is None or height is None or (
                    origin_x <= x < origin_x + width
                    and origin_y <= y < origin_y + height
                ):
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
    player_name = (body.player_name or "").strip() or "Player"
    # New runs spawn at the canonical trailhead when a baked world is present;
    # otherwise keep the legacy fallback spawn.
    from app.world.canonical import get_canonical_world

    canonical = get_canonical_world()
    start_x = (
        canonical.spec.start.x
        if canonical is not None and canonical.spec.start
        else 1
    )
    start_y = (
        canonical.spec.start.y
        if canonical is not None and canonical.spec.start
        else 1
    )
    run = Run(
        debate_topic=debate_topic,
        theme=body.theme,
        player_name=player_name,
        seed=body.seed,
        player_x=start_x,
        player_y=start_y,
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
        player_name=run.player_name,
        player_x=run.player_x,
        player_y=run.player_y,
        status=run.status.value,
        party=[_monster_to_summary(m) for m in party],
    )


@router.get("/runs/{run_id}/map", response_model=MapState)
async def get_map(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    center_x: Annotated[int | None, Query(ge=0)] = None,
    center_y: Annotated[int | None, Query(ge=0)] = None,
    chunk_size: Annotated[int, Query(ge=32, le=160)] = MAP_CHUNK_SIZE,
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

    from app.world.canonical import get_canonical_world

    canonical = get_canonical_world()
    world_width, world_height = _world_dims_for(run.seed)
    center_x = run.player_x if center_x is None else center_x
    center_y = run.player_y if center_y is None else center_y
    center_x = max(0, min(world_width - 1, center_x))
    center_y = max(0, min(world_height - 1, center_y))

    canonical_window = _canonical_tile_window(center_x, center_y, chunk_size)
    if canonical_window is not None:
        base_tiles, origin_x, origin_y, world_width, world_height = canonical_window
    else:
        base_tiles = _generate_tiles(run.seed)
        origin_x = 0
        origin_y = 0
        world_width, world_height = _tile_dims(base_tiles)
    width, height = _tile_dims(base_tiles)
    enemies = _place_wild_on_map(
        wilds,
        run.seed,
        origin_x=origin_x,
        origin_y=origin_y,
        width=width,
        height=height,
    )

    # Structured POIs (start/goal/camp/town/den/landmark) + campsite tiles (2).
    # Same helper the /world router uses, so the two endpoints always agree.
    pois = (
        canonical.spec.pois
        if canonical is not None
        else place_pois(run.seed, base_tiles, width, height)
    )
    local_pois = [
        p.model_copy(update={"x": p.x - origin_x, "y": p.y - origin_y})
        for p in pois
        if origin_x <= p.x < origin_x + width and origin_y <= p.y < origin_y + height
    ]
    tiles = apply_camp_tiles(base_tiles, local_pois)
    player_x = max(0, min(world_width - 1, run.player_x))
    player_y = max(0, min(world_height - 1, run.player_y))
    if _is_world_blocked(run.seed, player_x, player_y):
        start = canonical.spec.start if canonical is not None else None
        player_x = start.x if start is not None else 1
        player_y = start.y if start is not None else 1

    return MapState(
        width=width,
        height=height,
        tiles=tiles,
        player_x=player_x,
        player_y=player_y,
        enemies=enemies,
        origin_x=origin_x,
        origin_y=origin_y,
        world_width=world_width,
        world_height=world_height,
        chunk_size=chunk_size,
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

    width, height = _world_dims_for(run.seed)

    new_x = run.player_x + body.dx
    new_y = run.player_y + body.dy

    # Clamp to map bounds
    new_x = max(0, min(width - 1, new_x))
    new_y = max(0, min(height - 1, new_y))

    # Check tile walkability
    if _is_world_blocked(run.seed, new_x, new_y):
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

    width, height = _world_dims_for(run.seed)

    # (a) Clamp to map bounds.
    new_x = max(0, min(width - 1, body.x))
    new_y = max(0, min(height - 1, body.y))

    # (b) Validate walkability — if the client reports a blocked tile (desync /
    # tampering), keep the last good persisted position instead of corrupting it.
    # Mirrors the collision check in move_player.
    if _is_world_blocked(run.seed, new_x, new_y):
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
