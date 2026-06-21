"""watabou_import.py — Convert Watabou Procgen Arcana JSON into WorldSpecLite.

Supports two Watabou tools (https://watabou.github.io):
  - one-page-dungeon: simple JSON with rectangular ``rects`` + ``doors``
  - city / town generator: GeoJSON FeatureCollection of building Polygons

For both, vector data is rasterized to our integer collision grid:
    WALKABLE (0), BLOCKED (1), DOOR (3), FEATURE (4)
with hand-picked target ``width`` and ``height`` tiles. The Watabou data is
scaled uniformly (preserving aspect ratio) inside a 1-tile wall border, so the
output always has a solid frame and the interior reflects the source layout.

This is deliberately tolerant: Watabou's schemas have evolved over time and
field names vary (``w`` vs ``width``, rect arrays nested differently). The
importer accepts the common subset and falls back gracefully on missing fields.

Pure + deterministic in (data, width, height). No I/O beyond reading the file
in the public entry point; tests can pass a parsed dict directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.schemas import POI, Region, WorldSpecLite
from app.world.algorithms.base import BLOCKED, DOOR, FEATURE, WALKABLE


@dataclass
class WatabouImport:
    """Bundle returned by the importer: layout contract + tile grid.

    ``spec`` is the frozen ``WorldSpecLite`` the API serves. ``tiles`` is the
    rasterized grid used for client collision + canonical artifact serialization
    (the bake script writes both into the canonical JSON).
    """

    spec: WorldSpecLite
    tiles: list[list[int]]


def import_watabou(
    path: str | Path,
    width: int,
    height: int,
    *,
    name: str | None = None,
    seed: int = 0,
) -> WatabouImport:
    """Read a Watabou JSON export from ``path`` and rasterize.

    Auto-detects the Watabou variant by inspecting top-level keys. Raises
    ``ValueError`` if the file is not a recognisable Watabou shape; the caller
    is expected to validate before commit-time.
    """
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    name = name or p.stem
    return import_watabou_data(data, width, height, name=name, seed=seed)


def import_watabou_data(
    data: dict[str, Any],
    width: int,
    height: int,
    *,
    name: str,
    seed: int = 0,
) -> WatabouImport:
    """Detect shape + dispatch. Public so tests can pass a parsed dict."""
    if data.get("type") == "FeatureCollection":
        return _import_geojson(data, width, height, name=name, seed=seed)
    if "rects" in data or "rooms" in data:
        return _import_dungeon(data, width, height, name=name, seed=seed)
    raise ValueError(
        f"Unrecognised Watabou JSON for '{name}': "
        "expected GeoJSON FeatureCollection (town) or rects/rooms (dungeon)"
    )


# ---------------------------------------------------------------------------
# One Page Dungeon (simple rects + doors)
# ---------------------------------------------------------------------------

def _import_dungeon(
    data: dict[str, Any],
    width: int,
    height: int,
    *,
    name: str,
    seed: int,
) -> WatabouImport:
    rects = [_norm_rect(r) for r in (data.get("rects") or data.get("rooms") or [])]
    if not rects:
        raise ValueError(f"Watabou dungeon '{name}' has no rects")

    min_x = min(r["x"] for r in rects)
    min_y = min(r["y"] for r in rects)
    max_x = max(r["x"] + r["w"] for r in rects)
    max_y = max(r["y"] + r["h"] for r in rects)
    scale = _fit_scale(max_x - min_x, max_y - min_y, width, height)

    tiles = _solid(width, height)
    for r in rects:
        _fill_rect(tiles, r, min_x, min_y, scale, width, height, value=WALKABLE)

    pois: list[POI] = []
    for d in data.get("doors") or []:
        dx, dy = _xy_to_tile(d, min_x, min_y, scale)
        if 0 < dx < width - 1 and 0 < dy < height - 1:
            tiles[dy][dx] = DOOR
            pois.append(POI(kind="landmark", x=dx, y=dy, name="Door"))

    # Start = centroid of first rect; goal = centroid of last rect. Drop a
    # FEATURE on a "notable" room midway (notes/columns/water if present).
    first = rects[0]
    last = rects[-1]
    sx, sy = _rect_center_tile(first, min_x, min_y, scale)
    gx, gy = _rect_center_tile(last, min_x, min_y, scale)
    start = POI(kind="start", x=sx, y=sy, name=_title(data, "Entrance"))
    goal = POI(kind="goal", x=gx, y=gy, name="Deep Chamber") if last is not first else None

    # Optional "boss anchor" — the middle rect, marked FEATURE.
    if len(rects) >= 3:
        mid = rects[len(rects) // 2]
        mx, my = _rect_center_tile(mid, min_x, min_y, scale)
        if 0 < mx < width - 1 and 0 < my < height - 1 and tiles[my][mx] == WALKABLE:
            tiles[my][mx] = FEATURE
            pois.append(POI(kind="landmark", x=mx, y=my, name="Throne of Echoes"))

    pois.insert(0, start)
    if goal is not None:
        pois.append(goal)

    region_name = _title(data, default=name.replace("_", " ").title())
    spec = WorldSpecLite(
        seed=seed,
        width=width,
        height=height,
        regions=[
            Region(
                name=region_name,
                biome="dungeon",
                bounds=[0, 0, width - 1, height - 1],
            )
        ],
        pois=pois,
        start=start,
        goal=goal,
    )
    return WatabouImport(spec=spec, tiles=tiles)


# ---------------------------------------------------------------------------
# Town / city (GeoJSON FeatureCollection of building Polygons)
# ---------------------------------------------------------------------------

def _import_geojson(
    data: dict[str, Any],
    width: int,
    height: int,
    *,
    name: str,
    seed: int,
) -> WatabouImport:
    coords = _all_coords(data)
    if not coords:
        raise ValueError(f"Watabou GeoJSON '{name}' has no polygon coordinates")

    min_x = min(c[0] for c in coords)
    min_y = min(c[1] for c in coords)
    max_x = max(c[0] for c in coords)
    max_y = max(c[1] for c in coords)
    scale = _fit_scale(max_x - min_x, max_y - min_y, width, height)

    # Towns are walkable plazas. Start with all-WALKABLE + solid border.
    tiles = _open_with_border(width, height)

    pois: list[POI] = []
    building_idx = 0
    for feat in data.get("features") or []:
        polygons = _feature_polygons(feat)
        for ring in polygons:
            building_idx += 1
            label = (feat.get("properties") or {}).get("type") or "Building"
            poi = _stamp_building(
                ring, tiles, min_x, min_y, scale, width, height, label, building_idx
            )
            if poi is not None:
                pois.append(poi)

    # Entry gate: nearest walkable tile to bottom-center. Search outward in
    # rings so a building at center-bottom doesn't drop the gate entirely.
    gate = _nearest_walkable_to(tiles, width // 2, height - 2, width, height)
    if gate is not None:
        ex, ey = gate
        tiles[ey][ex] = DOOR
        start = POI(kind="start", x=ex, y=ey, name="Town Gate")
        pois.insert(0, start)
    else:
        # Degenerate fully-blocked town (unlikely): pin start to (1,1).
        start = POI(kind="start", x=1, y=1, name="Town Gate")
        pois.insert(0, start)

    region_name = (
        (data.get("properties") or {}).get("name")
        or _title(data, default=name.replace("_", " ").title())
    )
    spec = WorldSpecLite(
        seed=seed,
        width=width,
        height=height,
        regions=[
            Region(
                name=region_name,
                biome="town",
                bounds=[0, 0, width - 1, height - 1],
            )
        ],
        pois=pois,
        start=start,
        goal=None,
    )
    return WatabouImport(spec=spec, tiles=tiles)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_rect(r: dict[str, Any]) -> dict[str, float]:
    return {
        "x": float(r.get("x", 0)),
        "y": float(r.get("y", 0)),
        "w": float(r.get("w") or r.get("width") or 1),
        "h": float(r.get("h") or r.get("height") or 1),
    }


def _fit_scale(src_w: float, src_h: float, dst_w: int, dst_h: int) -> float:
    """Uniform scale that fits ``(src_w, src_h)`` inside the dst grid with a
    1-tile border. Falls back to 1.0 if the source is empty."""
    if src_w <= 0 or src_h <= 0:
        return 1.0
    return min((dst_w - 2) / src_w, (dst_h - 2) / src_h)


def _solid(width: int, height: int) -> list[list[int]]:
    return [[BLOCKED] * width for _ in range(height)]


def _open_with_border(width: int, height: int) -> list[list[int]]:
    tiles = [[WALKABLE] * width for _ in range(height)]
    for x in range(width):
        tiles[0][x] = BLOCKED
        tiles[height - 1][x] = BLOCKED
    for y in range(height):
        tiles[y][0] = BLOCKED
        tiles[y][width - 1] = BLOCKED
    return tiles


def _fill_rect(
    tiles: list[list[int]],
    r: dict[str, float],
    min_x: float,
    min_y: float,
    scale: float,
    width: int,
    height: int,
    *,
    value: int,
) -> None:
    x0 = max(1, int(round((r["x"] - min_x) * scale)) + 1)
    y0 = max(1, int(round((r["y"] - min_y) * scale)) + 1)
    x1 = min(width - 1, int(round((r["x"] + r["w"] - min_x) * scale)) + 1)
    y1 = min(height - 1, int(round((r["y"] + r["h"] - min_y) * scale)) + 1)
    for ty in range(y0, y1):
        for tx in range(x0, x1):
            tiles[ty][tx] = value


def _xy_to_tile(
    p: dict[str, Any], min_x: float, min_y: float, scale: float
) -> tuple[int, int]:
    px = float(p.get("x", 0))
    py = float(p.get("y", 0))
    return (
        int(round((px - min_x) * scale)) + 1,
        int(round((py - min_y) * scale)) + 1,
    )


def _rect_center_tile(
    r: dict[str, float], min_x: float, min_y: float, scale: float
) -> tuple[int, int]:
    cx = r["x"] + r["w"] / 2.0
    cy = r["y"] + r["h"] / 2.0
    return (
        int(round((cx - min_x) * scale)) + 1,
        int(round((cy - min_y) * scale)) + 1,
    )


def _title(data: dict[str, Any], default: str) -> str:
    """Prefer the JSON's own title/name; fall back to the supplied default."""
    t = data.get("title") or data.get("name")
    return str(t) if t else default


def _all_coords(geojson: dict[str, Any]) -> list[list[float]]:
    out: list[list[float]] = []
    for feat in geojson.get("features") or []:
        for ring in _feature_polygons(feat):
            out.extend(ring)
    return out


def _feature_polygons(feat: dict[str, Any]) -> list[list[list[float]]]:
    """Return the exterior rings of all polygons in a feature.

    Handles ``Polygon`` (one ring set) and ``MultiPolygon`` (many) uniformly,
    returning a flat list of exterior rings. Holes are ignored — Watabou
    buildings are rendered as solid masses for collision.
    """
    geom = feat.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon" and coords:
        return [coords[0]]
    if gtype == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def _nearest_walkable_to(
    tiles: list[list[int]],
    target_x: int,
    target_y: int,
    width: int,
    height: int,
) -> tuple[int, int] | None:
    """Spiral outward from ``(target_x, target_y)`` to the first walkable tile.

    Used to place the town gate near bottom-center even when a building was
    rasterized over the natural spawn coordinate. Deterministic — scan order is
    fixed (concentric square rings, then row-major inside each ring).
    """
    max_r = max(width, height)
    for r in range(max_r):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                # Only scan the ring boundary, not the filled square.
                if max(abs(dx), abs(dy)) != r:
                    continue
                x = target_x + dx
                y = target_y + dy
                if 0 < x < width - 1 and 0 < y < height - 1 and tiles[y][x] == WALKABLE:
                    return (x, y)
    return None


def _stamp_building(
    ring: list[list[float]],
    tiles: list[list[int]],
    min_x: float,
    min_y: float,
    scale: float,
    width: int,
    height: int,
    label: str,
    idx: int,
) -> POI | None:
    """Rasterize a building's polygon as a BLOCKED bbox + a FEATURE front-door.

    Bbox-only (not true polygon rasterization) — keeps the importer dependency-
    free and fast. Watabou buildings are nearly rectangular so the loss is
    cosmetic. Returns the front-door POI, or None if the building won't fit.
    """
    if not ring:
        return None
    pts = [
        (
            int(round((c[0] - min_x) * scale)) + 1,
            int(round((c[1] - min_y) * scale)) + 1,
        )
        for c in ring
    ]
    bx0 = max(1, min(p[0] for p in pts))
    by0 = max(1, min(p[1] for p in pts))
    bx1 = min(width - 1, max(p[0] for p in pts))
    by1 = min(height - 1, max(p[1] for p in pts))
    if bx1 - bx0 < 1 or by1 - by0 < 1:
        return None
    for ty in range(by0, by1):
        for tx in range(bx0, bx1):
            tiles[ty][tx] = BLOCKED

    # Front-door FEATURE anchor just BELOW the building (walkable).
    fy = min(height - 2, by1 + 1)
    fx = max(1, min(width - 2, (bx0 + bx1) // 2))
    if tiles[fy][fx] == BLOCKED:
        return None
    tiles[fy][fx] = FEATURE
    return POI(kind="town", x=fx, y=fy, name=f"{label.title()} {idx}")
