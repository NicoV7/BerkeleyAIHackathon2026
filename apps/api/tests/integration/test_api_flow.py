"""T2 integration — end-to-end exercise of the core gameplay loop.

This module drives the FastAPI app *in-process* via httpx's ``ASGITransport``
against ``app.main.app`` (no uvicorn, no network socket), walking the canonical
player loop:

    POST /api/runs                      -> RunState (run id + starter party)
    GET  /api/runs/{id}/map             -> MapState (tiles + wild enemy tiles)
    POST /api/runs/{id}/move            -> MoveResult (clamped position)
    POST /api/encounters                -> EncounterState (battle seeded)
    POST /api/encounters/{id}/turn      -> TurnResult (one debate round)
    POST /api/encounters/{id}/capture   -> CaptureResult (reachable best-effort)
    GET  /api/runs/{id}/party           -> [MonsterSummary]

For each hop we assert the HTTP status and the key response shape (the fields the
frontend + downstream routers code against), not the exact non-deterministic
content (HP rolls, judge scores, capture success are all RNG/model-driven).

------------------------------------------------------------------------------
COLLECTION IS ALWAYS HOST-SAFE
------------------------------------------------------------------------------
The live stack runs Postgres + Redis on Docker-internal hostnames
(``postgres`` / ``redis``) that do not resolve from a developer's host, and the
implementation fleet may be mid-edit. So:

  * Nothing here touches a DB or Redis at *import* time -> ``--collect-only``
    always passes on a bare host.
  * The whole module is SKIP-guarded: every test depends on the ``live_client``
    fixture, which (a) skips unless a Postgres is reachable from the host and
    (b) skips unless Redis is reachable, rewriting Docker-internal hosts to
    localhost for the probe. On a bare host all tests report ``skipped``.

------------------------------------------------------------------------------
RUNNING IT FOR REAL
------------------------------------------------------------------------------
Inside the Docker network the ``postgres`` / ``redis`` hostnames resolve, so run
it from within the api container against the live stack::

    docker compose exec api python -m pytest \
        tests/integration/test_api_flow.py -v

On the host, point the app at the published ports first (the compose stack maps
5432 -> localhost:5432 and 6379 -> localhost:6379), e.g.::

    DATABASE_URL=postgresql+asyncpg://debate:debate@localhost:5432/debate \
    REDIS_URL=redis://localhost:6379/0 \
    python -m pytest tests/integration/test_api_flow.py -v

Collect-only (the gate that must always pass, even on a bare host)::

    python -m pytest tests/integration/test_api_flow.py --collect-only -q
"""
from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest

# httpx is a hard runtime dependency of the api package, so importing it here is
# safe at collection time.
import httpx
from httpx import ASGITransport


# --------------------------------------------------------------------------- #
# Reachability probes (host-side, Docker-internal-host aware)
# --------------------------------------------------------------------------- #
#
# CRITICAL: we probe the app's *actual* configured URLs (NO Docker-host -> localhost
# rewrite). The skip-guard must reflect what the running app will really attempt:
# the app's lifespan calls init_db() and the encounter path opens Redis using
# settings.database_url / settings.redis_url verbatim. If those point at the
# Docker-internal hosts (`postgres` / `redis`) they won't resolve from a bare host
# -> the probe fails -> we skip (never error). Inside the compose network those
# hostnames resolve, or override DATABASE_URL/REDIS_URL to localhost (see the
# module docstring) and the same probe will pass. We attempt a real connection
# with a short timeout and treat ANY failure (DNS, refused, auth, timeout) as
# "unavailable".

_PROBE_TIMEOUT = 2.0


def _split_host_port(url: str, default_port: int) -> tuple[str, int]:
    netloc = url.split("://", 1)[-1].split("@", 1)[-1].split("/", 1)[0]
    host, _, port_s = netloc.partition(":")
    return host, int(port_s) if port_s.isdigit() else default_port


def _tcp_open(host: str, port: int) -> bool:
    # gaierror (unresolved Docker hostname) is an OSError subclass, so it's caught.
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _postgres_reachable() -> bool:
    """True iff the app's configured Postgres accepts a real connection now.

    Uses settings.database_url unchanged so the guard matches what init_db() does.
    """
    from app.config import settings

    url = settings.database_url
    host, port = _split_host_port(url, 5432)
    if not _tcp_open(host, port):
        return False
    try:
        import asyncpg  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        async def _connect() -> bool:
            conn = await asyncio.wait_for(asyncpg.connect(dsn=dsn), timeout=_PROBE_TIMEOUT)
            await conn.close()
            return True

        return asyncio.run(_connect())
    except Exception:  # noqa: BLE001
        return False


def _redis_reachable() -> bool:
    """True iff the app's configured Redis answers a TCP connect now.

    Uses settings.redis_url unchanged so the guard matches what the encounter
    path's get_redis() will use at runtime.
    """
    from app.config import settings

    host, port = _split_host_port(settings.redis_url, 6379)
    return _tcp_open(host, port)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def _stack_available() -> bool:
    """Session/module-cached: are BOTH Postgres and Redis reachable?

    The core loop seeds Redis (encounter state) and writes Postgres rows, so a
    real run needs both. Probed once per module.
    """
    return _postgres_reachable() and _redis_reachable()


@pytest.fixture
async def live_client(
    _stack_available: bool, gateway_mock: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx AsyncClient bound to app.main.app via ASGITransport.

    Skips the requesting test unless the live Postgres+Redis stack is reachable
    from the host. ``gateway_mock`` (from the shared conftest) neutralizes the
    LLM gateway so the debate-turn hop is deterministic and offline.

    The app's lifespan runs ``init_db`` on enter, so entering the transport
    context establishes the schema against the live DB.
    """
    if not _stack_available:
        pytest.skip(
            "Live Postgres+Redis stack not reachable from host; "
            "run inside the compose network (see module docstring)."
        )

    from app.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Trigger lifespan (init_db) explicitly so schema exists before requests.
        async with app.router.lifespan_context(app):
            yield client


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _create_run(
    client: httpx.AsyncClient,
    topic: str = "Should AI write tests?",
    player_name: str = "Test Player",
) -> dict:
    resp = await client.post(
        "/api/runs",
        json={"topic": topic, "player_name": player_name, "seed": 7},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_run_returns_run_state_with_party(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange
    topic = "Is a hotdog a sandwich?"
    player_name = "Ada"

    # Act
    resp = await live_client.post(
        "/api/runs",
        json={"topic": topic, "player_name": player_name, "seed": 3},
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["id"], str) and body["id"]
    assert body["debate_topic"] == topic
    assert body["player_name"] == player_name
    assert body["status"] == "active"
    assert isinstance(body["party"], list) and len(body["party"]) >= 1
    member = body["party"][0]
    for key in ("id", "name", "type", "owner", "level", "max_hp"):
        assert key in member, f"party member missing {key}"
    assert member["owner"] == "player"


@pytest.mark.asyncio
async def test_get_map_returns_grid_and_player_position(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange
    run = await _create_run(live_client)

    # Act
    resp = await live_client.get(f"/api/runs/{run['id']}/map")

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["width"] > 0 and body["height"] > 0
    assert len(body["tiles"]) == body["height"]
    assert all(len(row) == body["width"] for row in body["tiles"])
    assert 0 <= body["player_x"] < body["width"]
    assert 0 <= body["player_y"] < body["height"]
    assert isinstance(body["enemies"], list)


@pytest.mark.asyncio
async def test_get_map_unknown_run_returns_404(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange / Act
    resp = await live_client.get("/api/runs/does-not-exist/map")

    # Assert
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_move_returns_clamped_position(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange
    run = await _create_run(live_client)
    map_before = (await live_client.get(f"/api/runs/{run['id']}/map")).json()

    # Act — step one tile right.
    resp = await live_client.post(f"/api/runs/{run['id']}/move", json={"dx": 1, "dy": 0})

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert 0 <= body["player_x"] < map_before["width"]
    assert 0 <= body["player_y"] < map_before["height"]
    # encounter_id is either None or a wild monster id string.
    assert body["encounter_id"] is None or isinstance(body["encounter_id"], str)


@pytest.mark.asyncio
async def test_create_encounter_seeds_battle_state(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange
    run = await _create_run(live_client)

    # Act
    resp = await live_client.post("/api/encounters", json={"run_id": run["id"]})

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["id"], str) and body["id"]
    assert body["run_id"] == run["id"]
    assert body["phase"] in ("intro", "debating", "capturable", "won", "lost")
    # At least one party + one enemy combatant should be present.
    roles = {c["role"] for c in body["combatants"]}
    assert "party" in roles
    assert "enemy" in roles


@pytest.mark.asyncio
async def test_create_encounter_unknown_run_returns_404(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange / Act
    resp = await live_client.post("/api/encounters", json={"run_id": "no-such-run"})

    # Assert
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_take_turn_advances_debate_and_returns_turn_result(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange
    run = await _create_run(live_client)
    enc = (await live_client.post("/api/encounters", json={"run_id": run["id"]})).json()

    # Act — run one debate round (gateway is stubbed -> deterministic + offline).
    resp = await live_client.post(f"/api/encounters/{enc['id']}/turn")

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Shape of TurnResult.
    assert "encounter" in body
    assert isinstance(body["new_utterances"], list)
    assert isinstance(body["new_verdicts"], list)
    assert isinstance(body["capturable_ids"], list)
    assert body["encounter"]["id"] == enc["id"]
    # The round should not have regressed the turn counter.
    assert body["encounter"]["turn_no"] >= enc["turn_no"]


@pytest.mark.asyncio
async def test_get_party_lists_player_monsters(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange
    run = await _create_run(live_client)

    # Act
    resp = await live_client.get(f"/api/runs/{run['id']}/party")

    # Assert
    assert resp.status_code == 200, resp.text
    party = resp.json()
    assert isinstance(party, list) and len(party) >= 1
    assert all(m["owner"] == "player" for m in party)


@pytest.mark.asyncio
async def test_capture_attempt_returns_well_formed_result(
    live_client: httpx.AsyncClient,
) -> None:
    # Arrange — create a battle so a wild monster + encounter exist.
    run = await _create_run(live_client)
    enc = (await live_client.post("/api/encounters", json={"run_id": run["id"]})).json()
    wild_id = next(
        (c["monster_id"] for c in enc["combatants"] if c["role"] == "enemy"),
        None,
    )
    assert wild_id is not None, "encounter had no enemy combatant to target"

    # Act — attempt capture. Success is HP-gated/RNG; we assert the CONTRACT only.
    resp = await live_client.post(
        f"/api/encounters/{enc['id']}/capture", json={"wild_id": wild_id}
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["success"], bool)
    assert "message" in body
    # On success a monster summary is returned; on failure it's null.
    if body["success"]:
        assert body["monster"] is not None
        assert body["monster"]["owner"] == "player"
    else:
        assert body["monster"] is None


@pytest.mark.asyncio
async def test_full_loop_run_to_turn_smoke(
    live_client: httpx.AsyncClient,
) -> None:
    """End-to-end smoke: run -> map -> move -> encounter -> turn all 200 and chain."""
    # Arrange / Act / Assert — each hop validated inline as the loop progresses.
    run = await _create_run(live_client, topic="Pineapple on pizza?")
    rid = run["id"]

    map_resp = await live_client.get(f"/api/runs/{rid}/map")
    assert map_resp.status_code == 200, map_resp.text

    move_resp = await live_client.post(f"/api/runs/{rid}/move", json={"dx": 0, "dy": 1})
    assert move_resp.status_code == 200, move_resp.text

    enc_resp = await live_client.post("/api/encounters", json={"run_id": rid})
    assert enc_resp.status_code == 200, enc_resp.text
    eid = enc_resp.json()["id"]

    turn_resp = await live_client.post(f"/api/encounters/{eid}/turn")
    assert turn_resp.status_code == 200, turn_resp.text
    assert turn_resp.json()["encounter"]["id"] == eid
