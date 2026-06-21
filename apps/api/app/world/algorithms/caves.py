"""caves.py — cellular-automata cave generator (Track B, Wave 2).

Cellular-automata caves (the classic roguelike den/cave look):
  1. Randomly fill the grid with walls at density ``FILL_PROB``.
  2. Run N smoothing steps: a cell becomes a wall iff >= ``BIRTH_LIMIT`` of its
     8 neighbours are walls (this carves connected organic caverns).
  3. Flood-fill: keep only the LARGEST connected open region; everything else
     gets filled back to WALL (guarantees a single navigable cave).
  4. Drop a DOOR (exit) on that region + FEATURE anchors (loot/enemies/exit-up).

KNOBS (tune the cave feel):
    FILL_PROB    — initial wall density (higher = tighter caves).
    SMOOTH_STEPS — CA iterations (more = smoother walls).
    BIRTH_LIMIT  — neighbour-wall count that turns a cell into wall.
    FEATURE_COUNT — how many anchors to drop in the largest cavern.
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
FEATURE_COUNT = 3  # exit-up + 2 anchors in the largest cavern


class CaveGenerator(WorldGenerator):
    """Cellular-automata caves with flood-fill connectivity guarantee."""

    name = "cave"
    mask = 0xCA7E  # "CAVE" stream

    def generate(self, seed: int, width: int, height: int) -> GenResult:
        rng = random.Random(seed ^ self.mask)

        grid = [[WALL] * width for _ in range(height)]
        for y in range(1, height - 1):
            for x in range(1, width - 1):
                grid[y][x] = WALL if rng.random() < FILL_PROB else FLOOR

        for _ in range(SMOOTH_STEPS):
            grid = self._smooth(grid, width, height)

        # Flood-fill: keep ONLY the largest open region. Smaller pockets get
        # filled back to WALL so the cave is always one navigable space.
        main_region = self._largest_region(grid, width, height)
        if not main_region:
            # Degenerate cave — re-open a small chamber so the scene renders.
            cx, cy = width // 2, height // 2
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    if 0 < cx + dx < width - 1 and 0 < cy + dy < height - 1:
                        grid[cy + dy][cx + dx] = FLOOR
            main_region = self._largest_region(grid, width, height)

        # Fill everything not in the main region back to WALL.
        in_main = set(main_region)
        for y in range(height):
            for x in range(width):
                if grid[y][x] == FLOOR and (x, y) not in in_main:
                    grid[y][x] = WALL

        pois: list[POI] = []
        door_xy = self._place_door(grid, width, height, in_main)
        if door_xy is not None:
            pois.append(POI(kind="start", x=door_xy[0], y=door_xy[1], name="Entrance"))

        # FEATURE anchors deeper in the cave (sorted by distance from door).
        anchors = self._anchors_in_region(door_xy, main_region, FEATURE_COUNT - 1)
        feature_labels = ["Loot Cache", "Beast Den", "Whispering Glow"]
        for i, (fx, fy) in enumerate(anchors):
            if grid[fy][fx] != FLOOR:
                continue
            grid[fy][fx] = FEATURE
            pois.append(
                POI(
                    kind="landmark",
                    x=fx,
                    y=fy,
                    name=feature_labels[i % len(feature_labels)],
                )
            )

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
    def _largest_region(
        grid: list[list[int]], width: int, height: int
    ) -> list[tuple[int, int]]:
        """4-connected flood fill over FLOOR tiles; return largest region's cells.

        Deterministic: scan order is fixed (y outer, x inner) and BFS expands
        neighbours in a fixed (N,S,W,E) order, so the same grid always picks the
        same "largest" tie-break.
        """
        seen: set[tuple[int, int]] = set()
        best: list[tuple[int, int]] = []
        for sy in range(height):
            for sx in range(width):
                if grid[sy][sx] != FLOOR or (sx, sy) in seen:
                    continue
                region: list[tuple[int, int]] = []
                stack = [(sx, sy)]
                while stack:
                    x, y = stack.pop()
                    if (x, y) in seen:
                        continue
                    if not (0 <= x < width and 0 <= y < height):
                        continue
                    if grid[y][x] != FLOOR:
                        continue
                    seen.add((x, y))
                    region.append((x, y))
                    stack.extend([(x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y)])
                if len(region) > len(best):
                    best = region
        return best

    @staticmethod
    def _place_door(
        grid: list[list[int]],
        width: int,
        height: int,
        region: set[tuple[int, int]],
    ) -> tuple[int, int] | None:
        """Place DOOR on the region tile nearest the bottom-center (the exit)."""
        if not region:
            return None
        cx, cy = width // 2, height - 2
        # Closest region tile to (cx, cy) by Manhattan distance, deterministic
        # tie-break on (y desc, x asc) — prefers exits near the bottom.
        choice = min(region, key=lambda p: (abs(p[0] - cx) + abs(p[1] - cy), -p[1], p[0]))
        x, y = choice
        grid[y][x] = DOOR
        return (x, y)

    @staticmethod
    def _anchors_in_region(
        door_xy: tuple[int, int] | None,
        region: list[tuple[int, int]],
        n: int,
    ) -> list[tuple[int, int]]:
        """Pick N anchors as the FURTHEST cells from the door (depth = adventure).

        Deterministic via the region's BFS-discovery order combined with a stable
        Manhattan sort. Skips the door tile itself.
        """
        if n <= 0 or not region:
            return []
        if door_xy is None:
            door_xy = region[0]
        dx, dy = door_xy
        candidates = [p for p in region if p != door_xy]
        candidates.sort(key=lambda p: (-(abs(p[0] - dx) + abs(p[1] - dy)), p[1], p[0]))
        return candidates[:n]
