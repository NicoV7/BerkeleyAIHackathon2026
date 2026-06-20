"""Encounter router (WS-B): create + read battles.

POST /api/encounters   seed Redis (meta, hp, queue, momentum), write Encounter row.
GET  /api/encounters/{id}   read live EncounterState from Redis (+ DB fallback).

Combatant setup pulls party from the Monster table and the enemy from WS-A's
`generate_wild` when available; otherwise fabricates a minimal enemy so WS-B can
develop independently. Helpers here (load_combatants, build_encounter_state) are
reused by the debate router.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Encounter, EncounterResult, Monster, MonsterOwner, Run
from app.db.session import get_session
from app.debate.orchestrator import Combatant
from app.redis_state import (
    ENCOUNTER_TTL_SECONDS,
    encounter_keys,
    get_hp_map,
    get_redis,
    get_transcript,
    k_hp,
    k_judge,
    k_meta,
    k_momentum,
    k_queue,
)
from app.schemas import (
    CombatantState,
    CreateEncounterRequest,
    EncounterState,
    JudgeVerdict,
    Utterance,
)

router = APIRouter(prefix="/api/encounters", tags=["encounter"])


# ---- Enemy setup (WS-A seam) ------------------------------------------------


def _fabricate_enemy(run_id: str) -> Monster:
    """Minimal stand-in wild enemy when WS-A's generator is absent."""
    return Monster(
        id=str(uuid.uuid4()),
        run_id=run_id,
        owner=MonsterOwner.wild,
        name="Wild Sophist",
        # type defaults to logos via the model; pick CHAOS for variety
        persona={"style": "provocative", "bio": "A wandering contrarian."},
        skills=["Reframe", "Counterexample"],
        level=2,
        max_hp=90,
        # Pin to a model that's reliably pulled locally so WS-B is self-sufficient
        # before WS-A's generator lands. WS-A's real monsters carry their own model.
        model="gemma3:1b",
    )


async def _resolve_enemies(
    session: AsyncSession, run_id: str, req: CreateEncounterRequest
) -> list[Monster]:
    # If a specific wild_id was provided and exists, use it.
    if req.wild_id:
        m = await session.get(Monster, req.wild_id)
        if m:
            return [m]
    # Try WS-A generator. Real signature: generate_wild(session, run_id, n=1, seed=0)
    # -> list[Monster] (commits internally). Request a single enemy for a quick
    # battle; if an enemy_group_id is given, fan out to a small group.
    try:
        from app.party.generator import generate_wild  # type: ignore

        n = 3 if req.enemy_group_id else 1
        res = generate_wild(session=session, run_id=run_id, n=n)
        import asyncio

        if asyncio.iscoroutine(res):
            res = await res
        if isinstance(res, list) and res:
            return res
        if res is not None:
            return [res]
    except Exception:  # noqa: BLE001
        pass
    # Fallback fabricate + persist so HP/id are real.
    enemy = _fabricate_enemy(run_id)
    session.add(enemy)
    await session.flush()
    return [enemy]


async def _resolve_party(session: AsyncSession, run_id: str) -> list[Monster]:
    res = await session.execute(
        select(Monster).where(
            Monster.run_id == run_id, Monster.owner == MonsterOwner.player
        )
    )
    party = list(res.scalars().all())
    if party:
        return party
    # Fabricate a minimal player monster so WS-B is self-sufficient pre-WS-A.
    pm = Monster(
        id=str(uuid.uuid4()),
        run_id=run_id,
        owner=MonsterOwner.player,
        name="Rookie Debater",
        persona={"style": "earnest", "bio": "An aspiring champion of reason."},
        skills=["Steelman", "Evidence Drop"],
        level=2,
        max_hp=100,
    )
    session.add(pm)
    await session.flush()
    return [pm]


# ---- Combatant <-> Redis ----------------------------------------------------


def _to_combatant(m: Monster, role: str) -> Combatant:
    mtype = getattr(m.type, "value", m.type)
    owner = getattr(m.owner, "value", m.owner)
    return Combatant(
        monster_id=m.id,
        name=m.name,
        type=str(mtype),
        role=role,
        hp=m.max_hp,
        max_hp=m.max_hp,
        level=m.level,
        owner=str(owner),
        persona=dict(m.persona or {}),
        harness=dict(m.harness or {}),
        skills=list(m.skills or []),
        model=m.model,
    )


def _initiative_order(combatants: list[Combatant]) -> list[str]:
    """Turn order by initiative = level (tiebreak: party first, then name)."""
    return [
        c.monster_id
        for c in sorted(
            combatants,
            key=lambda c: (-c.level, 0 if c.role == "party" else 1, c.name),
        )
    ]


async def seed_redis(
    eid: str, run_id: str, topic: str, combatants: list[Combatant]
) -> None:
    r = get_redis()
    pipe = r.pipeline()
    pipe.delete(*encounter_keys(eid))
    pipe.hset(
        k_meta(eid),
        mapping={
            "id": eid,
            "run_id": run_id,
            "topic": topic,
            "turn_no": 0,
            "phase": "intro",
            "status": "ongoing",
            # store the static combatant roster so the engine can rehydrate
            "combatants": json.dumps(
                [
                    {
                        "monster_id": c.monster_id,
                        "name": c.name,
                        "type": c.type,
                        "role": c.role,
                        "max_hp": c.max_hp,
                        "level": c.level,
                        "owner": c.owner,
                        "persona": c.persona,
                        "harness": c.harness,
                        "skills": c.skills,
                        "model": c.model,
                    }
                    for c in combatants
                ]
            ),
        },
    )
    for c in combatants:
        pipe.hset(k_hp(eid), c.monster_id, c.hp)
    pipe.rpush(k_queue(eid), *_initiative_order(combatants))
    pipe.hset(k_momentum(eid), mapping={"party": 1.0, "enemy": 1.0})
    for key in encounter_keys(eid):
        pipe.expire(key, ENCOUNTER_TTL_SECONDS)
    await pipe.execute()


async def load_combatants(eid: str) -> list[Combatant]:
    """Rehydrate combatants from Redis meta + current HP."""
    r = get_redis()
    meta = await r.hgetall(k_meta(eid))
    if not meta:
        raise HTTPException(status_code=404, detail="encounter not found")
    roster = json.loads(meta.get("combatants", "[]"))
    hp_map = await get_hp_map(eid)
    combatants: list[Combatant] = []
    for c in roster:
        cur_hp = hp_map.get(c["monster_id"], c["max_hp"])
        combatants.append(
            Combatant(
                monster_id=c["monster_id"],
                name=c["name"],
                type=c["type"],
                role=c["role"],
                hp=int(cur_hp),
                max_hp=int(c["max_hp"]),
                level=int(c.get("level", 1)),
                owner=c.get("owner", "wild"),
                persona=c.get("persona", {}) or {},
                harness=c.get("harness", {}) or {},
                skills=c.get("skills", []) or [],
                model=c.get("model"),
            )
        )
    return combatants


async def load_momentum(eid: str) -> dict[str, float]:
    r = get_redis()
    raw = await r.hgetall(k_momentum(eid))
    return {k: float(v) for k, v in raw.items()} or {"party": 1.0, "enemy": 1.0}


async def get_meta(eid: str) -> dict[str, str]:
    r = get_redis()
    meta = await r.hgetall(k_meta(eid))
    if not meta:
        raise HTTPException(status_code=404, detail="encounter not found")
    return meta


async def set_meta(eid: str, **fields) -> None:
    r = get_redis()
    await r.hset(k_meta(eid), mapping={k: str(v) for k, v in fields.items()})
    await r.expire(k_meta(eid), ENCOUNTER_TTL_SECONDS)


async def build_encounter_state(eid: str) -> EncounterState:
    r = get_redis()
    meta = await get_meta(eid)
    combatants = await load_combatants(eid)
    transcript_raw = await get_transcript(eid)
    verdicts_raw = await r.lrange(k_judge(eid), 0, -1)

    # After finalize the conversation keys are evicted; fall back to the durable
    # snapshot on the Encounter row so the transcript/verdicts still render.
    if not transcript_raw and not verdicts_raw:
        from app.db.session import SessionLocal

        async with SessionLocal() as session:
            enc = await session.get(Encounter, eid)
        if enc and (enc.transcript or enc.verdicts):
            transcript_raw = list(enc.transcript or [])
            verdicts_raw = [json.dumps(v) for v in (enc.verdicts or [])]

    combatant_states = [
        CombatantState(
            monster_id=c.monster_id,
            name=c.name,
            type=c.type,
            role=c.role,  # type: ignore[arg-type]
            hp=c.hp,
            max_hp=c.max_hp,
        )
        for c in combatants
    ]
    transcript = [
        Utterance(
            turn=u["turn"],
            actor_id=u["actor_id"],
            actor_role=u["actor_role"],
            skill_used=u.get("skill_used"),
            text=u["text"],
            ts=u["ts"],
        )
        for u in transcript_raw
    ]
    verdicts = [JudgeVerdict(**json.loads(v)) for v in verdicts_raw]

    return EncounterState(
        id=eid,
        run_id=meta.get("run_id", ""),
        topic=meta.get("topic", ""),
        phase=meta.get("phase", "intro"),  # type: ignore[arg-type]
        turn_no=int(meta.get("turn_no", 0) or 0),
        combatants=combatant_states,
        transcript=transcript,
        verdicts=verdicts,
    )


# ---- Endpoints --------------------------------------------------------------


@router.post("", response_model=EncounterState)
@router.post("/", response_model=EncounterState)
async def create_encounter(
    req: CreateEncounterRequest, session: AsyncSession = Depends(get_session)
) -> EncounterState:
    run = await session.get(Run, req.run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")

    party_monsters = await _resolve_party(session, req.run_id)
    enemy_monsters = await _resolve_enemies(session, req.run_id, req)

    party = [_to_combatant(m, "party") for m in party_monsters]
    enemies = [_to_combatant(m, "enemy") for m in enemy_monsters]
    combatants = party + enemies

    eid = str(uuid.uuid4())
    enc = Encounter(
        id=eid,
        run_id=req.run_id,
        topic=run.debate_topic,
        enemy_ids=[m.id for m in enemy_monsters],
        party_ids=[m.id for m in party_monsters],
        result=EncounterResult.ongoing,
    )
    session.add(enc)
    await session.commit()

    await seed_redis(eid, req.run_id, run.debate_topic, combatants)
    await set_meta(eid, phase="debating")
    return await build_encounter_state(eid)


@router.get("/{eid}", response_model=EncounterState)
async def get_encounter(
    eid: str, session: AsyncSession = Depends(get_session)
) -> EncounterState:
    try:
        return await build_encounter_state(eid)
    except HTTPException:
        # Redis expired — minimal reconstruction from the durable row.
        enc = await session.get(Encounter, eid)
        if not enc:
            raise HTTPException(status_code=404, detail="encounter not found")
        return EncounterState(
            id=enc.id,
            run_id=enc.run_id,
            topic=enc.topic,
            phase="won" if enc.result == EncounterResult.win else "lost"
            if enc.result == EncounterResult.loss
            else "debating",
            turn_no=0,
            combatants=[],
            transcript=[],
            verdicts=[],
        )
