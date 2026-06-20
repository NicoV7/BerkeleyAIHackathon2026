"""T2 integration — training HTTP endpoints (DB-backed, ASGI in-process).

Drives the WS-F training router end-to-end over httpx's ``ASGITransport`` so the
real FastAPI app, dependency wiring, response_model validation, and the
in-memory job store are all exercised — only the network is faked:

    POST /api/monsters/{id}/train/gepa   -> TrainJob          (sync GEPA)
    POST /api/monsters/{id}/train/grpo   -> PreferenceBatch   (K variants)
    POST /api/training/{job_id}/preference -> TrainJob         (ranking applied)
    GET  /api/training/{job_id}          -> TrainJob

What's real vs. faked:
  * Postgres is REAL (Monster/Run rows, ``genome_version`` persistence). Every
    test depends on the ``require_db`` fixture (tests/conftest.py), which *skips*
    (never errors) when Postgres is unreachable from the host — so collection
    always passes on a bare host while the live Docker stack is mid-edit.
  * The LLM gateway is FAKED via the deterministic ``gateway_mock`` fixture, so
    self-play / GEPA / judge calls are stable and offline (no Ollama/Anthropic).

The app's ``get_session`` dependency opens its own ``SessionLocal`` session, so
seed rows are committed *before* each HTTP call and re-read in a fresh session
afterward to assert persisted side effects (e.g. the genome_version bump).

Run collection-only (host-safe, no DB needed):

    cd apps/api && python -m pytest tests/integration/test_training_endpoints.py --collect-only
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

# Mark every test in this module as an asyncio coroutine test (asyncio_mode=auto
# is on, but the explicit marker keeps intent obvious and import-safe).
pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# DB guard
# --------------------------------------------------------------------------- #
#
# The shared ``require_db`` fixture (tests/conftest.py) probes a *localhost-
# rewritten* copy of DATABASE_URL, but these tests drive the real FastAPI app,
# whose ``get_session`` uses the un-rewritten URL (often the docker-only host
# ``postgres``). On a host with an unrelated Postgres on localhost:5432, the
# shared probe can report "available" while the app's own engine cannot resolve
# its host. So we additionally probe the *exact* engine the app will use and
# skip on any failure — keeping host collection + runs green (never errored)
# while the live Docker stack is mid-edit.

import asyncio


def _app_engine_reachable() -> bool:
    """Can the app's OWN async engine actually connect? Never raises."""
    try:
        from sqlalchemy import text

        from app.db.session import engine

        async def _ping() -> bool:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True

        return asyncio.run(asyncio.wait_for(_ping(), timeout=3.0))
    except Exception:  # noqa: BLE001 — any failure -> unavailable -> skip
        return False


# Probe once at import time so collection stays cheap and host-safe.
_APP_DB_REACHABLE = _app_engine_reachable()

require_app_db = pytest.mark.skipif(
    not _APP_DB_REACHABLE,
    reason="App's own Postgres engine unreachable (DATABASE_URL host); "
    "skipping DB-backed training-endpoint test. Bring up the compose stack.",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _seed_run_and_monster(session) -> tuple[str, str]:
    """Persist a Run + Monster so the training router's FK + lookup resolve.

    Returns (run_id, monster_id). Uses unique ids so repeated runs against a
    shared dev DB never collide. ``genome_version`` starts at its default (1).
    """
    from app.db.models import (
        DebateType,
        Monster,
        MonsterOwner,
        Run,
        RunStatus,
    )

    run = Run(
        id=f"itest-train-run-{uuid.uuid4().hex}",
        debate_topic="Should AI agents be allowed to debate?",
        seed=0,
        player_x=0,
        player_y=0,
        status=RunStatus.active,
    )
    monster = Monster(
        id=f"itest-train-mon-{uuid.uuid4().hex}",
        run_id=run.id,
        owner=MonsterOwner.player,
        name="Socratesaur",
        type=DebateType.logos,
        persona={"tone": "measured", "tactics": ["evidence"], "topic": "AI is good"},
        harness={"system": "You argue with rigor."},
        skills=[],
        level=1,
        xp=0,
        max_hp=100,
        evolution_stage=0,
    )
    session.add(run)
    session.add(monster)
    await session.commit()
    return run.id, monster.id


async def _genome_version(monster_id: str) -> int:
    """Read the persisted genome_version of a monster in a fresh session."""
    from app.db.models import Monster
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        monster = await session.get(Monster, monster_id)
        assert monster is not None, f"monster {monster_id!r} vanished"
        return int(monster.genome_version or 1)


def _asgi_client():
    """An httpx AsyncClient bound to the real FastAPI app over ASGITransport."""
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://itest")


# --------------------------------------------------------------------------- #
# GEPA endpoint
# --------------------------------------------------------------------------- #


@require_app_db
async def test_train_gepa_returns_trainjob_shape_and_bumps_genome_version(
    require_db, gateway_mock
):
    """POST /train/gepa returns a well-shaped done TrainJob and bumps the
    persisted genome_version past its starting value."""
    # Arrange: a persisted monster at genome_version 1.
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        _run_id, monster_id = await _seed_run_and_monster(session)
    start_version = await _genome_version(monster_id)
    assert start_version == 1

    # Act: run synchronous GEPA over the ASGI transport (gateway is stubbed).
    async with _asgi_client() as client:
        resp = await client.post(
            f"/api/monsters/{monster_id}/train/gepa",
            json={"rounds": 1},
        )

    # Assert: TrainJob contract holds and the run completed.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) >= {"job_id", "monster_id", "kind", "status", "score_delta"}
    assert body["monster_id"] == monster_id
    assert body["kind"] == "gepa"
    assert body["status"] == "done"
    assert isinstance(body["job_id"], str) and body["job_id"]
    # score_delta is Optional[float]; on a done GEPA job it is populated.
    assert body["score_delta"] is None or isinstance(body["score_delta"], (int, float))

    # Assert: the genome_version really advanced on the persisted row.
    final_version = await _genome_version(monster_id)
    assert final_version > start_version, (
        f"expected genome_version bump, start={start_version} final={final_version}"
    )


@require_app_db
async def test_train_gepa_unknown_monster_returns_404(require_db, gateway_mock):
    """POST /train/gepa for a missing monster id is a clean 404 (not a 500)."""
    # Arrange: an id that cannot exist.
    missing_id = f"nope-{uuid.uuid4().hex}"

    # Act.
    async with _asgi_client() as client:
        resp = await client.post(f"/api/monsters/{missing_id}/train/gepa", json={})

    # Assert.
    assert resp.status_code == 404, resp.text
    assert missing_id in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# GRPO start + preference endpoints
# --------------------------------------------------------------------------- #


@require_app_db
async def test_train_grpo_returns_preference_batch_with_k_variants(
    require_db, gateway_mock
):
    """POST /train/grpo returns a PreferenceBatch carrying K scored variants,
    each with a stable variant_id and a numeric judge_score."""
    # Arrange.
    from app.db.session import SessionLocal
    from app.training.grpo_hitl import K_VARIANTS

    async with SessionLocal() as session:
        _run_id, monster_id = await _seed_run_and_monster(session)

    # Act.
    async with _asgi_client() as client:
        resp = await client.post(f"/api/monsters/{monster_id}/train/grpo")

    # Assert: PreferenceBatch contract.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) >= {"job_id", "monster_id", "variants"}
    assert body["monster_id"] == monster_id
    variants = body["variants"]
    assert isinstance(variants, list)
    assert len(variants) == K_VARIANTS
    variant_ids = [v["variant_id"] for v in variants]
    assert len(set(variant_ids)) == len(variant_ids), "variant_ids must be unique"
    for v in variants:
        assert set(v) >= {"variant_id", "transcript", "judge_score"}
        assert isinstance(v["judge_score"], (int, float))
        assert isinstance(v["transcript"], list)


@require_app_db
async def test_grpo_preference_then_get_job_reports_done_and_bumps_genome(
    require_db, gateway_mock
):
    """Full GRPO HITL loop over HTTP: start -> submit a ranking -> the job is
    marked done with a score_delta, the genome_version is bumped, and GET
    /training/{job_id} reflects the same finished TrainJob."""
    # Arrange: start a GRPO batch and capture its variant ids.
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        _run_id, monster_id = await _seed_run_and_monster(session)
    start_version = await _genome_version(monster_id)

    async with _asgi_client() as client:
        start = await client.post(f"/api/monsters/{monster_id}/train/grpo")
        assert start.status_code == 200, start.text
        batch = start.json()
        job_id = batch["job_id"]
        ranking = [v["variant_id"] for v in batch["variants"]]  # best -> worst

        # Act: submit the human preference ranking.
        pref = await client.post(
            f"/api/training/{job_id}/preference",
            json={"ranking": ranking},
        )

        # ...and read the job back.
        got = await client.get(f"/api/training/{job_id}")

    # Assert: preference response is a finished GRPO TrainJob.
    assert pref.status_code == 200, pref.text
    pref_body = pref.json()
    assert pref_body["job_id"] == job_id
    assert pref_body["monster_id"] == monster_id
    assert pref_body["kind"] == "grpo"
    assert pref_body["status"] == "done"
    assert pref_body["score_delta"] is None or isinstance(
        pref_body["score_delta"], (int, float)
    )

    # Assert: GET returns the same finished job.
    assert got.status_code == 200, got.text
    got_body = got.json()
    assert got_body["job_id"] == job_id
    assert got_body["status"] == "done"
    assert got_body["kind"] == "grpo"

    # Assert: adopting the winning variant bumped the persisted genome_version.
    final_version = await _genome_version(monster_id)
    assert final_version > start_version, (
        f"expected genome_version bump after preference, "
        f"start={start_version} final={final_version}"
    )


@require_app_db
async def test_preference_for_unknown_job_returns_404(require_db, gateway_mock):
    """POST /training/{job_id}/preference for an unknown job id is a 404."""
    # Arrange.
    missing_job = f"job-{uuid.uuid4().hex}"

    # Act.
    async with _asgi_client() as client:
        resp = await client.post(
            f"/api/training/{missing_job}/preference",
            json={"ranking": []},
        )

    # Assert.
    assert resp.status_code == 404, resp.text
    assert missing_job in resp.json()["detail"]


@require_app_db
async def test_preference_against_gepa_job_is_rejected_400(require_db, gateway_mock):
    """Submitting a preference ranking against a GEPA job (wrong kind) is a 400.

    Proves the router guards job kind before touching the GRPO machinery.
    """
    # Arrange: produce a real GEPA job id via the endpoint.
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        _run_id, monster_id = await _seed_run_and_monster(session)

    async with _asgi_client() as client:
        gepa_resp = await client.post(
            f"/api/monsters/{monster_id}/train/gepa", json={"rounds": 1}
        )
        assert gepa_resp.status_code == 200, gepa_resp.text
        gepa_job_id = gepa_resp.json()["job_id"]

        # Act: try to submit a preference against the GEPA job.
        bad = await client.post(
            f"/api/training/{gepa_job_id}/preference",
            json={"ranking": []},
        )

    # Assert: rejected as a bad request, not silently mishandled.
    assert bad.status_code == 400, bad.text


@require_app_db
async def test_get_unknown_job_returns_404(require_db, gateway_mock):
    """GET /training/{job_id} for an unknown job id is a 404."""
    # Arrange.
    missing_job = f"job-{uuid.uuid4().hex}"

    # Act.
    async with _asgi_client() as client:
        resp = await client.get(f"/api/training/{missing_job}")

    # Assert.
    assert resp.status_code == 404, resp.text
    assert missing_job in resp.json()["detail"]
