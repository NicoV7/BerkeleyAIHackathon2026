"""WorldGenerator interface + shared result type (Track B, Wave 2).

Every procedural algorithm (overworld biomes, interior caves/dungeons/towns)
implements ``WorldGenerator``. The contract is deliberately tiny so a teammate
can drop in a new algorithm by subclassing and registering it in
``algorithms/__init__.py``.

Determinism is part of the contract: ``generate(seed, width, height)`` MUST be a
pure function of its arguments (seed-derived RNG only). See the package docstring.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.schemas import POI, Region

# Tile legend (shared with the overworld, map.py): matches the FE renderer.
#   0 = grass/walkable, 1 = blocked/wall, 2 = campsite overlay (walkable).
# Interiors additionally use these *walkable* semantic markers so the FE can
# render them distinctly without breaking the existing legend. The expansive
# overworld adds semantic walkable terrain plus blocked water/mountain types.
WALKABLE = 0
BLOCKED = 1
CAMP = 2
FLOOR = 0  # interior floor (alias of walkable)
WALL = 1  # interior wall (alias of blocked)
DOOR = 3  # interior exit/entrance marker (walkable)
FEATURE = 4  # interior feature: chest/altar/NPC anchor etc. (walkable)
FOREST = 5  # overworld forest floor (walkable)
WATER = 6  # overworld water (blocked)
MOUNTAIN = 7  # overworld mountains/cliffs (blocked)
TOWN = 8  # overworld/town plaza (walkable)
CAVE = 9  # cave or dungeon entrance (walkable)
BLOCKED_TILES = {BLOCKED, WATER, MOUNTAIN}


@dataclass
class GenResult:
    """The raw output of a generator, before it is wrapped in a WorldSpecLite.

    Keeping this separate from ``WorldSpecLite`` lets generators return the tile
    grid too (interiors need it; the overworld already has its grid from map.py),
    while the router decides how to assemble the final frozen response.
    """

    width: int
    height: int
    tiles: list[list[int]]
    regions: list[Region] = field(default_factory=list)
    pois: list[POI] = field(default_factory=list)


class WorldGenerator(ABC):
    """Pluggable procedural generator. Subclass + register to add an algorithm."""

    #: Stable name used as the registry key (see algorithms/__init__.py).
    name: str = "base"

    #: Per-generator RNG mask, XOR'd with the seed so different algorithms on the
    #: same seed produce independent streams (one algorithm changing never
    #: perturbs another's output). Subclasses MUST override with a unique value.
    mask: int = 0x0000

    @abstractmethod
    def generate(self, seed: int, width: int, height: int) -> GenResult:
        """Produce a deterministic ``GenResult`` for ``(seed, width, height)``.

        MUST be pure: identical args -> identical output. Derive ALL randomness
        from ``random.Random(seed ^ self.mask)``. Never touch wall-clock, env,
        or unseeded RNG.
        """
        raise NotImplementedError
