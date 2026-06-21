"""Watabou JSON → WorldSpecLite + tile grid importer tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.world.algorithms.base import BLOCKED, DOOR, FEATURE, WALKABLE
from app.world.watabou_import import import_watabou, import_watabou_data

FIXTURES = Path(__file__).parent.parent / "fixtures" / "watabou"


def test_dungeon_rasterizes_rooms_to_floor():
    """Watabou dungeon rects carve FLOOR out of an initially solid grid."""
    imp = import_watabou(FIXTURES / "dungeon_min.json", width=20, height=18)

    assert imp.spec.width == 20 and imp.spec.height == 18
    floor = sum(row.count(WALKABLE) for row in imp.tiles)
    wall = sum(row.count(BLOCKED) for row in imp.tiles)
    assert floor > 0, "dungeon should carve some floor"
    assert wall > floor, "wall area should still dominate a small dungeon"


def test_dungeon_has_start_and_goal_pois():
    """First rect → start POI, last rect → goal POI; middle rect gets FEATURE."""
    imp = import_watabou(FIXTURES / "dungeon_min.json", width=20, height=18)
    kinds = [p.kind for p in imp.spec.pois]
    assert "start" in kinds, f"expected a start POI in {kinds}"
    assert "goal" in kinds, f"expected a goal POI in {kinds}"
    assert imp.spec.start is not None and imp.spec.start.kind == "start"
    assert imp.spec.goal is not None and imp.spec.goal.kind == "goal"
    assert "drystone" in imp.spec.regions[0].name.lower()


def test_dungeon_doors_become_DOOR_tiles():
    """Each Watabou door rasterizes to a DOOR tile (3)."""
    imp = import_watabou(FIXTURES / "dungeon_min.json", width=20, height=18)
    door_count = sum(row.count(DOOR) for row in imp.tiles)
    assert door_count >= 1, "should have at least one DOOR tile from rasterized doors"


def test_dungeon_has_boss_feature_anchor():
    """Three+ rects → middle rect gets a FEATURE anchor (boss-room landmark)."""
    imp = import_watabou(FIXTURES / "dungeon_min.json", width=20, height=18)
    feature_count = sum(row.count(FEATURE) for row in imp.tiles)
    assert feature_count >= 1, "expected a boss-anchor FEATURE in a multi-room dungeon"
    boss_pois = [p for p in imp.spec.pois if "throne" in p.name.lower()]
    assert len(boss_pois) == 1, "expected exactly one Throne of Echoes landmark"


def test_town_geojson_rasterizes_buildings_to_walls():
    """Town GeoJSON polygons become BLOCKED rectangles inside an open plaza."""
    imp = import_watabou(FIXTURES / "town_min.json", width=24, height=20)

    floor = sum(row.count(WALKABLE) for row in imp.tiles)
    wall = sum(row.count(BLOCKED) for row in imp.tiles)
    assert floor > wall, "town should be mostly walkable plaza"
    assert wall > 0, "buildings should produce some wall tiles"


def test_town_has_door_and_building_anchors():
    """Town gets a gate DOOR + one FEATURE anchor per rasterized building."""
    imp = import_watabou(FIXTURES / "town_min.json", width=24, height=20)

    door_count = sum(row.count(DOOR) for row in imp.tiles)
    feature_count = sum(row.count(FEATURE) for row in imp.tiles)
    assert door_count == 1, f"expected a single town gate, got {door_count}"
    assert feature_count >= 1, f"expected ≥1 building anchor, got {feature_count}"

    pois = imp.spec.pois
    assert pois[0].kind == "start" and "gate" in pois[0].name.lower()
    town_anchors = [p for p in pois if p.kind == "town"]
    assert len(town_anchors) >= 1, "expected town anchors for buildings"


def test_importer_is_pure_and_deterministic():
    """Same data → byte-identical spec + tiles (no wall-clock, no unseeded RNG)."""
    data = json.loads((FIXTURES / "dungeon_min.json").read_text())
    a = import_watabou_data(data, 20, 18, name="x")
    b = import_watabou_data(data, 20, 18, name="x")
    assert a.spec.model_dump() == b.spec.model_dump()
    assert a.tiles == b.tiles


def test_importer_rejects_unknown_shape():
    """An unrecognised JSON shape raises ValueError, not silently None."""
    with pytest.raises(ValueError):
        import_watabou_data({"weird": "shape"}, 20, 18, name="x")


def test_importer_handles_alias_field_names():
    """Watabou rects with 'width'/'height' (instead of 'w'/'h') still parse."""
    data = {
        "rects": [
            {"x": 0, "y": 0, "width": 4, "height": 3},
            {"x": 6, "y": 0, "width": 3, "height": 3},
        ],
        "doors": [{"x": 4, "y": 1}],
    }
    imp = import_watabou_data(data, 16, 12, name="alias_test")
    floor = sum(row.count(WALKABLE) for row in imp.tiles)
    assert floor > 0, "alias field names should produce floor tiles"
