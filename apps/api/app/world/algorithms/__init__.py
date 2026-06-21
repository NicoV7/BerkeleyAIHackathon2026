"""Pluggable procedural world-generation algorithms (Track B, Wave 2).

This package is a SKELETON for a teammate to flesh out. It defines a single
pluggable interface — ``WorldGenerator`` — and a registry, with:

  * ONE real implementation:
      - ``biomes.BiomeGenerator``     — Perlin/value-noise biome assignment for
        the OVERWORLD, replacing the old 4-quadrant random split.

  * THREE interior STUBS (interface satisfied, minimal output + clear TODO/knobs):
      - ``caves.CaveGenerator``       — cellular-automata caves (dens/dungeons).
      - ``bsp_dungeon.BspDungeonGenerator`` — BSP room/corridor dungeon layout.
      - ``town.TownGenerator``        — structured town interior (buildings/paths).

Determinism contract (the part that MUST stay stable — see tests/):
    Every generator derives ALL randomness from ``random.Random(seed ^ MASK)``
    with fixed per-generator masks. Identical (seed, width, height) ALWAYS yields
    byte-for-byte identical output. No wall-clock, no unseeded RNG, no I/O.
    (Runtime enemy FSM positions are intentionally NON-deterministic and are NOT
    covered by this contract — see the design doc, Track B determinism note.)

Public surface:
    WorldGenerator           — the interface (ABC) every algorithm implements.
    GenResult                — what generate() returns (tiles + regions + pois).
    get_generator(kind)      — registry lookup; returns a generator instance.
    INTERIOR_KINDS           — the interior generator kinds the endpoint accepts.
"""
from __future__ import annotations

from app.world.algorithms.base import GenResult, WorldGenerator
from app.world.algorithms.biomes import BiomeGenerator
from app.world.algorithms.bsp_dungeon import BspDungeonGenerator
from app.world.algorithms.caves import CaveGenerator
from app.world.algorithms.town import TownGenerator

# Registry: kind -> generator instance. Generators are stateless (all state is
# derived from the seed at generate() time), so a shared instance is safe.
_REGISTRY: dict[str, WorldGenerator] = {
    "biomes": BiomeGenerator(),
    "town": TownGenerator(),
    "cave": CaveGenerator(),
    "dungeon": BspDungeonGenerator(),
}

# Which kinds are valid INTERIOR generators (the /interior endpoint clamps to
# these). "biomes" is the overworld generator and is intentionally excluded.
INTERIOR_KINDS: tuple[str, ...] = ("town", "cave", "dungeon")


def get_generator(kind: str) -> WorldGenerator:
    """Return the generator registered for ``kind``.

    Falls back to the cave generator for an unknown interior kind so a bad hint
    can never raise — the caller (the interior endpoint) is non-throwing by
    contract. Raises only for a genuinely empty registry (programmer error).
    """
    gen = _REGISTRY.get(kind)
    if gen is not None:
        return gen
    return _REGISTRY["cave"]


__all__ = [
    "WorldGenerator",
    "GenResult",
    "get_generator",
    "INTERIOR_KINDS",
    "BiomeGenerator",
    "CaveGenerator",
    "BspDungeonGenerator",
    "TownGenerator",
]
