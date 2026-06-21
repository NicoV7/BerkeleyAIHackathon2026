"""Onboarding router (WS-2) — empty-start first-agent grant + first quest.

A NEW run starts with NO party agents (gated by ``settings.empty_start_enabled``
in ``routers/map.create_run``). The scripted intro NPC calls these endpoints to:

  POST /api/runs/{run_id}/onboarding/first-pull   -> FirstPullResponse
      Grant the player's FIRST agent by REUSING the gacha pull
      (``routers.gacha.pull``). Idempotent: if the run already has any
      player-owned monster, no second agent is created — the existing first
      agent is returned with ``granted=False``.

  POST /api/runs/{run_id}/onboarding/first-quest  -> {quest}
      Offer the scripted intro quest via ``world.quests.offer_typed_quest``.
      Idempotent at the quest level (re-offering the same quest is a no-op).

These reuse existing systems wholesale (gacha pull, quest offer, economy) — no
duplicated pull/quest logic lives here.
"""
from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Monster, MonsterOwner, Run
from app.db.session import get_session
from app.party.generator import apply_avatar_traits
from app.schemas import GachaPullRequest, MonsterSummary
from app.serializers import monster_summary

router = APIRouter(prefix="/api", tags=["onboarding"])

Session = Annotated[AsyncSession, Depends(get_session)]


class FirstPullRequest(BaseModel):
    """Optional deterministic seed so the scripted intro can reproduce the roll."""

    seed: Optional[int] = Field(default=None)


class FirstPullResponse(BaseModel):
    monster: MonsterSummary
    granted: bool  # True if THIS call created the agent; False if already present
    party_size: int


class FirstQuestRequest(BaseModel):
    """Offer the scripted intro quest from the intro NPC."""

    npc_id: str = "intro_guide"
    objective: str = "hunt_enemy"
    target: str = "wild"
    target_name: Optional[str] = None
    target_xy: Optional[dict[str, int]] = None


async def _player_monsters(session: AsyncSession, run_id: str) -> list[Monster]:
    res = await session.execute(
        select(Monster)
        .where(Monster.run_id == run_id, Monster.owner == MonsterOwner.player)
        .order_by(Monster.created_at.asc())
    )
    return list(res.scalars().all())


async def _apply_run_avatar(
    session: AsyncSession, run: Run, monster: Monster
) -> Monster:
    """Force the first onboarding monster to the run's selected avatar type."""
    if apply_avatar_traits(monster, getattr(run, "avatar_type", None)):
        session.add(monster)
        await session.commit()
        await session.refresh(monster)
    return monster


@router.post(
    "/runs/{run_id}/onboarding/first-pull", response_model=FirstPullResponse
)
async def first_pull(
    run_id: str,
    body: FirstPullRequest,
    session: Session,
) -> FirstPullResponse:
    """Grant the player's FIRST agent (idempotent), reusing the gacha pull.

    If the run already owns any monster, this is a no-op grant: the earliest
    player-owned monster is returned with ``granted=False``. Otherwise the gacha
    pull runs (common-tier starter roll) and the new agent is returned with
    ``granted=True``. This prevents double-granting if the intro NPC is re-talked.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    existing = await _player_monsters(session, run_id)
    if existing:
        first = await _apply_run_avatar(session, run, existing[0])
        return FirstPullResponse(
            monster=monster_summary(first),
            granted=False,
            party_size=len(existing),
        )

    # Reuse the gacha pull wholesale (no duplicated roll/persona logic). A
    # starter pull has no summon_item_id, so it rolls from the common tier.
    from app.routers.gacha import pull as gacha_pull

    result = await gacha_pull(
        run_id, GachaPullRequest(summon_item_id=None, seed=body.seed), session
    )

    party = await _player_monsters(session, run_id)
    if party:
        first = await _apply_run_avatar(session, run, party[0])
        result.monster = monster_summary(first)
    return FirstPullResponse(
        monster=result.monster,
        granted=True,
        party_size=len(party),
    )


@router.post("/runs/{run_id}/onboarding/first-quest")
async def first_quest(
    run_id: str,
    body: FirstQuestRequest,
    session: Session,
) -> dict[str, Any]:
    """Offer the scripted intro quest (idempotent), reusing the quest system."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    from app.world import quests

    quest = await quests.offer_typed_quest(
        run_id,
        body.npc_id,
        body.objective,
        body.target,
        target_name=body.target_name,
        target_xy=body.target_xy,
    )
    return {"quest": quest.to_dict()}
