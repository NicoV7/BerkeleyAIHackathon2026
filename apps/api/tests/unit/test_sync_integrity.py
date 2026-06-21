"""Unit tests — Track B position-sync integrity (POST /api/runs/{id}/sync).

Movement is now client-authoritative; the server's only remaining integrity
gate lives in the /sync path. These tests pin that contract with NO live DB
(pure helpers + a fake async session):

  * CLAMP an out-of-bounds absolute position into the map grid.
  * REJECT a blocked landing tile (tiles[y][x] == 1) — snap back to the stored
    position instead of corrupting it.
  * DROP a stale / out-of-order sync (incoming seq <= last accepted seq) so a
    refresh/reconnect race can't roll a newer position back to an older one.

Seed-agnostic: blocked / walkable tiles are discovered at runtime from
``_generate_tiles`` so the tests don't bake in a particular grid layout.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

# Import-time touches only pure Python + frozen schemas/models; skip the whole
# file (rather than erroring collection) if the sync impl hasn't landed.
map_router = pytest.importorskip("app.routers.map")

from app.routers.map import (  # noqa: E402
    MAP_HEIGHT,
    MAP_WIDTH,
    _LAST_SYNC_SEQ,
    SyncPositionRequest,
    _generate_tiles,
    sync_position,
)


# --------------------------------------------------------------------------- #
# Fake async session (only get/commit are exercised by /sync)
# --------------------------------------------------------------------------- #
class _FakeSession:
    """Minimal async session double for the /sync endpoint.

    - get(Run, id) -> the seeded run (or None when ids mismatch)
    - add(row)     -> no-op (the run is mutated in place by the endpoint)
    - commit()     -> records that a write happened
    """

    def __init__(self, run: Any) -> None:
        self._run = run
        self.committed = False

    async def get(self, model: Any, ident: str) -> Any:
        return self._run if (self._run and ident == self._run.id) else None

    def add(self, row: Any) -> None:  # sync, like SQLAlchemy's Session.add
        pass

    async def commit(self) -> None:
        self.committed = True


# --------------------------------------------------------------------------- #
# Tile helpers — find a walkable / blocked tile for a given seed at runtime
# --------------------------------------------------------------------------- #
def _first_walkable(seed: int) -> tuple[int, int]:
    tiles = _generate_tiles(seed)
    for y in range(MAP_HEIGHT):
        for x in range(MAP_WIDTH):
            if tiles[y][x] == 0:
                return x, y
    raise AssertionError("no walkable tile for seed")


def _first_blocked(seed: int) -> tuple[int, int]:
    tiles = _generate_tiles(seed)
    for y in range(MAP_HEIGHT):
        for x in range(MAP_WIDTH):
            if tiles[y][x] == 1:
                return x, y
    raise AssertionError("no blocked tile for seed")


@pytest.fixture(autouse=True)
def _clear_seq_state():
    """Each test starts with a clean sync-seq high-water-mark table."""
    _LAST_SYNC_SEQ.clear()
    yield
    _LAST_SYNC_SEQ.clear()


# --------------------------------------------------------------------------- #
# (a) CLAMP out-of-bounds
# --------------------------------------------------------------------------- #
def test_sync_clamps_out_of_bounds(make_run) -> None:
    run = make_run(seed=5, player_x=1, player_y=1)
    session = _FakeSession(run)

    # Way past the bottom-right corner — must clamp into [0, W-1] x [0, H-1].
    body = SyncPositionRequest(x=9999, y=9999, seq=1)
    result = asyncio.run(sync_position(run.id, body, session))  # type: ignore[arg-type]

    assert 0 <= result.player_x <= MAP_WIDTH - 1
    assert 0 <= result.player_y <= MAP_HEIGHT - 1
    # The clamped corner may itself be blocked; either way it never escapes bounds.


def test_sync_clamps_negative_into_bounds(make_run) -> None:
    run = make_run(seed=5, player_x=2, player_y=2)
    session = _FakeSession(run)

    body = SyncPositionRequest(x=-50, y=-50, seq=1)
    result = asyncio.run(sync_position(run.id, body, session))  # type: ignore[arg-type]

    assert 0 <= result.player_x <= MAP_WIDTH - 1
    assert 0 <= result.player_y <= MAP_HEIGHT - 1


# --------------------------------------------------------------------------- #
# (b) REJECT a blocked landing tile
# --------------------------------------------------------------------------- #
def test_sync_rejects_blocked_tile(make_run) -> None:
    seed = 5
    bx, by = _first_blocked(seed)
    run = make_run(seed=seed, player_x=1, player_y=1)
    session = _FakeSession(run)

    body = SyncPositionRequest(x=bx, y=by, seq=1)
    result = asyncio.run(sync_position(run.id, body, session))  # type: ignore[arg-type]

    # Snap back to the stored position; the blocked tile is never persisted.
    assert (result.player_x, result.player_y) == (1, 1)
    assert run.player_x == 1 and run.player_y == 1
    assert session.committed is False


def test_sync_accepts_walkable_tile(make_run) -> None:
    seed = 5
    wx, wy = _first_walkable(seed)
    run = make_run(seed=seed, player_x=1, player_y=1)
    session = _FakeSession(run)

    body = SyncPositionRequest(x=wx, y=wy, seq=1)
    result = asyncio.run(sync_position(run.id, body, session))  # type: ignore[arg-type]

    assert (result.player_x, result.player_y) == (wx, wy)
    assert run.player_x == wx and run.player_y == wy
    assert session.committed is True


# --------------------------------------------------------------------------- #
# (c) DROP stale / out-of-order sequence numbers
# --------------------------------------------------------------------------- #
def test_sync_drops_stale_seq(make_run) -> None:
    seed = 5
    tiles = _generate_tiles(seed)
    # Two distinct walkable tiles so a successful move is observable.
    walk = [
        (x, y)
        for y in range(MAP_HEIGHT)
        for x in range(MAP_WIDTH)
        if tiles[y][x] == 0
    ]
    (ax, ay), (bx, by) = walk[0], walk[1]

    run = make_run(seed=seed, player_x=1, player_y=1)
    session = _FakeSession(run)

    # seq=5 lands at A.
    r1 = asyncio.run(
        sync_position(run.id, SyncPositionRequest(x=ax, y=ay, seq=5), session)  # type: ignore[arg-type]
    )
    assert (r1.player_x, r1.player_y) == (ax, ay)
    assert r1.stale is False

    # seq=3 (older) arrives late targeting B — must be DROPPED, position holds.
    r2 = asyncio.run(
        sync_position(run.id, SyncPositionRequest(x=bx, y=by, seq=3), session)  # type: ignore[arg-type]
    )
    assert r2.stale is True
    assert (r2.player_x, r2.player_y) == (ax, ay)
    assert run.player_x == ax and run.player_y == ay


def test_sync_drops_equal_seq_duplicate(make_run) -> None:
    seed = 5
    walk = _first_walkable(seed)
    other = next(
        (x, y)
        for y in range(MAP_HEIGHT)
        for x in range(MAP_WIDTH)
        if _generate_tiles(seed)[y][x] == 0 and (x, y) != walk
    )
    run = make_run(seed=seed, player_x=1, player_y=1)
    session = _FakeSession(run)

    asyncio.run(
        sync_position(run.id, SyncPositionRequest(x=walk[0], y=walk[1], seq=7), session)  # type: ignore[arg-type]
    )
    # Same seq again (a duplicate/retried debounce) -> dropped as not-newer.
    r = asyncio.run(
        sync_position(run.id, SyncPositionRequest(x=other[0], y=other[1], seq=7), session)  # type: ignore[arg-type]
    )
    assert r.stale is True
    assert (r.player_x, r.player_y) == (walk[0], walk[1])


def test_sync_accepts_increasing_seq(make_run) -> None:
    seed = 5
    tiles = _generate_tiles(seed)
    walk = [
        (x, y)
        for y in range(MAP_HEIGHT)
        for x in range(MAP_WIDTH)
        if tiles[y][x] == 0
    ]
    (ax, ay), (bx, by) = walk[0], walk[1]
    run = make_run(seed=seed, player_x=1, player_y=1)
    session = _FakeSession(run)

    asyncio.run(
        sync_position(run.id, SyncPositionRequest(x=ax, y=ay, seq=1), session)  # type: ignore[arg-type]
    )
    r = asyncio.run(
        sync_position(run.id, SyncPositionRequest(x=bx, y=by, seq=2), session)  # type: ignore[arg-type]
    )
    assert r.stale is False
    assert (r.player_x, r.player_y) == (bx, by)


def test_sync_404_when_run_missing(make_run) -> None:
    from fastapi import HTTPException

    run = make_run(seed=5)
    session = _FakeSession(run)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            sync_position("nonexistent-id", SyncPositionRequest(x=1, y=1, seq=1), session)  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 404
