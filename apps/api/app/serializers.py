"""Shared API projections for database models.

Routers use these helpers to keep additive fields (gacha stats, persona voice,
hydration state) consistent across list, poll, and pull endpoints.
"""
from __future__ import annotations

from app.db.models import Monster, MonsterDomain
from app.schemas import MonsterSummary


def monster_summary(monster: Monster) -> MonsterSummary:
    """Project a ``Monster`` row into the public ``MonsterSummary`` contract."""
    return MonsterSummary(
        id=monster.id,
        name=monster.name,
        type=monster.type.value if hasattr(monster.type, "value") else str(monster.type),
        owner=monster.owner.value if hasattr(monster.owner, "value") else str(monster.owner),
        level=monster.level,
        xp=monster.xp,
        max_hp=monster.max_hp,
        evolution_stage=monster.evolution_stage,
        skills=monster.skills or [],
        atk=getattr(monster, "atk", 10),
        def_=getattr(monster, "def_", 10),
        mp=getattr(monster, "mp", 50),
        max_mp=getattr(monster, "max_mp", 50),
        domain=getattr(monster, "domain", MonsterDomain.GENERAL),
        wiki_url=getattr(monster, "wiki_url", None),
        wiki_hydrated=getattr(monster, "wiki_hydrated", False),
        is_avatar=bool(getattr(monster, "is_avatar", False)),
        persona=dict(getattr(monster, "persona", {}) or {}),
    )
