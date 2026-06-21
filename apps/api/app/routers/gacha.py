"""Gacha router (Wave A) — persona summons + post-battle drops.

Endpoints (mounted under the shared /api prefix):
    POST /api/runs/{run_id}/gacha/pull   GachaPullRequest -> GachaPullResult
    GET  /api/runs/{run_id}/summons      -> list[SummonItemSummary]

The pull weights by tier (common 70 / rare 25 / legendary 5 by default; a
``summon_item_id`` body shifts the distribution upward by consuming the item).
A ``Monster`` is inserted with the rolled ``Persona``'s name/domain/type/stats
and ``wiki_hydrated=False``; an in-process ``asyncio.create_task`` schedules
the Wikipedia hydration so the request returns instantly.

This router is import-safe even if hydration / personas_seed is mid-edit: the
hydration scheduler swallows any local failure so the endpoint never 500s.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DebateType,
    Monster,
    MonsterDomain,
    MonsterOwner,
    Persona,
    Run,
    SummonItem,
)
from app.db.session import get_session
from app.schemas import (
    GachaPullRequest,
    GachaPullResult,
    SummonItemSummary,
)
from app.serializers import monster_summary

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["gacha"])

Session = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Tier weighting
# ---------------------------------------------------------------------------

# Default starter weights: common 70 / rare 25 / legendary 5. A consumed item
# of tier T promotes the floor — a "rare" item rolls strictly from the rare or
# legendary buckets, and a "legendary" item is a guaranteed legendary pull.
_DEFAULT_TIER_WEIGHTS: dict[str, int] = {"common": 70, "rare": 25, "legendary": 5}
_TIER_ORDER: tuple[str, ...] = ("common", "rare", "legendary")


def _weights_for_item(item_tier: Optional[str]) -> dict[str, int]:
    """Return the tier-weight dict for a pull, biased by an item's tier."""
    if not item_tier or item_tier == "common":
        return dict(_DEFAULT_TIER_WEIGHTS)
    if item_tier == "rare":
        # Rare items unlock rare + legendary only.
        return {"rare": 80, "legendary": 20}
    if item_tier == "legendary":
        return {"legendary": 100}
    return dict(_DEFAULT_TIER_WEIGHTS)


def _roll_tier(weights: dict[str, int], rng: random.Random) -> str:
    """Sample a tier label by integer weight; falls back to ``common`` defensively."""
    pool = [(t, w) for t, w in weights.items() if w > 0]
    if not pool:
        return "common"
    total = sum(w for _, w in pool)
    pick = rng.uniform(0, total)
    acc = 0.0
    for tier, w in pool:
        acc += w
        if pick <= acc:
            return tier
    return pool[-1][0]


def _pick_persona(personas: list[Persona], tier: str, rng: random.Random) -> Persona:
    """Pick a persona from the rolled tier; degrade to any persona if the tier is empty."""
    candidates = [p for p in personas if (p.tier or "common") == tier]
    if not candidates:
        candidates = personas
    return rng.choice(candidates)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summon_summary(item: SummonItem) -> SummonItemSummary:
    return SummonItemSummary(
        id=item.id,
        run_id=item.run_id,
        tier=item.tier,
        consumed=item.consumed,
    )


def _schedule_hydration(monster_id: str, wiki_url: Optional[str], fallback_tagline: str) -> None:
    """Fire-and-forget background hydration. Best-effort: any failure is swallowed
    so the request path never depends on it.
    """
    try:
        from app.party.hydrate import hydrate_monster

        asyncio.create_task(
            hydrate_monster(monster_id, wiki_url or "", fallback_tagline or "")
        )
    except Exception as e:  # noqa: BLE001
        log.info("gacha: hydration not scheduled (%s)", e)


def _coerce_type(value: object) -> DebateType:
    """Coerce a stored persona type (enum, str, or weird value) into a DebateType."""
    if isinstance(value, DebateType):
        return value
    try:
        return DebateType(str(value).lower() if str(value).isupper() else str(value))
    except Exception:  # noqa: BLE001
        # Final fallback so a bad seed row can never 500 the pull.
        return DebateType.logos


def _rng_for_pull(run_id: str, body: GachaPullRequest) -> random.Random:
    """Return an RNG for a pull, deterministic only when the request asks for it."""
    if body.seed is None:
        return random.Random()
    material = f"{run_id}:{body.summon_item_id or 'starter'}:{body.seed}".encode()
    seed = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return random.Random(seed)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/runs/{run_id}/gacha/pull", response_model=GachaPullResult)
async def pull(
    run_id: str,
    body: GachaPullRequest,
    session: Session,
) -> GachaPullResult:
    """Pull a persona into the run's party.

    Body's ``summon_item_id`` (if present) must be an unconsumed item for this
    run; it is marked consumed in the same transaction as the new Monster row.
    Hydration runs in the background — the response carries ``wiki_hydrated=False``
    and the frontend polls ``GET /api/monsters/{id}`` until it flips true.
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    item: Optional[SummonItem] = None
    item_tier: Optional[str] = None
    if body.summon_item_id:
        item = await session.get(SummonItem, body.summon_item_id)
        if item is None or item.run_id != run_id:
            raise HTTPException(status_code=404, detail="Summon item not found")
        if item.consumed:
            raise HTTPException(status_code=409, detail="Summon item already consumed")
        item_tier = item.tier

    # Persona catalog (Wave 0 seeds this on startup; query is small and bounded).
    personas_res = await session.execute(select(Persona))
    personas = list(personas_res.scalars().all())
    if not personas:
        raise HTTPException(
            status_code=503,
            detail="Persona catalog not seeded; cannot pull",
        )

    rng = _rng_for_pull(run_id, body)
    tier = _roll_tier(_weights_for_item(item_tier), rng)
    persona = _pick_persona(personas, tier, rng)

    # Build the Monster from the persona's defaults. wiki_hydrated stays False;
    # hydration flips it after the background task patches persona JSONB.
    monster = Monster(
        run_id=run_id,
        owner=MonsterOwner.player,
        name=persona.name,
        type=_coerce_type(persona.type),
        persona={
            "key": persona.key,
            "tagline": persona.tagline or "",
            "voice": persona.tagline or "",
        },
        harness={},
        skills=[],
        level=1,
        xp=0,
        max_hp=persona.default_max_hp,
        evolution_stage=0,
        atk=persona.default_atk,
        def_=persona.default_def,
        mp=persona.default_mp,
        max_mp=persona.default_mp,
        domain=persona.domain or MonsterDomain.GENERAL,
        wiki_url=persona.wiki_url,
        wiki_hydrated=False,
        model="llama3.2:3b",
    )
    session.add(monster)

    if item is not None:
        item.consumed = True
        session.add(item)

    await session.commit()
    await session.refresh(monster)

    # Schedule background hydration AFTER the row is durable so the worker can
    # see the monster by id. Best-effort: never blocks the response.
    _schedule_hydration(monster.id, persona.wiki_url, persona.tagline or "")

    return GachaPullResult(
        monster=monster_summary(monster),
        persona_key=persona.key,
        persona_tier=tier,
    )


@router.get("/runs/{run_id}/summons", response_model=list[SummonItemSummary])
async def list_summons(run_id: str, session: Session) -> list[SummonItemSummary]:
    """List all summon items dropped in this run (consumed + un-consumed)."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    res = await session.execute(
        select(SummonItem)
        .where(SummonItem.run_id == run_id)
        .order_by(SummonItem.created_at.desc())
    )
    return [_summon_summary(s) for s in res.scalars().all()]
