"""bsp_dungeon.py — STUB: BSP room/corridor dungeon generator (Track B, Wave 2).

⚠️ SKELETON for a teammate. Implements the interface and emits a SMALL, VALID,
deterministic dungeon (rooms carved into a wall grid + corridors connecting them)
so the /interior endpoint works end-to-end. The recursive BSP split is shallow
and corridor routing is L-shaped only; the TODOs mark the real work.

Binary Space Partitioning dungeon (the classic rooms-and-corridors look):
  1. Recursively split the grid into sub-regions (alternating axis) down to
     ``MIN_LEAF`` size.
  2. Carve one room inside each leaf (with ``ROOM_PAD`` margin).
  3. Connect sibling rooms with L-shaped corridors (carved as FLOOR).
  4. Drop a DOOR exit in the first room.

KNOBS:
    MIN_LEAF   — smallest partition before we stop splitting (room granularity).
    ROOM_PAD   — wall margin between a room and its leaf bounds.
    MAX_DEPTH  — recursion cap (defensive; also bounds room count).

TODO(teammate):
    - Connect across the BSP TREE (sibling-of-sibling), not just direct siblings,
      so every room is reachable (current L-corridors only join direct siblings).
    - Vary room sizes / add a boss room at the deepest leaf.
    - Place loot/enemy ANCHOR pois (FEATURE) and a goal/stairs.
    - Door/secret-passage tiles; themed tilesets per depth.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from app.schemas import POI
from app.world.algorithms.base import (
    DOOR,
    FLOOR,
    WALL,
    GenResult,
    WorldGenerator,
)

# --- KNOBS ---
MIN_LEAF = 5
ROOM_PAD = 1
MAX_DEPTH = 4


@dataclass
class _Rect:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def cx(self) -> int:
        return (self.x0 + self.x1) // 2

    @property
    def cy(self) -> int:
        return (self.y0 + self.y1) // 2


class BspDungeonGenerator(WorldGenerator):
    """BSP rooms + L-corridors. STUB — see module docstring TODOs."""

    name = "dungeon"
    mask = 0xB597  # "BSP" stream

    def generate(self, seed: int, width: int, height: int) -> GenResult:
        rng = random.Random(seed ^ self.mask)
        grid = [[WALL] * width for _ in range(height)]

        rooms: list[_Rect] = []
        self._split(rng, _Rect(1, 1, width - 2, height - 2), 0, grid, rooms)

        # Connect rooms in placement order with L-shaped corridors (best-effort;
        # see TODO about tree-aware connectivity).
        for a, b in zip(rooms, rooms[1:]):
            self._corridor(grid, a.cx, a.cy, b.cx, b.cy)

        pois: list[POI] = []
        if rooms:
            first = rooms[0]
            grid[first.cy][first.cx] = DOOR
            pois.append(POI(kind="start", x=first.cx, y=first.cy, name="Stairs Up"))
            last = rooms[-1]
            pois.append(POI(kind="goal", x=last.cx, y=last.cy, name="Deep Chamber"))

        return GenResult(width=width, height=height, tiles=grid, regions=[], pois=pois)

    def _split(
        self,
        rng: random.Random,
        rect: _Rect,
        depth: int,
        grid: list[list[int]],
        rooms: list[_Rect],
    ) -> None:
        w = rect.x1 - rect.x0
        h = rect.y1 - rect.y0
        # Stop: too small or too deep -> carve a room in this leaf.
        if depth >= MAX_DEPTH or (w < MIN_LEAF * 2 and h < MIN_LEAF * 2):
            self._carve_room(rng, rect, grid, rooms)
            return

        # Choose split axis (prefer splitting the longer dimension).
        split_vert = w > h if w != h else rng.random() < 0.5
        if split_vert and w >= MIN_LEAF * 2:
            cut = rng.randint(rect.x0 + MIN_LEAF, rect.x1 - MIN_LEAF)
            self._split(rng, _Rect(rect.x0, rect.y0, cut, rect.y1), depth + 1, grid, rooms)
            self._split(rng, _Rect(cut + 1, rect.y0, rect.x1, rect.y1), depth + 1, grid, rooms)
        elif h >= MIN_LEAF * 2:
            cut = rng.randint(rect.y0 + MIN_LEAF, rect.y1 - MIN_LEAF)
            self._split(rng, _Rect(rect.x0, rect.y0, rect.x1, cut), depth + 1, grid, rooms)
            self._split(rng, _Rect(rect.x0, cut + 1, rect.x1, rect.y1), depth + 1, grid, rooms)
        else:
            self._carve_room(rng, rect, grid, rooms)

    def _carve_room(
        self,
        rng: random.Random,
        leaf: _Rect,
        grid: list[list[int]],
        rooms: list[_Rect],
    ) -> None:
        x0 = leaf.x0 + ROOM_PAD
        y0 = leaf.y0 + ROOM_PAD
        x1 = leaf.x1 - ROOM_PAD
        y1 = leaf.y1 - ROOM_PAD
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        # Optionally shrink for variety (deterministic via rng).
        if x1 - x0 > 2:
            x0 += rng.randint(0, 1)
        if y1 - y0 > 2:
            y1 -= rng.randint(0, 1)
        room = _Rect(x0, y0, x1, y1)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                grid[y][x] = FLOOR
        rooms.append(room)

    @staticmethod
    def _corridor(grid: list[list[int]], x0: int, y0: int, x1: int, y1: int) -> None:
        """Carve an L-shaped FLOOR corridor between two points."""
        for x in range(min(x0, x1), max(x0, x1) + 1):
            grid[y0][x] = FLOOR
        for y in range(min(y0, y1), max(y0, y1) + 1):
            grid[y][x1] = FLOOR
