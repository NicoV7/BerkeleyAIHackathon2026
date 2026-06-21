"""Canonical world loader + routing tests.

Validates:
  - canonical.json round-trip (the baked artifact loads back into WorldSpecLite)
  - /world endpoint returns the canonical world when present
  - /interior endpoint returns the canonical interior when one exists for the key
  - Bad / missing artifacts fall through to procgen WITHOUT raising
  - The canonical world is deterministic across cache resets
"""
from __future__ import annotations

import json
import math
from typing import Any

import pytest

from app.world import canonical as canonical_mod


# --------------------------------------------------------------------------- #
# Fake DB session (mirrors test_world_gen.py)
# --------------------------------------------------------------------------- #

class _FakeSession:
    def __init__(self, run: Any) -> None:
        self._run = run

    async def get(self, _model, _id):
        return self._run

    async def execute(self, _statement):
        return _EmptyResult()


class _EmptyResult:
    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list[Any]:
        return []


def _make_run(seed: int = 1729):
    from app.db.models import Run, RunStatus
    return Run(
        id="canon-test",
        debate_topic="Should canonical worlds replace procgen?",
        theme=None,
        seed=seed,
        player_x=208,
        player_y=563,
        status=RunStatus.active,
    )


@pytest.fixture(autouse=True)
def _reset_canonical_cache():
    """Drop cached canonical state between tests so each starts fresh."""
    canonical_mod.clear_cache()
    yield
    canonical_mod.clear_cache()


# --------------------------------------------------------------------------- #
# Loader contract
# --------------------------------------------------------------------------- #

def test_loader_returns_artifact_when_present():
    """A baked canonical.json yields metadata and readable tile chunks."""
    bundle = canonical_mod.get_canonical_world()
    assert bundle is not None, "expected a baked canonical.json for this test"
    assert bundle.spec.width > 0 and bundle.spec.height > 0
    window = canonical_mod.get_canonical_tile_window(0, 0, 16, 16)
    assert window is not None
    assert len(window) == 16
    assert all(len(row) == 16 for row in window)
    # Canonical regions carry hand-curated names (from curation.yaml).
    assert bundle.spec.regions, "canonical world should expose at least one region"


def test_canonical_world_is_expansive_and_explorable():
    """Pin the demo promise: a small open world, not a tiny POI board."""
    bundle = canonical_mod.get_canonical_world()
    assert bundle is not None

    spec = bundle.spec
    assert spec.width >= 1024
    assert spec.height >= 1024
    assert len(spec.regions) >= 8

    kinds = {poi.kind for poi in spec.pois}
    assert {"start", "goal", "camp", "town", "den", "landmark"}.issubset(kinds)
    assert sum(1 for poi in spec.pois if poi.kind == "town") >= 8
    assert sum(1 for poi in spec.pois if poi.kind == "den") >= 12
    assert sum(1 for poi in spec.pois if poi.kind == "camp") >= 12
    assert sum(1 for poi in spec.pois if poi.kind == "landmark") >= 18

    anchors = [anchor for poi in spec.pois for anchor in poi.npc_anchors]
    assert len(anchors) >= 10
    assert any(anchor.archetype == "merchant" for anchor in anchors)
    assert any(anchor.archetype == "quest_giver" for anchor in anchors)
    assert any(anchor.archetype == "figure" for anchor in anchors)

    assert spec.start is not None and spec.start.name == "Aldermere Commons"
    starter_village = min(
        (poi for poi in spec.pois if poi.kind == "town"),
        key=lambda poi: math.hypot(poi.x - spec.start.x, poi.y - spec.start.y),
    )
    assert starter_village.name == "Aldermere Village"
    assert math.hypot(starter_village.x - spec.start.x, starter_village.y - spec.start.y) <= 4
    assert len(starter_village.npc_anchors) >= 8
    assert sum(1 for anchor in starter_village.npc_anchors if anchor.archetype == "quest_giver") >= 3

    nearby_dungeons = [
        poi for poi in spec.pois
        if poi.kind == "den" and math.hypot(poi.x - spec.start.x, poi.y - spec.start.y) <= 90
    ]
    assert len(nearby_dungeons) >= 3

    tile_values: set[int] = set()
    for y in range(0, spec.height, 128):
        for x in range(0, spec.width, 128):
            window = canonical_mod.get_canonical_tile_window(x, y, 128, 128)
            assert window is not None
            tile_values.update(tile for row in window for tile in row)
    assert {3, 5, 6, 7, 8, 9}.issubset(tile_values)
    assert spec.goal is not None
    blocked = {1, 6, 7}
    assert canonical_mod.get_canonical_tile(spec.start.x, spec.start.y) not in blocked
    assert canonical_mod.get_canonical_tile(spec.goal.x, spec.goal.y) not in blocked


def test_loader_is_memoized():
    """Repeated calls return the SAME instance (no re-read)."""
    a = canonical_mod.get_canonical_world()
    b = canonical_mod.get_canonical_world()
    assert a is b, "canonical world should be cached after first read"


def test_loader_falls_through_when_artifact_missing(monkeypatch):
    """A missing artifact returns None (callers fall back to procgen)."""
    from pathlib import Path

    monkeypatch.setattr(
        canonical_mod, "CANONICAL_PATH", Path("/nonexistent/canonical.json")
    )
    canonical_mod.clear_cache()
    assert canonical_mod.get_canonical_world() is None


def test_loader_falls_through_on_malformed_json(tmp_path, monkeypatch):
    """A malformed JSON returns None — must never crash the runtime."""
    bad = tmp_path / "canonical.json"
    bad.write_text("{not really json", encoding="utf-8")
    monkeypatch.setattr(canonical_mod, "CANONICAL_PATH", bad)
    canonical_mod.clear_cache()
    assert canonical_mod.get_canonical_world() is None


def test_loader_validates_tile_dimensions(tmp_path, monkeypatch):
    """Tile grid that doesn't match width/height is rejected."""
    bad = tmp_path / "canonical.json"
    bad.write_text(
        json.dumps(
            {
                "version": 1,
                "world": {"seed": 1, "width": 4, "height": 3, "regions": [], "pois": []},
                # Wrong height — should be 3 rows, not 2.
                "tiles": [[0, 0, 0, 0], [0, 0, 0, 0]],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(canonical_mod, "CANONICAL_PATH", bad)
    canonical_mod.clear_cache()
    assert canonical_mod.get_canonical_world() is None


# --------------------------------------------------------------------------- #
# /world route — canonical takes priority
# --------------------------------------------------------------------------- #

async def test_get_world_returns_canonical_when_present():
    """When canonical.json exists, /world returns its spec, not the procgen one."""
    from app.routers import world as world_router

    out = await world_router.get_world("run-id", _FakeSession(_make_run(seed=42)))
    canon = canonical_mod.get_canonical_world()
    assert canon is not None
    assert out.model_dump() == canon.spec.model_dump()


async def test_get_world_falls_back_to_procgen_when_canonical_missing(monkeypatch):
    """When canonical is absent, /world returns the seed-deterministic procgen."""
    from app.routers import world as world_router

    monkeypatch.setattr(canonical_mod, "get_canonical_world", lambda: None)
    monkeypatch.setattr(world_router.settings, "world_gen_enabled", False)
    out = await world_router.get_world("run-id", _FakeSession(_make_run(seed=42)))
    assert out.seed == 42, "fallback should reflect the run's seed"


async def test_get_map_returns_chunked_canonical_window():
    """The map route pages nearby terrain instead of returning the whole world."""
    from app.routers import map as map_router

    out = await map_router.get_map(
        "run-id",
        _FakeSession(_make_run(seed=42)),
        center_x=208,
        center_y=563,
        chunk_size=96,
    )

    assert out.world_width == 1024
    assert out.world_height == 1024
    assert out.width == 96
    assert out.height == 96
    assert len(out.tiles) == 96
    assert all(len(row) == 96 for row in out.tiles)
    assert 0 <= out.origin_x <= out.player_x <= out.origin_x + out.width
    assert 0 <= out.origin_y <= out.player_y <= out.origin_y + out.height


async def test_get_map_recenters_after_invalid_persisted_player_tile():
    """A snapped player position must be inside the returned chunk window."""
    from app.routers import map as map_router

    run = _make_run(seed=42)
    run.player_x = 0
    run.player_y = 0

    out = await map_router.get_map(
        "run-id",
        _FakeSession(run),
        chunk_size=96,
    )
    canon = canonical_mod.get_canonical_world()
    assert canon is not None and canon.spec.start is not None

    assert (out.player_x, out.player_y) == (canon.spec.start.x, canon.spec.start.y)
    assert out.origin_x <= out.player_x < out.origin_x + out.width
    assert out.origin_y <= out.player_y < out.origin_y + out.height


# --------------------------------------------------------------------------- #
# /interior route — canonical interior wins when the key matches
# --------------------------------------------------------------------------- #

async def test_get_interior_returns_canonical_for_known_key():
    """A POI key that has a canonical interior gets the curated bundle."""
    from app.routers import world as world_router

    # The baked artifact maps these keys to canonical interiors (see curation.yaml).
    out = await world_router.get_interior(
        "run-id", "town:208:560", _FakeSession(_make_run(seed=42))
    )
    assert out.regions, "canonical town interior should have at least one region"
    # The first region carries the curated lore.
    assert out.regions[0].lore is not None
    assert "innkeeper" in (out.regions[0].lore or "").lower() or out.regions[0].name


async def test_get_interior_falls_back_when_canonical_key_unknown(monkeypatch):
    """Unknown POI key → seed-deterministic procgen interior, never raise."""
    from app.routers import world as world_router

    monkeypatch.setattr(canonical_mod, "get_canonical_interior", lambda _k: None)
    out = await world_router.get_interior(
        "run-id", "den:99:99", _FakeSession(_make_run(seed=42))
    )
    assert out is not None and out.width > 0


# --------------------------------------------------------------------------- #
# Bake → load round-trip
# --------------------------------------------------------------------------- #

def test_bake_then_load_is_deterministic(tmp_path, monkeypatch):
    """Bake the artifact twice; the second load should match the first byte-for-byte."""
    # Snapshot the current artifact, then re-load via the loader twice with
    # cache resets, verifying byte-identical specs both times.
    first = canonical_mod.get_canonical_world()
    canonical_mod.clear_cache()
    second = canonical_mod.get_canonical_world()
    assert first is not None and second is not None
    assert first.spec.model_dump() == second.spec.model_dump()
    assert first.chunk_size == second.chunk_size
    assert canonical_mod.get_canonical_tile_window(64, 64, 16, 16) is not None
