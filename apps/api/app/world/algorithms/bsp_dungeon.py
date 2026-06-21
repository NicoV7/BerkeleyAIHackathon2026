"""bsp_dungeon.py — BSP room/corridor dungeon generator (Track B, Wave 2).

Binary Space Partitioning dungeon (rooms-and-corridors):
  1. Recursively split the grid into sub-regions (alternating axis) down to
     ``MIN_LEAF`` size, building a BSP tree.
  2. Carve one room inside each leaf (with ``ROOM_PAD`` margin).
  3. Connect rooms via the TREE (each parent links the centroids of its two
     children) — every room is reachable from every other.
  4. Drop a START door in the first room, a BOSS-room landmark in the largest
     room (the dramatic chamber), and a GOAL (Deep Chamber) in the last room.

KNOBS:
    MIN_LEAF   — smallest partition before we stop splitting (room granularity).
    ROOM_PAD   — wall margin between a room and its leaf bounds.
    MAX_DEPTH  — recursion cap (defensive; also bounds room count).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

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

    @property
    def area(self) -> int:
        return max(0, self.x1 - self.x0 + 1) * max(0, self.y1 - self.y0 + 1)


@dataclass
class _Node:
    """BSP tree node: a partition + its two children + its room (if leaf)."""

    rect: "_Rect"
    left: "_Node | None" = None
    right: "_Node | None" = None
    room: "_Rect | None" = None

    @property
    def link_xy(self) -> tuple[int, int]:
        """The centroid this node exposes upward for tree-connect — the room
        centroid if leaf, else the rect centroid (close enough for an L-corridor)."""
        if self.room is not None:
            return (self.room.cx, self.room.cy)
        return (self.rect.cx, self.rect.cy)


class BspDungeonGenerator(WorldGenerator):
    """BSP rooms + tree-aware L-corridors + boss-room landmark."""

    name = "dungeon"
    mask = 0xB597  # "BSP" stream

    def generate(self, seed: int, width: int, height: int) -> GenResult:
        rng = random.Random(seed ^ self.mask)
        grid = [[WALL] * width for _ in range(height)]

        rooms: list[_Rect] = []
        root = self._build(rng, _Rect(1, 1, width - 2, height - 2), 0, grid, rooms)

        # Tree-aware connectivity: every parent connects its children's link
        # points, so every room is reachable from every other.
        self._connect_tree(root, grid)

        pois: list[POI] = []
        if rooms:
            first = rooms[0]
            grid[first.cy][first.cx] = DOOR
            pois.append(POI(kind="start", x=first.cx, y=first.cy, name="Stairs Up"))

            # Boss room: the largest room that is NOT the first/last (the dramatic
            # chamber). FEATURE-marked so the FE renderer + NPC system can latch.
            boss = self._boss_room(rooms)
            if boss is not None and boss is not first:
                grid[boss.cy][boss.cx] = FEATURE
                pois.append(
                    POI(kind="landmark", x=boss.cx, y=boss.cy, name="Throne of Echoes")
                )

            last = rooms[-1]
            if last is not first:
                pois.append(
                    POI(kind="goal", x=last.cx, y=last.cy, name="Deep Chamber")
                )

        return GenResult(width=width, height=height, tiles=grid, regions=[], pois=pois)

    @staticmethod
    def _boss_room(rooms: list[_Rect]) -> _Rect | None:
        """Largest room excluding first/last; deterministic tie-break on coords."""
        if len(rooms) < 3:
            return rooms[len(rooms) // 2] if rooms else None
        middle = rooms[1:-1]
        return max(middle, key=lambda r: (r.area, -r.x0, -r.y0))

    def _build(
        self,
        rng: random.Random,
        rect: _Rect,
        depth: int,
        grid: list[list[int]],
        rooms: list[_Rect],
    ) -> _Node:
        """BSP build that returns the tree root (used for tree-aware corridors)."""
        node = _Node(rect=rect)
        w = rect.x1 - rect.x0
        h = rect.y1 - rect.y0
        if depth >= MAX_DEPTH or (w < MIN_LEAF * 2 and h < MIN_LEAF * 2):
            node.room = self._carve_room(rng, rect, grid, rooms)
            return node
        split_vert = w > h if w != h else rng.random() < 0.5
        if split_vert and w >= MIN_LEAF * 2:
            cut = rng.randint(rect.x0 + MIN_LEAF, rect.x1 - MIN_LEAF)
            node.left = self._build(
                rng, _Rect(rect.x0, rect.y0, cut, rect.y1), depth + 1, grid, rooms
            )
            node.right = self._build(
                rng,
                _Rect(cut + 1, rect.y0, rect.x1, rect.y1),
                depth + 1,
                grid,
                rooms,
            )
        elif h >= MIN_LEAF * 2:
            cut = rng.randint(rect.y0 + MIN_LEAF, rect.y1 - MIN_LEAF)
            node.left = self._build(
                rng, _Rect(rect.x0, rect.y0, rect.x1, cut), depth + 1, grid, rooms
            )
            node.right = self._build(
                rng,
                _Rect(rect.x0, cut + 1, rect.x1, rect.y1),
                depth + 1,
                grid,
                rooms,
            )
        else:
            node.room = self._carve_room(rng, rect, grid, rooms)
        return node

    def _connect_tree(self, node: _Node | None, grid: list[list[int]]) -> None:
        """Post-order tree walk: each parent connects its children's link points."""
        if node is None:
            return
        self._connect_tree(node.left, grid)
        self._connect_tree(node.right, grid)
        if node.left is not None and node.right is not None:
            ax, ay = node.left.link_xy
            bx, by = node.right.link_xy
            self._corridor(grid, ax, ay, bx, by)

    def _carve_room(
        self,
        rng: random.Random,
        leaf: _Rect,
        grid: list[list[int]],
        rooms: list[_Rect],
    ) -> _Rect | None:
        x0 = leaf.x0 + ROOM_PAD
        y0 = leaf.y0 + ROOM_PAD
        x1 = leaf.x1 - ROOM_PAD
        y1 = leaf.y1 - ROOM_PAD
        if x1 - x0 < 1 or y1 - y0 < 1:
            return None
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
        return room

    @staticmethod
    def _corridor(grid: list[list[int]], x0: int, y0: int, x1: int, y1: int) -> None:
        """Carve an L-shaped FLOOR corridor between two points."""
        for x in range(min(x0, x1), max(x0, x1) + 1):
            grid[y0][x] = FLOOR
        for y in range(min(y0, y1), max(y0, y1) + 1):
            grid[y][x1] = FLOOR
