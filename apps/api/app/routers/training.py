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
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Monster, TrainingArtifact
from app.db.session import get_session
from app.schemas import (
    PreferenceBatch,
    PreferenceSubmit,
    Scorecard,
    TrainingHistory,
    TrainingHistoryEntry,
    TrainJob,
    TrainRequest,
)
from app.training import benchmark as benchmark_mod
from app.training import gepa as gepa_mod
from app.training import genome as genome_mod
from app.training import grpo_hitl

router = APIRouter(prefix="/api", tags=["training"])

# Use grpo_hitl's job store as the single source of truth for both kinds.
_JOBS = grpo_hitl._JOBS

# Test-friendly model override; production self-play uses the "default" alias.
_MODEL = "gemma3:1b"

# How many denoised self-play runs back each before/after benchmark. Kept small
# so train endpoints stay responsive on a CPU stack.
_BENCH_RUNS = 3


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
        "scorecard": None,
    }

    # Snapshot the "before" genome + cache key BEFORE training mutates the row.
    before_genome = genome_mod.read_genome(monster)
    before_version = int(getattr(monster, "genome_version", 1) or 1)
    before_bench = await _safe_baseline(monster_id, before_version, before_genome)

    try:
        new_genome, delta = await gepa_mod.run_gepa(
            session, monster, rounds=rounds, model=_MODEL
        )
        await session.commit()
        _JOBS[job_id]["status"] = "done"
        _JOBS[job_id]["score_delta"] = float(delta)
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        _JOBS[job_id]["status"] = "failed"
        raise HTTPException(status_code=500, detail=f"GEPA failed: {e}") from e

    # Measurable before/after delta (best-effort: never break the endpoint).
    _JOBS[job_id]["scorecard"] = await _build_scorecard(
        monster, before_genome, before_bench, after_genome=new_genome
    )

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

    # Snapshot the "before" genome + cache key BEFORE adoption mutates the row.
    before_genome = job.get("base_genome") or genome_mod.read_genome(monster)
    before_version = int(getattr(monster, "genome_version", 1) or 1)
    before_bench = await _safe_baseline(monster.id, before_version, before_genome)

    try:
        grpo_hitl.apply_preference(session, job_id, submit.ranking, monster=monster)
        await session.commit()
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"apply_preference failed: {e}") from e

    # Measurable before/after delta (best-effort: never break the endpoint).
    after_genome = genome_mod.read_genome(monster)
    _JOBS[job_id]["scorecard"] = await _build_scorecard(
        monster, before_genome, before_bench, after_genome=after_genome
    )

    return _job_to_trainjob(_JOBS[job_id])


@router.get("/training/{job_id}", response_model=TrainJob)
async def get_job(job_id: str) -> TrainJob:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _job_to_trainjob(job)


@router.get(
    "/monsters/{monster_id}/training/history", response_model=TrainingHistory
)
async def training_history(
    monster_id: str,
    session: AsyncSession = Depends(get_session),
) -> TrainingHistory:
    """Legible training timeline for a monster, one entry per TrainingArtifact.

    Each artifact records a before/after genome + score_delta + accepted flag. We
    surface them oldest-first so the frontend can render an improvement curve. The
    artifact schema has no genome_version column, so we reconstruct a monotonic
    version label by counting *accepted* artifacts up to each row (matching how
    ``apply_genome`` bumps ``Monster.genome_version`` only on acceptance).
    """
    monster = await _get_monster(session, monster_id)

    res = await session.execute(
        select(TrainingArtifact)
        .where(TrainingArtifact.monster_id == monster_id)
        .order_by(asc(TrainingArtifact.created_at))
    )
    artifacts = list(res.scalars().all())

    entries: list[TrainingHistoryEntry] = [
        TrainingHistoryEntry(
            genome_version=1,
            kind="seed",
            created_at=_iso(getattr(monster, "created_at", None)),
            note="initial genome",
        )
    ]

    version = 1
    for art in artifacts:
        if getattr(art, "accepted", False):
            version += 1
        kind = art.kind if art.kind in ("gepa", "grpo", "evolution", "seed") else "gepa"
        entries.append(
            TrainingHistoryEntry(
                genome_version=version,
                kind=kind,  # type: ignore[arg-type]
                created_at=_iso(getattr(art, "created_at", None)),
                judge_score=None,
                win_rate=None,
                note=_artifact_note(art),
            )
        )

    return TrainingHistory(monster_id=monster_id, entries=entries)


def _job_to_trainjob(job: dict) -> TrainJob:
    return TrainJob(
        job_id=job["job_id"],
        monster_id=job["monster_id"],
        kind=job["kind"],
        status=job["status"],
        score_delta=job.get("score_delta"),
        scorecard=job.get("scorecard"),
    )


# --------------------------------------------------------------------- helpers


async def _safe_baseline(
    monster_id: Optional[str],
    genome_version: int,
    genome: dict[str, Any],
) -> Optional[dict[str, float]]:
    """Cached "before" benchmark. Returns None on any failure (graceful)."""
    try:
        return await benchmark_mod.baseline_benchmark(
            genome,
            monster_id=monster_id,
            genome_version=genome_version,
            n_runs=_BENCH_RUNS,
            model=_MODEL,
            judge_model=settings.llm_judge_model,
        )
    except Exception:  # noqa: BLE001 — benchmarking is best-effort.
        return None


async def _build_scorecard(
    monster: Monster,
    before_genome: dict[str, Any],
    before_bench: Optional[dict[str, float]],
    *,
    after_genome: dict[str, Any],
) -> Optional[Scorecard]:
    """Run the "after" benchmark and assemble a Scorecard, or None on failure.

    Never raises: the design's whole point is graceful degradation, so a benchmark
    error leaves ``scorecard=None`` and the existing endpoint behavior intact.
    """
    if before_bench is None:
        return None
    try:
        after_bench = await benchmark_mod.run_benchmark(
            after_genome,
            n_runs=_BENCH_RUNS,
            model=_MODEL,
            judge_model=settings.llm_judge_model,
        )
    except Exception:  # noqa: BLE001
        return None

    return Scorecard(
        win_rate_before=before_bench["win_rate"],
        win_rate_after=after_bench["win_rate"],
        win_rate_delta=after_bench["win_rate"] - before_bench["win_rate"],
        judge_score_before=before_bench["judge_score"],
        judge_score_after=after_bench["judge_score"],
        judge_score_delta=after_bench["judge_score"] - before_bench["judge_score"],
        genome_diff=_genome_diff(before_genome, after_genome),
        n_benchmark_runs=_BENCH_RUNS,
    )


def _genome_diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Compact, legible summary of what training changed in the genome."""
    changed: list[str] = []
    keys = set(before) | set(after)
    for k in sorted(keys):
        if before.get(k) != after.get(k):
            changed.append(k)
    if not changed:
        return "no genome change"
    return "changed: " + ", ".join(changed)


def _artifact_note(art: TrainingArtifact) -> str:
    status = "accepted" if getattr(art, "accepted", False) else "rejected"
    delta = float(getattr(art, "score_delta", 0.0) or 0.0)
    return f"{art.kind} {status} (score_delta {delta:+.1f})"


def _iso(dt: Any) -> str:
    if dt is None:
        return ""
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return str(dt)
