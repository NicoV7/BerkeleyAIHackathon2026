"""Party router — Gambit CRUD for monster behavior rules.

Owns:
  GET  /api/monsters/{id}             -> MonsterSummary
  GET  /api/monsters/{id}/gambits     -> GambitList
  PUT  /api/monsters/{id}/gambits     -> GambitList  (full replace)

WS-E owns capture/party-listing routes in capture.py / party_progress.py.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import GambitRule, Monster
from app.db.session import get_session
from app.schemas import GambitList, GambitRuleModel, MonsterSummary
from app.serializers import monster_summary

router = APIRouter(prefix="/api", tags=["party"])

Session = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _monster_summary(m: Monster) -> MonsterSummary:
    """Return the shared monster projection used by polling and party routes."""
    return monster_summary(m)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/monsters/{monster_id}", response_model=MonsterSummary)
async def get_monster(monster_id: str, session: Session) -> MonsterSummary:
    """Return basic monster info (used by GambitEditor + BattleDebateView)."""
    monster = await session.get(Monster, monster_id)
    if monster is None:
        raise HTTPException(status_code=404, detail="Monster not found")
    return _monster_summary(monster)


@router.get("/monsters/{monster_id}/gambits", response_model=GambitList)
async def get_gambits(monster_id: str, session: Session) -> GambitList:
    """Return all gambit rules for a monster, sorted by priority ascending."""
    monster = await session.get(Monster, monster_id)
    if monster is None:
        raise HTTPException(status_code=404, detail="Monster not found")

    result = await session.execute(
        select(GambitRule)
        .where(GambitRule.monster_id == monster_id)
        .order_by(GambitRule.priority)
    )
    rules = result.scalars().all()

    return GambitList(
        monster_id=monster_id,
        rules=[
            GambitRuleModel(
                id=r.id,
                priority=r.priority,
                condition=r.condition,
                action=r.action,
                enabled=r.enabled,
            )
            for r in rules
        ],
    )


@router.put("/monsters/{monster_id}/gambits", response_model=GambitList)
async def put_gambits(monster_id: str, payload: GambitList, session: Session) -> GambitList:
    """Full-replace the gambit list for a monster.

    Deletes all existing rules then inserts the provided list in order.
    The frontend editor sends the full ordered list; server assigns new UUIDs
    for rules that don't have an id yet.
    """
    monster = await session.get(Monster, monster_id)
    if monster is None:
        raise HTTPException(status_code=404, detail="Monster not found")

    # Delete existing rules
    existing = await session.execute(
        select(GambitRule).where(GambitRule.monster_id == monster_id)
    )
    for rule in existing.scalars().all():
        await session.delete(rule)
    await session.flush()

    # Insert new rules
    saved: list[GambitRuleModel] = []
    for rule_model in payload.rules:
        rule_id = rule_model.id or str(uuid.uuid4())
        rule = GambitRule(
            id=rule_id,
            monster_id=monster_id,
            priority=rule_model.priority,
            condition=rule_model.condition,
            action=rule_model.action,
            enabled=rule_model.enabled,
        )
        session.add(rule)
        saved.append(
            GambitRuleModel(
                id=rule_id,
                priority=rule_model.priority,
                condition=rule_model.condition,
                action=rule_model.action,
                enabled=rule_model.enabled,
            )
        )

    await session.commit()
    return GambitList(monster_id=monster_id, rules=saved)
