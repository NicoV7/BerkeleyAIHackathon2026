"""Training router (WS-F) — GEPA + GRPO-HITL endpoints.

  POST /api/monsters/{id}/train/gepa        -> TrainJob (runs synchronously)
  POST /api/monsters/{id}/train/grpo        -> PreferenceBatch (K variants)
  POST /api/training/{job_id}/preference    -> TrainJob (ranking applied)
  GET  /api/training/{job_id}               -> TrainJob

Jobs live in the in-memory store in grpo_hitl (`_JOBS`); GEPA records a job there
too so GET works uniformly. Genomes + TrainingArtifacts persist to Postgres.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Monster
from app.db.session import get_session
from app.schemas import (
    PreferenceBatch,
    PreferenceSubmit,
    TrainJob,
    TrainRequest,
)
from app.training import gepa as gepa_mod
from app.training import grpo_hitl

router = APIRouter(prefix="/api", tags=["training"])

# Use grpo_hitl's job store as the single source of truth for both kinds.
_JOBS = grpo_hitl._JOBS

# Test-friendly model override; production self-play uses the "default" alias.
_MODEL = "gemma3:1b"


async def _get_monster(session: AsyncSession, monster_id: str) -> Monster:
    m = await session.get(Monster, monster_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Monster {monster_id} not found")
    return m


@router.post("/monsters/{monster_id}/train/gepa", response_model=TrainJob)
async def train_gepa(
    monster_id: str,
    req: Optional[TrainRequest] = None,
    session: AsyncSession = Depends(get_session),
) -> TrainJob:
    monster = await _get_monster(session, monster_id)
    rounds = (req or TrainRequest()).rounds

    job_id = str(uuid.uuid4())
    _JOBS[job_id] = {
        "job_id": job_id,
        "monster_id": monster_id,
        "kind": "gepa",
        "status": "running",
        "score_delta": None,
    }

    try:
        _genome, delta = await gepa_mod.run_gepa(
            session, monster, rounds=rounds, model=_MODEL
        )
        await session.commit()
        _JOBS[job_id]["status"] = "done"
        _JOBS[job_id]["score_delta"] = float(delta)
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        _JOBS[job_id]["status"] = "failed"
        raise HTTPException(status_code=500, detail=f"GEPA failed: {e}") from e

    return _job_to_trainjob(_JOBS[job_id])


@router.post("/monsters/{monster_id}/train/grpo", response_model=PreferenceBatch)
async def train_grpo(
    monster_id: str,
    session: AsyncSession = Depends(get_session),
) -> PreferenceBatch:
    monster = await _get_monster(session, monster_id)
    batch = await grpo_hitl.start_grpo(session, monster, model=_MODEL)
    return batch


@router.post("/training/{job_id}/preference", response_model=TrainJob)
async def submit_preference(
    job_id: str,
    submit: PreferenceSubmit,
    session: AsyncSession = Depends(get_session),
) -> TrainJob:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.get("kind") != "grpo":
        raise HTTPException(status_code=400, detail="Job is not a GRPO job")

    monster = await _get_monster(session, job["monster_id"])
    try:
        grpo_hitl.apply_preference(session, job_id, submit.ranking, monster=monster)
        await session.commit()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"apply_preference failed: {e}") from e

    return _job_to_trainjob(_JOBS[job_id])


@router.get("/training/{job_id}", response_model=TrainJob)
async def get_job(job_id: str) -> TrainJob:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _job_to_trainjob(job)


def _job_to_trainjob(job: dict) -> TrainJob:
    return TrainJob(
        job_id=job["job_id"],
        monster_id=job["monster_id"],
        kind=job["kind"],
        status=job["status"],
        score_delta=job.get("score_delta"),
    )
