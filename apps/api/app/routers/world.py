"""Wave 2 — World structure router (POIs + procedural WorldSpecLite).

Endpoints (mounted under the shared /api prefix, matching map.py):
    GET /api/runs/{run_id}/world  (-> WorldSpecLite)

This module owns the SINGLE source of truth for procedural POI placement
(``place_pois``) and region layout (``build_regions``). The map router imports
``place_pois`` so that ``GET /api/runs/{id}/map`` and ``GET /api/runs/{id}/world``
return *identical* POIs for the same run seed — there is no second placement
algorithm to drift out of sync.

Determinism contract:
    Everything derives from ``random.Random(seed ^ MASK)`` with fixed masks, so a
    given seed always yields byte-for-byte identical POIs / regions / world. No
    wall-clock, no unseeded RNG.

Tile legend (the map grid):
    0 = walkable, 1 = blocked, 2 = campsite (a ``camp`` POI tile; FE renders it).
"""
from __future__ import annotations

import random
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Run
from app.db.session import get_session
from app.schemas import POI, Region, WorldSpecLite

router = APIRouter(prefix="/api", tags=["world"])

# ---------------------------------------------------------------------------
# Procedural placement — the ONE shared source of truth for map + world.
# ---------------------------------------------------------------------------

# RNG masks: keep POI placement independent of tile/wild-enemy RNG streams so a
# change to one does not perturb the other, while staying fully seed-derived.
_POI_MASK = 0x5EED  # POI placement stream
_REGION_MASK = 0xBEEF  # region/biome stream

# How many of each "extra" POI kind to scatter (besides start/goal).
_CAMP_COUNT = 2
_TOWN_COUNT = 1
_DEN_COUNT = 1
_LANDMARK_COUNT = 2

# Tile value for a campsite (camp POI). 0=walkable, 1=blocked, 2=campsite.
CAMP_TILE = 2

_BIOMES = ("plains", "forest", "mountains", "wetland")

_KIND_NAMES = {
    "start": "Trailhead",
    "goal": "The Summit",
    "camp": "Campsite",
    "town": "Town",
    "den": "Monster Den",
    "landmark": "Landmark",
}


def place_pois(
    seed: int,
    tiles: list[list[int]],
    width: int,
    height: int,
) -> list[POI]:
    """Deterministically place POIs on walkable tiles for ``seed``.

    Pure + deterministic: identical (seed, tiles, width, height) -> identical
    POI list (same order, coords, names). Both the map router and the world
    router call this so their POIs never diverge.

    Placement rules:
      * ``start`` is pinned to the spawn zone (1, 1) — matches create_run's
        ``player_x/y`` so the player begins on the start POI.
      * ``goal`` is pinned to the far walkable corner so the exit is reachable
        and visually "across the map".
      * camp/town/den/landmark are scattered on distinct walkable, non-pinned
        tiles via the seeded RNG.
    """
    rng = random.Random(seed ^ _POI_MASK)

    def _walkable(x: int, y: int) -> bool:
        if not (0 <= x < width and 0 <= y < height):
            return False
        # Treat campsite (2) as walkable too, though we place camps last.
        return tiles[y][x] != 1

    pois: list[POI] = []
    used: set[tuple[int, int]] = set()

    # start: pinned to spawn (matches create_run player_x/y = 1,1).
    start_xy = (1, 1)
    if not _walkable(*start_xy):
        start_xy = _nearest_walkable(start_xy, width, height, tiles)
    pois.append(POI(kind="start", x=start_xy[0], y=start_xy[1], name=_KIND_NAMES["start"]))
    used.add(start_xy)

    # goal: pinned to far corner (walkable nearest to bottom-right interior).
    goal_xy = _nearest_walkable((width - 2, height - 2), width, height, tiles)
    if goal_xy in used:
        goal_xy = _scatter_one(rng, width, height, tiles, used)
    pois.append(POI(kind="goal", x=goal_xy[0], y=goal_xy[1], name=_KIND_NAMES["goal"]))
    used.add(goal_xy)

    # Scatter the rest deterministically, in a fixed kind order.
    plan: list[str] = (
        ["camp"] * _CAMP_COUNT
        + ["town"] * _TOWN_COUNT
        + ["den"] * _DEN_COUNT
        + ["landmark"] * _LANDMARK_COUNT
    )
    counters: dict[str, int] = {}
    for kind in plan:
        xy = _scatter_one(rng, width, height, tiles, used)
        if xy is None:
            break
        used.add(xy)
        counters[kind] = counters.get(kind, 0) + 1
        suffix = f" {counters[kind]}" if plan.count(kind) > 1 else ""
        pois.append(POI(kind=kind, x=xy[0], y=xy[1], name=f"{_KIND_NAMES[kind]}{suffix}"))

    return pois


def _scatter_one(
    rng: random.Random,
    width: int,
    height: int,
    tiles: list[list[int]],
    used: set[tuple[int, int]],
) -> tuple[int, int] | None:
    """Pick a fresh walkable interior tile not already used; None if exhausted."""
    for _ in range(500):
        x = rng.randint(1, width - 2)
        y = rng.randint(1, height - 2)
        if (x, y) in used:
            continue
        if tiles[y][x] != 1:
            return (x, y)
    # Deterministic fallback: first free walkable tile by scan order.
    for y in range(height):
        for x in range(width):
            if (x, y) not in used and tiles[y][x] != 1:
                return (x, y)
    return None


def _nearest_walkable(
    target: tuple[int, int],
    width: int,
    height: int,
    tiles: list[list[int]],
) -> tuple[int, int]:
    """Spiral outward from ``target`` to the closest walkable tile (deterministic)."""
    tx, ty = target
    max_r = max(width, height)
    for r in range(max_r):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                x, y = tx + dx, ty + dy
                if 0 <= x < width and 0 <= y < height and tiles[y][x] != 1:
                    return (x, y)
    return (1, 1)


def apply_camp_tiles(tiles: list[list[int]], pois: list[POI]) -> list[list[int]]:
    """Overlay CAMP_TILE (2) onto the grid at every ``camp`` POI coordinate.

    Returns a NEW grid (does not mutate the input) so callers that also need the
    raw walkable/blocked grid are unaffected. Camp tiles only ever replace
    walkable (0) tiles — placement guarantees camps sit on walkable positions.
    """
    out = [row[:] for row in tiles]
    for poi in pois:
        if poi.kind == "camp" and 0 <= poi.y < len(out) and 0 <= poi.x < len(out[0]):
            if out[poi.y][poi.x] != 1:
                out[poi.y][poi.x] = CAMP_TILE
    return out


def build_regions(seed: int, width: int, height: int) -> list[Region]:
    """Deterministically split the map into 4 quadrant regions with biomes."""
    rng = random.Random(seed ^ _REGION_MASK)
    half_w = width // 2
    half_h = height // 2
    quadrants = [
        ("Northwest", [0, 0, half_w - 1, half_h - 1]),
        ("Northeast", [half_w, 0, width - 1, half_h - 1]),
        ("Southwest", [0, half_h, half_w - 1, height - 1]),
        ("Southeast", [half_w, half_h, width - 1, height - 1]),
    ]
    biomes = list(_BIOMES)
    rng.shuffle(biomes)
    regions: list[Region] = []
    for i, (name, bounds) in enumerate(quadrants):
        regions.append(Region(name=name, biome=biomes[i % len(biomes)], bounds=bounds))
    return regions


def build_world(
    seed: int,
    tiles: list[list[int]],
    width: int,
    height: int,
) -> WorldSpecLite:
    """Assemble the full WorldSpecLite for a seed — pure + deterministic."""
    pois = place_pois(seed, tiles, width, height)
    regions = build_regions(seed, width, height)
    start = next((p for p in pois if p.kind == "start"), None)
    goal = next((p for p in pois if p.kind == "goal"), None)
    return WorldSpecLite(
        seed=seed,
        width=width,
        height=height,
        regions=regions,
        pois=pois,
        start=start,
        goal=goal,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/world", response_model=WorldSpecLite)
async def get_world(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorldSpecLite:
    """Return the seed-deterministic WorldSpecLite for a run.

    POIs are produced by the same ``place_pois`` helper the map router uses, so
    ``/world`` and ``/map`` always agree for a given seed.
    """
    # Import here to avoid a circular import at module load (map imports world).
    from app.routers.map import MAP_HEIGHT, MAP_WIDTH, _generate_tiles

    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    tiles = _generate_tiles(run.seed)

    # Wave 3 (gated, default OFF): try the agent generator first. It is cached
    # per seed and returns None on ANY failure, so this can NEVER 500 or break
    # determinism — on None (or an unexpected raise) we use the procedural world.
    if settings.world_gen_enabled:
        try:
            from app.world.generator import generate_world

            generated = await generate_world(run.seed, MAP_WIDTH, MAP_HEIGHT)
            if generated is not None:
                return generated
        except Exception:  # noqa: BLE001 — generator must never break the route
            pass

    # Default / fallback path: the Wave-2 seed-deterministic procedural world.
    return build_world(run.seed, tiles, MAP_WIDTH, MAP_HEIGHT)
