"""Capture router (WS-E).

Routes owned by this module:
  POST /api/encounters/{encounter_id}/capture   — attempt to capture a wild
  GET  /api/runs/{run_id}/party                 — list player's party members

Auto-mounted by main.py via OPTIONAL_ROUTERS (name "capture" is already listed).
WS-C owns party.py (gambits); do NOT duplicate those paths here.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.db.models import Monster, MonsterOwner
from app.db.session import get_session
from app.schemas import CaptureRequest, CaptureResult, MonsterSummary
from app.party.capture import attempt_capture

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["capture"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/encounters/{encounter_id}/capture",
    response_model=CaptureResult,
    summary="Attempt to capture a weakened wild monster",
)
async def post_capture(
    encounter_id: str,
    body: CaptureRequest,
    session: SessionDep,
) -> CaptureResult:
    """Roll a capture attempt against the wild monster identified by wild_id.

    The monster must be in a capturable HP window (< 25% of max_hp) or the
    attempt is rejected with success=False and an explanatory message.
    """
    success, monster, message = await attempt_capture(
        session=session,
        encounter_id=encounter_id,
        wild_id=body.wild_id,
    )

    if success and monster is not None:
        try:
            await session.commit()
            await session.refresh(monster)
        except Exception as exc:
            await session.rollback()
            log.error("Failed to commit capture for %s: %s", body.wild_id, exc)
            raise HTTPException(status_code=500, detail="DB error during capture commit.") from exc

        summary = _to_summary(monster)
        return CaptureResult(success=True, monster=summary, message=message)

    return CaptureResult(success=False, monster=None, message=message)


@router.get(
    "/runs/{run_id}/party",
    response_model=list[MonsterSummary],
    summary="List all player-owned monsters in a run",
)
async def get_party(
    run_id: str,
    session: SessionDep,
) -> list[MonsterSummary]:
    """Return all monsters with owner='player' belonging to the given run."""
    result = await session.execute(
        select(Monster).where(
            Monster.run_id == run_id,
            Monster.owner == MonsterOwner.player,
        )
    )
    monsters = result.scalars().all()
    return [_to_summary(m) for m in monsters]


def _to_summary(m: Monster) -> MonsterSummary:
    return MonsterSummary(
        id=m.id,
        name=m.name,
        type=m.type.value if hasattr(m.type, "value") else str(m.type),
        owner=m.owner.value if hasattr(m.owner, "value") else str(m.owner),
        level=m.level,
        xp=m.xp,
        max_hp=m.max_hp,
        evolution_stage=m.evolution_stage,
        skills=list(m.skills) if m.skills else [],
    )
