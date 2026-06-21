"""Canonical world loader — the ONE hand-curated world we ship.

When ``apps/api/data/world/canonical.json`` is present, the runtime serves it
instead of the seed-procedural overworld. Interior POIs match by ``kind:x:y``
to interior bundles under ``apps/api/data/world/interiors/``.

Loading is cheap + memoized (the artifact is small JSON read once at import +
on demand). Falls through to procgen on any error so a malformed artifact can
never break the game.

Public surface:
    get_canonical_world() -> CanonicalWorld | None
    get_canonical_tile(x: int, y: int) -> int | None
    get_canonical_tile_window(origin_x, origin_y, width, height) -> tiles | None
    get_canonical_interior(poi_key: str) -> CanonicalInterior | None
    clear_cache() — test/maintenance hook
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.schemas import WorldSpecLite

CANONICAL_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "world" / "canonical.json"
)
INTERIORS_DIR = (
    Path(__file__).resolve().parents[2] / "data" / "world" / "interiors"
)
CHUNKS_DIR = Path(__file__).resolve().parents[2] / "data" / "world" / "chunks"
DEFAULT_CHUNK_SIZE = 64


@dataclass
class CanonicalWorld:
    spec: WorldSpecLite
    tiles: list[list[int]]
    chunk_size: int = DEFAULT_CHUNK_SIZE


@dataclass
class CanonicalInterior:
    spec: WorldSpecLite
    tiles: list[list[int]]


_world_cache: CanonicalWorld | None = None
_world_loaded: bool = False
_interior_cache: dict[str, CanonicalInterior] = {}
_chunk_cache: dict[tuple[int, int], list[list[int]]] = {}


def get_canonical_world() -> CanonicalWorld | None:
    """Return the canonical overworld bundle, or ``None`` if not present/bad.

    Memoized — the JSON is read at most once per process. On any parse/validate
    error returns ``None`` so the caller falls back to procgen.
    """
    global _world_cache, _world_loaded
    if _world_loaded:
        return _world_cache
    _world_loaded = True
    try:
        data = json.loads(CANONICAL_PATH.read_text(encoding="utf-8"))
        spec = WorldSpecLite.model_validate(data["world"])
        tiles = data.get("tiles") or []
        chunk_size = int(data.get("chunk_size") or DEFAULT_CHUNK_SIZE)
        if tiles and not _validate_tiles(tiles, spec.width, spec.height):
            return None
        if not tiles and not CHUNKS_DIR.exists():
            return None
        _world_cache = CanonicalWorld(
            spec=spec,
            tiles=tiles,
            chunk_size=chunk_size,
        )
    except FileNotFoundError:
        _world_cache = None
    except Exception:  # malformed JSON / schema mismatch — never crash the game
        _world_cache = None
    return _world_cache


def get_canonical_tile(x: int, y: int) -> int | None:
    """Return one canonical overworld tile by global coord, or None if missing."""
    window = get_canonical_tile_window(x, y, 1, 1)
    if window is None:
        return None
    return window[0][0]


def get_canonical_tile_window(
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
) -> list[list[int]] | None:
    """Return a global overworld tile window from full tiles or chunk files.

    Out-of-bounds cells are filled as blocked (1) so callers can safely request
    edge-adjacent windows without doing separate clipping math.
    """
    world = get_canonical_world()
    if world is None:
        return None
    if width <= 0 or height <= 0:
        return []

    if world.tiles:
        return _slice_tiles(world.tiles, origin_x, origin_y, width, height)

    rows: list[list[int]] = []
    for y in range(origin_y, origin_y + height):
        row: list[int] = []
        for x in range(origin_x, origin_x + width):
            row.append(_tile_from_chunk(world, x, y))
        rows.append(row)
    return rows


def get_canonical_interior(poi_key: str) -> CanonicalInterior | None:
    """Return the canonical interior for ``poi_key`` ("kind:x:y"), or None."""
    if poi_key in _interior_cache:
        return _interior_cache[poi_key]
    safe = poi_key.replace(":", "_").replace("/", "_")
    path = INTERIORS_DIR / f"{safe}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        spec = WorldSpecLite.model_validate(data["world"])
        tiles = data["tiles"]
        if not _validate_tiles(tiles, spec.width, spec.height):
            return None
        bundle = CanonicalInterior(spec=spec, tiles=tiles)
        _interior_cache[poi_key] = bundle
        return bundle
    except FileNotFoundError:
        return None
    except Exception:
        return None


def clear_cache() -> None:
    """Test/maintenance hook: drop the memoized canonical world + interiors."""
    global _world_cache, _world_loaded
    _world_cache = None
    _world_loaded = False
    _interior_cache.clear()
    _chunk_cache.clear()


def _slice_tiles(
    tiles: list[list[int]], origin_x: int, origin_y: int, width: int, height: int
) -> list[list[int]]:
    """Slice a tile window, filling any out-of-bounds cells as blocked."""
    rows: list[list[int]] = []
    for y in range(origin_y, origin_y + height):
        row: list[int] = []
        for x in range(origin_x, origin_x + width):
            if y < 0 or y >= len(tiles) or x < 0 or x >= len(tiles[y]):
                row.append(1)
            else:
                row.append(int(tiles[y][x]))
        rows.append(row)
    return rows


def _tile_from_chunk(world: CanonicalWorld, x: int, y: int) -> int:
    """Read one global tile from the chunk cache; outside world is blocked."""
    if x < 0 or y < 0 or x >= world.spec.width or y >= world.spec.height:
        return 1

    chunk_size = world.chunk_size
    cx = x // chunk_size
    cy = y // chunk_size
    chunk = _load_chunk(cx, cy)
    if chunk is None:
        return 1
    lx = x - cx * chunk_size
    ly = y - cy * chunk_size
    if ly < 0 or ly >= len(chunk) or lx < 0 or lx >= len(chunk[ly]):
        return 1
    return int(chunk[ly][lx])


def _load_chunk(cx: int, cy: int) -> list[list[int]] | None:
    """Load one chunk file into the in-process cache."""
    key = (cx, cy)
    if key in _chunk_cache:
        return _chunk_cache[key]
    path = CHUNKS_DIR / f"{cx}_{cy}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tiles = data["tiles"] if isinstance(data, dict) else data
        if not isinstance(tiles, list):
            return None
        _chunk_cache[key] = tiles
        return tiles
    except Exception:
        return None


def _validate_tiles(tiles: Any, width: int, height: int) -> bool:
    """Cheap sanity check: 2D int grid of the expected dimensions."""
    if not isinstance(tiles, list) or len(tiles) != height:
        return False
    for row in tiles:
        if not isinstance(row, list) or len(row) != width:
            return False
    return True
