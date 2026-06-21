"""caves.py — STUB: cellular-automata cave generator (Track B, Wave 2).

⚠️ SKELETON for a teammate. The interface is implemented and produces a SMALL,
VALID, deterministic interior (so the /interior endpoint works end-to-end today),
but the algorithm is a single CA smoothing pass with no advanced features. The
TODOs below mark where the teammate fleshes it out.

Cellular-automata caves (the classic roguelike den/cave look):
  1. Randomly fill the grid with walls at density ``FILL_PROB``.
  2. Run N smoothing steps: a cell becomes a wall iff >= ``BIRTH_LIMIT`` of its
     8 neighbours are walls (this carves connected organic caverns).
  3. Force a solid border, drop a DOOR (exit) + a couple FEATURE tiles.

KNOBS (tune the cave feel):
    FILL_PROB    — initial wall density (higher = tighter caves).
    SMOOTH_STEPS — CA iterations (more = smoother walls).
    BIRTH_LIMIT  — neighbour-wall count that turns a cell into wall.

TODO(teammate):
    - Flood-fill to guarantee the DOOR connects to the open area; discard/retry
      isolated pockets (right now connectivity is best-effort).
    - Place loot/enemy ANCHOR pois (FEATURE tiles) in the largest cavern.
    - Multi-level caves; biome-tinted variants.
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
FILL_PROB = 0.42
SMOOTH_STEPS = 4
BIRTH_LIMIT = 5


class CaveGenerator(WorldGenerator):
    """Cellular-automata caves. STUB — see module docstring TODOs."""

    name = "cave"
    mask = 0xCA7E  # "CAVE" stream

    def generate(self, seed: int, width: int, height: int) -> GenResult:
        rng = random.Random(seed ^ self.mask)

        # 1) Random fill (interior only; border stays wall).
        grid = [[WALL] * width for _ in range(height)]
        for y in range(1, height - 1):
            for x in range(1, width - 1):
                grid[y][x] = WALL if rng.random() < FILL_PROB else FLOOR

        # 2) CA smoothing passes.
        for _ in range(SMOOTH_STEPS):
            grid = self._smooth(grid, width, height)

        # 3) Exit door on the bottom edge over the nearest open floor, + a couple
        #    feature anchors on open floor (deterministic scan order).
        pois: list[POI] = []
        door_xy = self._carve_door(grid, width, height)
        if door_xy is not None:
            pois.append(POI(kind="start", x=door_xy[0], y=door_xy[1], name="Entrance"))

        for fx, fy in self._open_floors(grid, width, height)[:2]:
            grid[fy][fx] = FEATURE
            pois.append(POI(kind="landmark", x=fx, y=fy, name="Cave Feature"))

        return GenResult(width=width, height=height, tiles=grid, regions=[], pois=pois)

    def _smooth(self, grid: list[list[int]], width: int, height: int) -> list[list[int]]:
        out = [row[:] for row in grid]
        for y in range(1, height - 1):
            for x in range(1, width - 1):
                walls = self._wall_neighbours(grid, x, y, width, height)
                out[y][x] = WALL if walls >= BIRTH_LIMIT else FLOOR
        return out

    @staticmethod
    def _wall_neighbours(
        grid: list[list[int]], x: int, y: int, width: int, height: int
    ) -> int:
        n = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if nx < 0 or ny < 0 or nx >= width or ny >= height:
                    n += 1  # out-of-bounds counts as wall
                elif grid[ny][nx] == WALL:
                    n += 1
        return n

    @staticmethod
    def _open_floors(
        grid: list[list[int]], width: int, height: int
    ) -> list[tuple[int, int]]:
        return [
            (x, y)
            for y in range(height)
            for x in range(width)
            if grid[y][x] == FLOOR
        ]

    def _carve_door(
        self, grid: list[list[int]], width: int, height: int
    ) -> tuple[int, int] | None:
        """Place a DOOR on the lowest open floor near center-bottom (the exit)."""
        cx = width // 2
        for y in range(height - 2, 0, -1):
            # Search outward from center on this row for the first open floor.
            for off in range(width):
                for x in (cx - off, cx + off):
                    if 0 < x < width - 1 and grid[y][x] == FLOOR:
                        grid[y][x] = DOOR
                        return (x, y)
        return None
