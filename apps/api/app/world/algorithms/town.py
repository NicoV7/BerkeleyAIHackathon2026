"""town.py — STUB: structured town interior generator (Track B, Wave 2).

⚠️ SKELETON for a teammate. Implements the interface and emits a SMALL, VALID,
deterministic town interior (an open plaza, a few building footprints, a path
grid, and NPC/door anchors) so the /interior endpoint works end-to-end. The
layout is a simple grid of building blocks — not yet a believable town. TODOs
below mark the real work.

Structured town (Pokémon-town feel):
  1. Open floor everywhere (towns are walkable plazas, not mazes).
  2. Stamp a grid of rectangular BUILDING footprints (WALL) with gaps for paths.
  3. Put a FEATURE (NPC/shop anchor) at each building's front, and a DOOR exit
     at the bottom-center (return to overworld).

KNOBS:
    BUILD_COLS/ROWS — building grid dimensions.
    BUILD_W/H       — footprint size of each building.
    GUTTER          — path width between buildings.

TODO(teammate):
    - Real building interiors (enter a shop) + named NPC anchors with dialogue ids.
    - Organic street layout (not a rigid grid); plaza/fountain centerpiece.
    - Tie FEATURE anchors to actual services (shop/heal/quest-giver).
    - Theme by region biome (snowy town, desert town, ...).
"""
from __future__ import annotations

import random

from app.schemas import POI
from app.world.algorithms.base import (
    DOOR,
    FEATURE,
    FLOOR,
    WALL,
    GenResult,
    WorldGenerator,
)

# --- KNOBS ---
BUILD_COLS = 2
BUILD_ROWS = 2
BUILD_W = 4
BUILD_H = 3
GUTTER = 2


class TownGenerator(WorldGenerator):
    """Structured town interior. STUB — see module docstring TODOs."""

    name = "town"
    mask = 0x7041  # "TOWN" stream

    def generate(self, seed: int, width: int, height: int) -> GenResult:
        rng = random.Random(seed ^ self.mask)

        # 1) Open plaza everywhere, solid border.
        grid = [[FLOOR] * width for _ in range(height)]
        for x in range(width):
            grid[0][x] = WALL
            grid[height - 1][x] = WALL
        for y in range(height):
            grid[y][0] = WALL
            grid[y][width - 1] = WALL

        pois: list[POI] = []

        # 2) Grid of building footprints with gutters; FEATURE anchor at each
        #    building's front-center (an NPC/shop spot).
        origin_x, origin_y = 2, 2
        building_no = 0
        for r in range(BUILD_ROWS):
            for c in range(BUILD_COLS):
                bx = origin_x + c * (BUILD_W + GUTTER)
                by = origin_y + r * (BUILD_H + GUTTER)
                if bx + BUILD_W >= width - 1 or by + BUILD_H >= height - 1:
                    continue
                for yy in range(by, by + BUILD_H):
                    for xx in range(bx, bx + BUILD_W):
                        grid[yy][xx] = WALL
                # Front-door FEATURE anchor (just below the building, walkable).
                fy = by + BUILD_H
                fx = bx + BUILD_W // 2
                if 0 < fy < height - 1 and 0 < fx < width - 1:
                    grid[fy][fx] = FEATURE
                    building_no += 1
                    # rng gives a deterministic-but-varied building label.
                    label = rng.choice(["Shop", "Inn", "Guild", "Home"])
                    pois.append(
                        POI(kind="town", x=fx, y=fy, name=f"{label} {building_no}")
                    )

        # 3) Exit DOOR at bottom-center (return to overworld).
        ex, ey = width // 2, height - 2
        grid[ey][ex] = DOOR
        pois.insert(0, POI(kind="start", x=ex, y=ey, name="Town Gate"))

        return GenResult(width=width, height=height, tiles=grid, regions=[], pois=pois)
