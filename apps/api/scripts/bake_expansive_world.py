"""Bake the hackathon's chunked 1024x1024 canonical overworld.

The runtime serves ``canonical.json`` for world identity (regions, POIs, NPCs)
and reads terrain from 64x64 chunk files on demand. That keeps the world large
enough to explore without making every map request ship a million tiles.

Run from ``apps/api``:
    uv run python scripts/bake_expansive_world.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "world"
INTERIORS = DATA / "interiors"
CHUNKS = DATA / "chunks"
CANONICAL = DATA / "canonical.json"

GRASS = 0
BLOCKED = 1
CAMP = 2
ROAD = 3
FEATURE = 4
FOREST = 5
WATER = 6
MOUNTAIN = 7
TOWN = 8
CAVE = 9

WORLD_W = 1024
WORLD_H = 1024
CHUNK_SIZE = 64


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    INTERIORS.mkdir(parents=True, exist_ok=True)
    CHUNKS.mkdir(parents=True, exist_ok=True)
    for folder in (INTERIORS, CHUNKS):
        for old in folder.glob("*.json"):
            old.unlink()

    world, tiles = build_overworld()
    CANONICAL.write_text(pretty(world), encoding="utf-8")
    write_chunks(tiles)

    interiors = build_interiors()
    for key, interior in interiors.items():
        (INTERIORS / f"{key.replace(':', '_')}.json").write_text(
            pretty(interior), encoding="utf-8"
        )

    print(f"wrote {CANONICAL}")
    print(f"wrote {len(list(CHUNKS.glob('*.json')))} chunks")
    print(f"wrote {len(list(INTERIORS.glob('*.json')))} interiors")
    return 0


def build_overworld() -> tuple[dict[str, Any], list[list[int]]]:
    tiles = build_tiles()

    roads = [
        [(80, 656), (208, 560), (300, 600), (464, 496), (608, 384), (784, 320), (848, 128), (944, 96)],
        [(208, 560), (160, 480), (256, 288), (384, 192), (620, 130), (656, 160), (848, 128)],
        [(208, 560), (132, 792), (246, 880), (360, 740), (760, 780), (880, 760), (900, 620), (824, 470), (608, 384)],
        [(256, 288), (112, 160), (64, 96)],
        [(608, 384), (680, 520), (566, 620), (540, 900)],
        [(760, 780), (700, 600), (680, 520), (824, 470), (930, 420)],
    ]
    for route in roads:
        carve_route(tiles, route)

    pois = overworld_pois()
    for p in pois:
        stamp_poi(tiles, p)

    world = {
        "version": 3,
        "name": "Aldermere",
        "chunk_size": CHUNK_SIZE,
        "world": {
            "seed": 1729,
            "width": WORLD_W,
            "height": WORLD_H,
            "regions": regions(),
            "pois": pois,
            "start": pois[0],
            "goal": pois[-1],
        },
    }
    return world, tiles


def build_tiles() -> list[list[int]]:
    tiles: list[list[int]] = []
    for y in range(WORLD_H):
        row: list[int] = []
        for x in range(WORLD_W):
            row.append(terrain_at(x, y))
        tiles.append(row)
    return tiles


def terrain_at(x: int, y: int) -> int:
    if x == 0 or y == 0 or x == WORLD_W - 1 or y == WORLD_H - 1:
        return BLOCKED

    river_y = 560 + int(90 * math.sin(x / 88))
    if abs(y - river_y) <= 4 and 120 < x < 890:
        return WATER

    if ellipse(x, y, 214, 182, 175, 100) < 1.0:
        return WATER if noise(x, y) % 9 < 6 else FOREST
    if ellipse(x, y, 95, 875, 135, 95) < 1.0:
        return WATER if noise(x, y) % 11 < 7 else GRASS

    if x > 560 and y < 660:
        ridge = abs((x - 720) - y // 2)
        if ridge < 95 or noise(x, y) % 29 == 0:
            return MOUNTAIN
        if y < 220 and noise(x, y) % 5 == 0:
            return FOREST

    if 40 < x < 440 and 250 < y < 655 and noise(x, y) % 10 < 7:
        return FOREST
    if 40 < x < 355 and 650 <= y < 1010 and noise(x, y) % 10 < 4:
        return FOREST
    if 430 < x < 760 and y > 720 and noise(x, y) % 13 < 3:
        return FOREST
    return GRASS


def regions() -> list[dict[str, Any]]:
    return [
        region("Aldermere Meadows", "plains", [0, 610, 430, 1023], "Market roads, farms, orchards, and the safest early routes."),
        region("Whispering Wilds", "forest", [0, 260, 455, 650], "Old forest paths curl around shrines, barrows, and speaking trees."),
        region("Sunless Basin", "wetland", [0, 0, 455, 259], "Reeds, black water, drowned halls, and lantern towns on stilts."),
        region("Drystone Reach", "mountains", [456, 300, 1023, 660], "Quarries, keeps, switchbacks, mines, and argument-cut stone."),
        region("Frostcap Pass", "tundra", [560, 0, 1023, 299], "Cold ridges, thin air, and the final road to the Tribunal Gate."),
        region("Redleaf Lowlands", "forest", [0, 651, 355, 1023], "Autumn woods, mill roads, and quiet camps beside still water."),
        region("Starfall Uplands", "plains", [356, 661, 1023, 1023], "High fields, abbeys, observatories, and exposed roads under bright skies."),
        region("Emberfen March", "wetland", [0, 820, 220, 1023], "Warm marsh pools and old ferry stones at the edge of the known road."),
    ]


def region(name: str, biome: str, bounds: list[int], lore: str) -> dict[str, Any]:
    return {"name": name, "biome": biome, "bounds": bounds, "lore": lore}


def overworld_pois() -> list[dict[str, Any]]:
    towns = [
        town(208, 560, "Aldermere Village", [
            ("aldermere_innkeeper", "innkeeper", "Marin the Innkeeper", None),
            ("aldermere_merchant", "merchant", "Talia the Apothecary", None),
            ("aldermere_captain", "quest_giver", "Captain Veyl", None),
            ("socrates_anchor", "figure", "Socrates", "socrates"),
            ("curie_anchor", "figure", "Marie Curie", "curie"),
        ]),
        town(256, 288, "Reedmarket", [
            ("reedmarket_innkeeper", "innkeeper", "Oren of the Lamps", None),
            ("reedmarket_merchant", "merchant", "Sable the Ferryman", None),
            ("reedmarket_villager", "villager", "Nessa of the Reeds", None),
        ]),
        town(608, 384, "Quarrycross", [
            ("quarrycross_smith", "merchant", "Berrin the Smith", None),
            ("quarrycross_guard", "quest_giver", "Ser Hale", None),
            ("mlk_anchor", "figure", "Martin Luther King Jr.", "mlk"),
        ]),
        town(132, 792, "Brightmill", [
            ("brightmill_innkeeper", "innkeeper", "Pella Bright", None),
            ("brightmill_miller", "merchant", "Garron Millhand", None),
            ("brightmill_scholar", "villager", "Ives the Listener", None),
        ]),
        town(360, 740, "Lakehaven", [
            ("lakehaven_keeper", "innkeeper", "Mira of the Blue Porch", None),
            ("lakehaven_cartographer", "merchant", "Tovin Mapthread", None),
            ("lakehaven_warden", "quest_giver", "Warden Sol", None),
        ]),
        town(656, 160, "Northwatch", [
            ("northwatch_keeper", "innkeeper", "Helka Snowbell", None),
            ("northwatch_scout", "quest_giver", "Scout Ren", None),
            ("northwatch_trader", "merchant", "Orsik Coldpack", None),
        ]),
        town(824, 470, "Ironroot Hold", [
            ("ironroot_warden", "quest_giver", "Mael Ironroot", None),
            ("ironroot_smith", "merchant", "Vessa Anvil", None),
            ("ironroot_miner", "villager", "Tarn Deepcut", None),
        ]),
        town(760, 780, "Starfall Abbey", [
            ("starfall_prior", "quest_giver", "Prior Ansel", None),
            ("starfall_keeper", "innkeeper", "Lio of the Bells", None),
            ("starfall_archivist", "merchant", "Archivist Fen", None),
        ]),
    ]
    dens = [
        den(464, 496, "Glassroot Cave", "cave"),
        den(384, 192, "The Sunless Halls", "dungeon"),
        den(784, 320, "Drystone Keep", "dungeon"),
        den(848, 128, "Frostcap Cave", "cave"),
        den(112, 160, "Mirevault", "dungeon"),
        den(680, 520, "Echoing Mine", "cave"),
        den(270, 430, "Redleaf Barrow", "dungeon"),
        den(900, 620, "Stormglass Spire", "dungeon"),
        den(700, 76, "Old Observatory", "dungeon"),
        den(566, 620, "Deep Quarry", "cave"),
        den(160, 480, "Thornwold Den", "cave"),
        den(912, 112, "Tribunal Catacombs", "dungeon"),
    ]
    camps = [
        camp(300, 600, "Willow Camp"),
        camp(430, 760, "Bluebank Camp"),
        camp(520, 420, "Switchback Camp"),
        camp(720, 430, "Anvil Camp"),
        camp(815, 250, "Highroad Camp"),
        camp(128, 320, "Lantern Camp"),
        camp(620, 130, "Northwatch Camp"),
        camp(880, 760, "Starfall Camp"),
        camp(246, 880, "Redleaf Camp"),
        camp(540, 900, "Southwatch Camp"),
        camp(930, 420, "Stormroad Camp"),
        camp(64, 96, "Mire Edge Camp"),
    ]
    landmarks = [
        landmark(80, 656, "Greenward Trailhead", "start"),
        landmark(174, 604, "King's Road Shrine"),
        landmark(342, 590, "Old Stone Bridge"),
        landmark(225, 342, "Moonwell"),
        landmark(318, 510, "The Speaking Oak"),
        landmark(548, 320, "Scholar's Obelisk"),
        landmark(850, 210, "Windcut Pass"),
        landmark(940, 96, "The Tribunal Gate", "goal"),
        landmark(84, 880, "Emberfen Ferry"),
        landmark(198, 724, "Redleaf Gate"),
        landmark(448, 840, "Mirror Orchard"),
        landmark(612, 718, "Stone Choir"),
        landmark(742, 668, "Abbey Causeway"),
        landmark(940, 560, "Stormglass Road"),
        landmark(720, 252, "Frostline Cairn"),
        landmark(492, 548, "Glassroot Spring"),
        landmark(570, 910, "Southwatch Beacon"),
        landmark(332, 220, "Drowned Library Steps"),
        landmark(995, 380, "Eastern Boundary Marker"),
        landmark(48, 520, "Old West Gate"),
    ]

    start = landmarks[0]
    goal = landmarks[7]
    ordered = [start, *towns[:1], *landmarks[1:3], camps[0], dens[0]]
    ordered += towns[1:3] + landmarks[3:7] + camps[1:7] + dens[1:8]
    ordered += towns[3:] + camps[7:] + dens[8:] + landmarks[8:]
    ordered.append(goal)
    return ordered


def town(x: int, y: int, name: str, npc_defs: list[tuple[str, str, str, str | None]]) -> dict[str, Any]:
    offsets = [(-1, 0), (1, -1), (0, 2), (-2, 1), (2, 1)]
    anchors = []
    for i, (npc_id, archetype, npc_name, figure_id) in enumerate(npc_defs):
        ox, oy = offsets[i % len(offsets)]
        anchors.append(npc(npc_id, archetype, x + ox, y + oy, npc_name, figure_id))
    return poi("town", x, y, name, interior_kind="town", npcs=anchors)


def den(x: int, y: int, name: str, interior_kind: str) -> dict[str, Any]:
    return poi("den", x, y, name, interior_kind=interior_kind)


def camp(x: int, y: int, name: str) -> dict[str, Any]:
    return poi("camp", x, y, name)


def landmark(x: int, y: int, name: str, kind: str = "landmark") -> dict[str, Any]:
    return poi(kind, x, y, name)


def build_interiors() -> dict[str, dict[str, Any]]:
    interiors: dict[str, dict[str, Any]] = {}
    for p in overworld_pois():
        key = f"{p['kind']}:{p['x']}:{p['y']}"
        if p["kind"] == "town":
            interiors[key] = town_interior(
                p["name"],
                f"{p['name']} has shops, an inn, a local notice board, and roads back into Aldermere.",
                interior_anchors_for(p),
            )
        elif p["kind"] == "den":
            interiors[key] = dungeon_interior(
                p["name"],
                f"{p['name']} is one of Aldermere's named trial dungeons.",
                f"Heart of {p['name']}",
            )
    return interiors


def interior_anchors_for(poi_data: dict[str, Any]) -> list[dict[str, Any]]:
    spots = [(4, 5), (12, 5), (10, 9), (6, 8), (14, 9)]
    anchors = []
    for i, anchor in enumerate(poi_data.get("npc_anchors") or []):
        x, y = spots[i % len(spots)]
        anchors.append(
            npc(
                f"town_{poi_data['x']}_{poi_data['y']}__{i}",
                anchor["archetype"],
                x,
                y,
                anchor["name"],
                anchor.get("figure_id"),
            )
        )
    return anchors


def town_interior(name: str, lore: str, anchors: list[dict[str, Any]]) -> dict[str, Any]:
    width, height = 20, 14
    tiles = [[TOWN for _ in range(width)] for _ in range(height)]
    add_frame(tiles)
    for x in range(2, width - 2):
        tiles[7][x] = ROAD
    for y in range(2, height - 2):
        tiles[y][width // 2] = ROAD
    for rect in [(2, 2, 6, 4), (12, 2, 17, 4), (3, 9, 7, 11), (13, 9, 17, 11)]:
        fill_rect(tiles, rect, BLOCKED)
    for anchor in anchors:
        tiles[anchor["y"]][anchor["x"]] = FEATURE

    start = poi("start", width // 2, height - 2, "Town Gate")
    pois = [
        start,
        poi("landmark", 4, 3, "Inn"),
        poi("landmark", 14, 3, "Shop"),
        poi("landmark", width // 2, 7, "Debate Square", npcs=anchors),
    ]
    return bundle(name, tiles, "town", lore, pois, start, None)


def dungeon_interior(name: str, lore: str, boss_name: str) -> dict[str, Any]:
    width, height = 22, 16
    tiles = [[BLOCKED for _ in range(width)] for _ in range(height)]
    rooms = [(2, 2, 7, 5), (9, 2, 14, 5), (15, 4, 19, 8), (8, 8, 14, 12), (3, 10, 7, 13)]
    for rect in rooms:
        fill_rect(tiles, rect, ROAD)
    for route in [[(5, 4), (11, 4), (17, 6)], [(11, 4), (11, 10), (5, 12)]]:
        carve_route(tiles, route, radius=1)
    start = poi("start", 4, 4, name)
    boss = poi("landmark", 11, 10, boss_name)
    goal = poi("goal", 5, 12, "Deep Chamber")
    for p in [start, boss, goal]:
        tiles[p["y"]][p["x"]] = FEATURE if p["kind"] == "landmark" else ROAD
    return bundle(name, tiles, "dungeon", lore, [start, boss, goal], start, goal)


def bundle(
    name: str,
    tiles: list[list[int]],
    biome: str,
    lore: str,
    pois: list[dict[str, Any]],
    start: dict[str, Any],
    goal: dict[str, Any] | None,
) -> dict[str, Any]:
    width, height = len(tiles[0]), len(tiles)
    return {
        "version": 3,
        "name": name,
        "world": {
            "seed": 1729,
            "width": width,
            "height": height,
            "regions": [
                {
                    "name": name,
                    "biome": biome,
                    "bounds": [0, 0, width - 1, height - 1],
                    "lore": lore,
                }
            ],
            "pois": pois,
            "start": start,
            "goal": goal,
        },
        "tiles": tiles,
    }


def poi(
    kind: str,
    x: int,
    y: int,
    name: str,
    *,
    interior_kind: str | None = None,
    npcs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "x": x,
        "y": y,
        "name": name,
        "interior_seed": 1729000000 + x * 1000 + y if interior_kind else None,
        "interior_kind": interior_kind,
        "npc_anchors": npcs or [],
        "scripted": True,
    }


def npc(
    npc_id: str,
    archetype: str,
    x: int,
    y: int,
    name: str,
    figure_id: str | None = None,
) -> dict[str, Any]:
    return {
        "npc_id": npc_id,
        "archetype": archetype,
        "x": x,
        "y": y,
        "name": name,
        "figure_id": figure_id,
    }


def carve_route(
    tiles: list[list[int]], points: list[tuple[int, int]], *, radius: int = 2
) -> None:
    for a, b in zip(points, points[1:]):
        x, y = a
        bx, by = b
        while x != bx:
            stamp_walkable(tiles, x, y, ROAD, radius=radius)
            x += 1 if bx > x else -1
        while y != by:
            stamp_walkable(tiles, x, y, ROAD, radius=radius)
            y += 1 if by > y else -1
        stamp_walkable(tiles, x, y, ROAD, radius=radius)


def stamp_walkable(
    tiles: list[list[int]], x: int, y: int, value: int, *, radius: int = 2
) -> None:
    for yy in range(max(1, y - radius), min(len(tiles) - 1, y + radius + 1)):
        for xx in range(max(1, x - radius), min(len(tiles[0]) - 1, x + radius + 1)):
            if abs(xx - x) + abs(yy - y) <= radius:
                tiles[yy][xx] = value


def stamp_poi(tiles: list[list[int]], p: dict[str, Any]) -> None:
    value = {
        "camp": CAMP,
        "town": TOWN,
        "den": CAVE,
        "landmark": FEATURE,
        "start": ROAD,
        "goal": FEATURE,
    }[p["kind"]]
    radius = {"town": 5, "camp": 3, "den": 3, "goal": 4, "start": 3}.get(p["kind"], 2)
    stamp_walkable(tiles, p["x"], p["y"], value, radius=radius)


def add_frame(tiles: list[list[int]]) -> None:
    width, height = len(tiles[0]), len(tiles)
    for x in range(width):
        tiles[0][x] = BLOCKED
        tiles[height - 1][x] = BLOCKED
    for y in range(height):
        tiles[y][0] = BLOCKED
        tiles[y][width - 1] = BLOCKED


def fill_rect(tiles: list[list[int]], rect: tuple[int, int, int, int], value: int) -> None:
    x0, y0, x1, y1 = rect
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if 0 <= y < len(tiles) and 0 <= x < len(tiles[0]):
                tiles[y][x] = value


def write_chunks(tiles: list[list[int]]) -> None:
    for cy in range(0, WORLD_H, CHUNK_SIZE):
        for cx in range(0, WORLD_W, CHUNK_SIZE):
            chunk = [row[cx : cx + CHUNK_SIZE] for row in tiles[cy : cy + CHUNK_SIZE]]
            data = {
                "x": cx // CHUNK_SIZE,
                "y": cy // CHUNK_SIZE,
                "size": CHUNK_SIZE,
                "tiles": chunk,
            }
            (CHUNKS / f"{cx // CHUNK_SIZE}_{cy // CHUNK_SIZE}.json").write_text(
                json.dumps(data, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )


def ellipse(x: int, y: int, cx: int, cy: int, rx: int, ry: int) -> float:
    return ((x - cx) ** 2) / (rx * rx) + ((y - cy) ** 2) / (ry * ry)


def noise(x: int, y: int) -> int:
    return ((x * 73856093) ^ (y * 19349663) ^ 1729) & 0xFFFF


def pretty(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
