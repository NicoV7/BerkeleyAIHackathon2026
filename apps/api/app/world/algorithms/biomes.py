"""biomes.py — REAL overworld biome generator (Track B, Wave 2).

Replaces the old 4-quadrant random split (``build_regions`` in routers/world.py,
~:186-202) with smooth, organic biome bands derived from a deterministic
value-noise field. Two stacked noise dimensions (elevation + moisture) map every
tile to one of four biomes, so the overworld shows contiguous regions (forests,
mountains, wetlands, plains) instead of hard quadrant borders.

No external dependencies: we implement a tiny seeded VALUE NOISE (lattice of
seeded random values + smooth bilinear interpolation). This is "Perlin/simplex
in spirit" — a smooth, seed-deterministic scalar field — without numpy/noise libs.
A teammate can swap in true simplex later behind the same interface; the contract
(seed -> stable biome map) is what matters.

Determinism: every value comes from ``random.Random(seed ^ MASK)``. Identical
(seed, width, height) -> identical regions + biome-per-tile. Verified by the
determinism test.

KNOBS (tune for look):
    NOISE_SCALE     — lattice cell size in tiles; larger = bigger biome blobs.
    ELEVATION_MASK  — RNG stream for the elevation field.
    MOISTURE_MASK   — RNG stream for the moisture field.
    REGION_CELLS    — how the biome map is summarized into coarse Region boxes.
"""
from __future__ import annotations

import random

from app.schemas import POI, Region
from app.world.algorithms.base import WALKABLE, GenResult, WorldGenerator

# --- KNOBS ---------------------------------------------------------------------
NOISE_SCALE = 6.0  # tiles per noise lattice cell; bigger => larger biome blobs
ELEVATION_MASK = 0xE1E7  # seeded stream for the elevation field
MOISTURE_MASK = 0x303D  # seeded stream for the moisture field
REGION_CELLS = (2, 2)  # coarse Region grid (cols, rows) summarizing the biome map

# Biome assignment from (elevation, moisture), each in [0, 1).
#   high elevation            -> mountains
#   low elevation + wet       -> wetland
#   low elevation + moist     -> forest
#   otherwise                 -> plains
_BIOMES = ("plains", "forest", "mountains", "wetland")


def _smoothstep(t: float) -> float:
    """Hermite smoothing (3t^2 - 2t^3) for soft lattice interpolation."""
    return t * t * (3.0 - 2.0 * t)


class _ValueNoise:
    """Deterministic 2D value noise: seeded lattice + smooth bilinear interp.

    Lattice corner values are produced on demand from a seeded RNG keyed by the
    integer cell coords, so the field is reproducible and unbounded without
    storing a grid. (Hash-based corner value -> classic value noise.)
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed & 0xFFFFFFFF

    def _corner(self, ix: int, iy: int) -> float:
        # Mix integer lattice coords + seed into a stable per-corner value [0,1).
        h = (ix * 374761393 + iy * 668265263 + self._seed * 2246822519) & 0xFFFFFFFF
        return random.Random(h).random()

    def at(self, x: float, y: float) -> float:
        x0, y0 = int(x // 1), int(y // 1)
        fx, fy = x - x0, y - y0
        sx, sy = _smoothstep(fx), _smoothstep(fy)
        c00 = self._corner(x0, y0)
        c10 = self._corner(x0 + 1, y0)
        c01 = self._corner(x0, y0 + 1)
        c11 = self._corner(x0 + 1, y0 + 1)
        top = c00 + sx * (c10 - c00)
        bot = c01 + sx * (c11 - c01)
        return top + sy * (bot - top)


def _biome_for(elevation: float, moisture: float) -> str:
    """Map (elevation, moisture) in [0,1) to a biome name."""
    if elevation > 0.62:
        return "mountains"
    if elevation < 0.35:
        return "wetland" if moisture > 0.5 else "plains"
    return "forest" if moisture > 0.45 else "plains"


class BiomeGenerator(WorldGenerator):
    """Overworld biome assignment via stacked value-noise fields."""

    name = "biomes"
    mask = 0xB10E  # "BIOME" stream

    def biome_map(self, seed: int, width: int, height: int) -> list[list[str]]:
        """Per-tile biome grid (height x width) — the smooth biome field.

        Public so the router / FE renderer can color tiles by biome later. Pure
        + deterministic in (seed, width, height).
        """
        elev = _ValueNoise(seed ^ ELEVATION_MASK ^ self.mask)
        moist = _ValueNoise(seed ^ MOISTURE_MASK ^ self.mask)
        grid: list[list[str]] = []
        for y in range(height):
            row: list[str] = []
            for x in range(width):
                nx, ny = x / NOISE_SCALE, y / NOISE_SCALE
                row.append(_biome_for(elev.at(nx, ny), moist.at(nx, ny)))
            grid.append(row)
        return grid

    def regions(self, seed: int, width: int, height: int) -> list[Region]:
        """Summarize the biome map into coarse Region boxes (the dominant biome
        per coarse cell). Replaces the random-quadrant split with biome-aware
        regions whose biome reflects the actual noise field underneath.
        """
        bmap = self.biome_map(seed, width, height)
        cols, rows = REGION_CELLS
        cw = max(1, width // cols)
        ch = max(1, height // rows)
        regions: list[Region] = []
        compass = [["Northwest", "Northeast"], ["Southwest", "Southeast"]]
        for ry in range(rows):
            for rx in range(cols):
                x0, y0 = rx * cw, ry * ch
                x1 = width - 1 if rx == cols - 1 else (rx + 1) * cw - 1
                y1 = height - 1 if ry == rows - 1 else (ry + 1) * ch - 1
                # Dominant biome in this box.
                counts: dict[str, int] = {}
                for yy in range(y0, y1 + 1):
                    for xx in range(x0, x1 + 1):
                        b = bmap[yy][xx]
                        counts[b] = counts.get(b, 0) + 1
                # max() over a deterministic (count desc, name asc) key.
                biome = max(sorted(counts), key=lambda b: counts[b])
                name = (
                    compass[ry][rx]
                    if ry < len(compass) and rx < len(compass[0])
                    else f"Region {ry}-{rx}"
                )
                regions.append(Region(name=name, biome=biome, bounds=[x0, y0, x1, y1]))
        return regions

    def generate(self, seed: int, width: int, height: int) -> GenResult:
        """Produce overworld regions from noise. Tiles/POIs are owned by the
        existing map.py + place_pois pipeline, so this generator returns a
        walkable-only grid and empty POIs — its job is the BIOME LAYER. The
        router merges these biome-aware regions into the WorldSpecLite.
        """
        tiles = [[WALKABLE] * width for _ in range(height)]
        return GenResult(
            width=width,
            height=height,
            tiles=tiles,
            regions=self.regions(seed, width, height),
            pois=[],
        )
