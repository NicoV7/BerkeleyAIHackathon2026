"""Determinism tests for procedural generation OUTPUT (Track B, Wave 2).

Scope (per the design doc): the WORLD LAYOUT from a seed must be byte-for-byte
stable — overworld tiles, POIs, biome regions, and interiors. This is what makes
the game replayable.

EXPLICITLY OUT OF SCOPE: runtime enemy positions. The real-time enemy FSM
(EnemyAI.update(delta) on the client) is intentionally NON-deterministic and is
NOT covered by this guarantee — only the seed-derived layout is.

No DB, no model server: every function under test is a pure function of its args.
"""
from __future__ import annotations

import pytest

from app.routers import world as W
from app.routers.map import MAP_HEIGHT, MAP_WIDTH, _generate_tiles
from app.schemas import POI
from app.world.algorithms import INTERIOR_KINDS, get_generator
from app.world.algorithms.biomes import BiomeGenerator

SEEDS = [0, 1, 7, 42, 123, 2024]


@pytest.fixture(autouse=True)
def _clear_interior_cache() -> None:
    """Each test starts with a clean interior cache so cache + recompute agree."""
    W._clear_interior_cache()
    yield
    W._clear_interior_cache()


# --------------------------------------------------------------------------- #
# Overworld: tiles + POIs + regions are stable per seed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", SEEDS)
def test_overworld_world_is_byte_stable(seed: int) -> None:
    tiles_a = _generate_tiles(seed)
    tiles_b = _generate_tiles(seed)
    a = W.build_world(seed, tiles_a, MAP_WIDTH, MAP_HEIGHT)
    b = W.build_world(seed, tiles_b, MAP_WIDTH, MAP_HEIGHT)
    assert a.model_dump() == b.model_dump()


@pytest.mark.parametrize("seed", SEEDS)
def test_place_pois_stable(seed: int) -> None:
    tiles = _generate_tiles(seed)
    p1 = W.place_pois(seed, tiles, MAP_WIDTH, MAP_HEIGHT)
    p2 = W.place_pois(seed, tiles, MAP_WIDTH, MAP_HEIGHT)
    assert [poi.model_dump() for poi in p1] == [poi.model_dump() for poi in p2]


def test_different_seeds_diverge() -> None:
    """Sanity: the generator actually varies with the seed (not a constant)."""
    a = W.build_world(1, _generate_tiles(1), MAP_WIDTH, MAP_HEIGHT)
    b = W.build_world(2, _generate_tiles(2), MAP_WIDTH, MAP_HEIGHT)
    assert a.model_dump() != b.model_dump()


# --------------------------------------------------------------------------- #
# Biome layer (the REAL generator) is deterministic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", SEEDS)
def test_biome_map_stable(seed: int) -> None:
    gen = BiomeGenerator()
    m1 = gen.biome_map(seed, MAP_WIDTH, MAP_HEIGHT)
    m2 = gen.biome_map(seed, MAP_WIDTH, MAP_HEIGHT)
    assert m1 == m2


def test_biome_map_has_contiguous_variety() -> None:
    """The noise field should yield >1 biome (not a flat single-biome map) for a
    typical seed — proves the noise is actually shaping the world."""
    gen = BiomeGenerator()
    flat = {b for row in gen.biome_map(7, MAP_WIDTH, MAP_HEIGHT) for b in row}
    assert len(flat) >= 2


@pytest.mark.parametrize("seed", SEEDS)
def test_regions_stable_and_bounded(seed: int) -> None:
    r1 = W.build_regions(seed, MAP_WIDTH, MAP_HEIGHT)
    r2 = W.build_regions(seed, MAP_WIDTH, MAP_HEIGHT)
    assert [r.model_dump() for r in r1] == [r.model_dump() for r in r2]
    # Region bounds stay inside the grid.
    for r in r1:
        assert r.bounds is not None
        x0, y0, x1, y1 = r.bounds
        assert 0 <= x0 <= x1 <= MAP_WIDTH - 1
        assert 0 <= y0 <= y1 <= MAP_HEIGHT - 1


# --------------------------------------------------------------------------- #
# Interiors: same (interior_seed, kind) -> identical interior; cache agrees
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", INTERIOR_KINDS)
@pytest.mark.parametrize("seed", SEEDS)
def test_interior_generator_is_pure(kind: str, seed: int) -> None:
    gen = get_generator(kind)
    a = gen.generate(seed, W.INTERIOR_WIDTH, W.INTERIOR_HEIGHT)
    b = gen.generate(seed, W.INTERIOR_WIDTH, W.INTERIOR_HEIGHT)
    assert a.tiles == b.tiles
    assert [p.model_dump() for p in a.pois] == [p.model_dump() for p in b.pois]
    # Dimensions are respected.
    assert a.width == W.INTERIOR_WIDTH
    assert a.height == W.INTERIOR_HEIGHT
    assert len(a.tiles) == W.INTERIOR_HEIGHT
    assert all(len(row) == W.INTERIOR_WIDTH for row in a.tiles)


@pytest.mark.parametrize("kind", INTERIOR_KINDS)
def test_build_interior_cache_matches_recompute(kind: str) -> None:
    seed = 12345
    first = W.build_interior(seed, kind)  # generates + caches
    second = W.build_interior(seed, kind)  # cache hit -> identical object
    assert second is first
    # And the cached value equals a fresh recompute after a cache clear.
    W._clear_interior_cache()
    fresh = W.build_interior(seed, kind)
    assert fresh.model_dump() == first.model_dump()


def test_build_interior_clamps_unknown_kind() -> None:
    """An unknown interior_kind hint must not raise — it clamps to a generator."""
    spec = W.build_interior(99, "not-a-real-kind")
    assert spec.width == W.INTERIOR_WIDTH
    assert spec.height == W.INTERIOR_HEIGHT


# --------------------------------------------------------------------------- #
# Enterable POIs carry stable interior_seed/kind; non-enterable do not
# --------------------------------------------------------------------------- #


def test_enterable_pois_get_interior_fields() -> None:
    seed = 42
    world = W.build_world(seed, _generate_tiles(seed), MAP_WIDTH, MAP_HEIGHT)
    by_kind: dict[str, list[POI]] = {}
    for p in world.pois:
        by_kind.setdefault(p.kind, []).append(p)

    for p in by_kind.get("town", []) + by_kind.get("den", []):
        assert p.interior_seed is not None
        assert p.interior_kind in INTERIOR_KINDS
    # Non-enterable kinds stay additive-default (None).
    for p in by_kind.get("camp", []) + by_kind.get("landmark", []):
        assert p.interior_seed is None
        assert p.interior_kind is None


def test_interior_seed_is_stable_across_builds() -> None:
    seed = 7
    w1 = W.build_world(seed, _generate_tiles(seed), MAP_WIDTH, MAP_HEIGHT)
    w2 = W.build_world(seed, _generate_tiles(seed), MAP_WIDTH, MAP_HEIGHT)
    ids1 = {W.poi_id(p): p.interior_seed for p in w1.pois}
    ids2 = {W.poi_id(p): p.interior_seed for p in w2.pois}
    assert ids1 == ids2
