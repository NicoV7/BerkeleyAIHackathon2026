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

from app.db.models import GambitRule, Monster, MonsterDomain
from app.db.session import get_session
from app.schemas import GambitList, GambitRuleModel, MonsterSummary

router = APIRouter(prefix="/api", tags=["party"])

Session = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _monster_summary(m: Monster) -> MonsterSummary:
    # Gacha-wave stats are surfaced here too so the FE polling endpoint
    # (`GET /api/monsters/{id}`) carries `wiki_hydrated` for the hydrate gate.
    return MonsterSummary(
        id=m.id,
        name=m.name,
        type=m.type.value if hasattr(m.type, "value") else str(m.type),
        owner=m.owner.value if hasattr(m.owner, "value") else str(m.owner),
        level=m.level,
        xp=m.xp,
        max_hp=m.max_hp,
        evolution_stage=m.evolution_stage,
        skills=m.skills or [],
        atk=getattr(m, "atk", 10),
        def_=getattr(m, "def_", 10),
        mp=getattr(m, "mp", 50),
        max_mp=getattr(m, "max_mp", 50),
        domain=getattr(m, "domain", MonsterDomain.GENERAL),
        wiki_url=getattr(m, "wiki_url", None),
        wiki_hydrated=getattr(m, "wiki_hydrated", False),
    )


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
