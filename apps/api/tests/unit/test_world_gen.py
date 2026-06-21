"""Unit tests for the Wave-3 agent-generated world (Agent 7).

Scope: prove the "thin harness / fat skill" level generator is BULLETPROOF — it
turns a valid model response into a ``WorldSpecLite``, returns ``None`` on any
failure (so the route falls back), caches by seed (no re-invocation), is gated by
the additive ``world_gen_enabled`` flag, and can NEVER make ``get_world`` raise.

No live model server and no DB: ``gateway.complete`` is monkeypatched to canned
strings, and the route is driven with a tiny fake async session that returns a
``Run`` for ``session.get(Run, id)``. Pure unit tests; collect + run on a bare host.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.schemas import WorldSpecLite
from app.world import generator as gen


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

WIDTH = 16
HEIGHT = 16


def _valid_world_json(seed: int = 7, width: int = WIDTH, height: int = HEIGHT) -> str:
    """A well-formed model response matching the WorldSpecLite contract."""
    start = {"kind": "start", "x": 1, "y": 1, "name": "Trailhead"}
    goal = {"kind": "goal", "x": width - 2, "y": height - 2, "name": "The Summit"}
    return json.dumps(
        {
            "seed": seed,
            "width": width,
            "height": height,
            "regions": [
                {"name": "Ashfen Mire", "biome": "wetland", "bounds": [0, 0, 7, 7]},
                {"name": "Pale Highlands", "biome": "mountains", "bounds": [8, 8, 15, 15]},
            ],
            "pois": [
                start,
                goal,
                {"kind": "camp", "x": 3, "y": 4, "name": "Old Camp"},
                {"kind": "town", "x": 6, "y": 9, "name": "Millbrook"},
                {"kind": "den", "x": 11, "y": 5, "name": "Gloomden"},
                {"kind": "landmark", "x": 8, "y": 2, "name": "Standing Stones"},
            ],
            "start": start,
            "goal": goal,
        }
    )


def _patch_complete(monkeypatch: pytest.MonkeyPatch, return_value: str) -> dict[str, int]:
    """Patch the gateway.complete the generator imported; count invocations."""
    counter = {"calls": 0}

    async def fake_complete(*_args: Any, **_kwargs: Any) -> str:
        counter["calls"] += 1
        return return_value

    monkeypatch.setattr(gen.gateway, "complete", fake_complete)
    return counter


def _patch_complete_raises(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Patch gateway.complete to raise, simulating a gateway/network failure."""
    counter = {"calls": 0}

    async def boom(*_args: Any, **_kwargs: Any) -> str:
        counter["calls"] += 1
        raise RuntimeError("ollama is down")

    monkeypatch.setattr(gen.gateway, "complete", boom)
    return counter


@pytest.fixture(autouse=True)
def _clear_world_cache() -> None:
    """Each test starts with an empty world cache so cases don't bleed."""
    gen._clear_cache()
    yield
    gen._clear_cache()


# --------------------------------------------------------------------------- #
# generate_world: happy path -> parsed WorldSpecLite
# --------------------------------------------------------------------------- #


async def test_valid_json_response_parses_to_world_spec_lite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_complete(monkeypatch, _valid_world_json(seed=7))

    spec = await gen.generate_world(7, WIDTH, HEIGHT)

    assert isinstance(spec, WorldSpecLite)
    # Seed / dims are authoritative from the request.
    assert spec.seed == 7
    assert spec.width == WIDTH
    assert spec.height == HEIGHT
    # Structure round-tripped: regions, pois, and a start + goal.
    assert spec.start is not None and spec.start.kind == "start"
    assert spec.goal is not None and spec.goal.kind == "goal"
    kinds = {p.kind for p in spec.pois}
    assert {"start", "goal", "camp", "town", "den", "landmark"} <= kinds


async def test_seed_and_dims_overwrite_model_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Model echoes the WRONG seed/dims; the harness must pin the request's values
    # so the world stays consistent + seed-deterministic.
    _patch_complete(monkeypatch, _valid_world_json(seed=999, width=4, height=4))

    spec = await gen.generate_world(42, WIDTH, HEIGHT)

    assert spec is not None
    assert spec.seed == 42
    assert spec.width == WIDTH
    assert spec.height == HEIGHT


# --------------------------------------------------------------------------- #
# generate_world: failure modes -> None (never raise)
# --------------------------------------------------------------------------- #


async def test_malformed_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pure garbage that json_repair cannot turn into a dict.
    _patch_complete(monkeypatch, "not json at all <<< ???")

    spec = await gen.generate_world(1, WIDTH, HEIGHT)

    assert spec is None


async def test_empty_response_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_complete(monkeypatch, "")
    assert await gen.generate_world(2, WIDTH, HEIGHT) is None


async def test_validation_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid JSON, but a POI.kind outside the frozen Literal -> validation fails.
    bad = json.dumps(
        {
            "regions": [],
            "pois": [{"kind": "volcano", "x": 1, "y": 1, "name": "Nope"}],
        }
    )
    _patch_complete(monkeypatch, bad)

    spec = await gen.generate_world(3, WIDTH, HEIGHT)

    assert spec is None


async def test_non_dict_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # A JSON array is valid JSON but not a WorldSpecLite object.
    _patch_complete(monkeypatch, "[1, 2, 3]")
    assert await gen.generate_world(4, WIDTH, HEIGHT) is None


async def test_gateway_error_returns_none_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _patch_complete_raises(monkeypatch)

    # Must swallow the gateway exception and signal fallback via None.
    spec = await gen.generate_world(5, WIDTH, HEIGHT)

    assert spec is None
    assert counter["calls"] == 1


# --------------------------------------------------------------------------- #
# generate_world: caching — same seed does not re-invoke the gateway
# --------------------------------------------------------------------------- #


async def test_cache_hit_does_not_reinvoke_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _patch_complete(monkeypatch, _valid_world_json(seed=7))

    first = await gen.generate_world(7, WIDTH, HEIGHT)
    second = await gen.generate_world(7, WIDTH, HEIGHT)

    assert first is not None and second is not None
    # Same seed -> same world object, generated exactly once.
    assert second is first
    assert counter["calls"] == 1


async def test_failure_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failing call returns None and must NOT poison the cache: a later good
    # response should still be able to populate it.
    counter = _patch_complete(monkeypatch, "garbage")
    assert await gen.generate_world(8, WIDTH, HEIGHT) is None
    assert counter["calls"] == 1

    good = _patch_complete(monkeypatch, _valid_world_json(seed=8))
    spec = await gen.generate_world(8, WIDTH, HEIGHT)
    assert spec is not None
    assert good["calls"] == 1


# --------------------------------------------------------------------------- #
# Route integration: flag-gating + total fallback (no DB, fake session)
# --------------------------------------------------------------------------- #


class _FakeSession:
    """Minimal async session: returns a preset object for session.get(...)."""

    def __init__(self, run: Any) -> None:
        self._run = run

    async def get(self, _model: Any, _ident: Any) -> Any:
        return self._run


def _make_run(seed: int):
    from app.db.models import Run, RunStatus

    return Run(
        debate_topic="t",
        seed=seed,
        player_x=1,
        player_y=1,
        status=RunStatus.active,
    )


async def test_route_flag_off_is_identical_to_procedural(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import world as world_router

    # Flag OFF (default): the generator must not even be consulted.
    monkeypatch.setattr(world_router.settings, "world_gen_enabled", False)

    called = {"n": 0}

    async def tripwire(*_a: Any, **_k: Any):
        called["n"] += 1
        return None

    # Patch on the generator module so an accidental call would be detected.
    monkeypatch.setattr(gen, "generate_world", tripwire)

    run = _make_run(seed=123)
    out = await world_router.get_world("run-id", _FakeSession(run))

    # Identical to the pure procedural world for this seed.
    from app.routers.map import MAP_HEIGHT, MAP_WIDTH, _generate_tiles

    tiles = _generate_tiles(123)
    expected = world_router.build_world(123, tiles, MAP_WIDTH, MAP_HEIGHT)
    assert out.model_dump() == expected.model_dump()
    assert called["n"] == 0  # generator never invoked when flag is off


async def test_route_flag_on_falls_back_when_generator_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import world as world_router

    monkeypatch.setattr(world_router.settings, "world_gen_enabled", True)

    async def returns_none(*_a: Any, **_k: Any):
        return None

    monkeypatch.setattr("app.world.generator.generate_world", returns_none)

    run = _make_run(seed=55)
    out = await world_router.get_world("run-id", _FakeSession(run))

    # Falls back to the deterministic procedural world — no 500.
    from app.routers.map import MAP_HEIGHT, MAP_WIDTH, _generate_tiles

    tiles = _generate_tiles(55)
    expected = world_router.build_world(55, tiles, MAP_WIDTH, MAP_HEIGHT)
    assert out.model_dump() == expected.model_dump()


async def test_route_never_raises_when_generator_throws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import world as world_router

    monkeypatch.setattr(world_router.settings, "world_gen_enabled", True)

    async def explodes(*_a: Any, **_k: Any):
        raise RuntimeError("generator blew up unexpectedly")

    monkeypatch.setattr("app.world.generator.generate_world", explodes)

    run = _make_run(seed=77)
    # The headline guarantee: get_world must NEVER propagate the generator error.
    out = await world_router.get_world("run-id", _FakeSession(run))

    assert isinstance(out, WorldSpecLite)
    assert out.seed == 77


async def test_route_flag_on_uses_generated_world_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import world as world_router

    monkeypatch.setattr(world_router.settings, "world_gen_enabled", True)
    _patch_complete(monkeypatch, _valid_world_json(seed=88))

    run = _make_run(seed=88)
    out = await world_router.get_world("run-id", _FakeSession(run))

    assert isinstance(out, WorldSpecLite)
    assert out.seed == 88
    # The generated world carries our distinctive region name, proving it was used.
    assert any(r.name == "Ashfen Mire" for r in out.regions)
