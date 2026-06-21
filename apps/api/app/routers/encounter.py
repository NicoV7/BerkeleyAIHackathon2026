"""Encounter router (WS-B): create + read battles.

POST /api/encounters   seed Redis (meta, hp, queue, momentum), write Encounter row.
GET  /api/encounters/{id}   read live EncounterState from Redis (+ DB fallback).

Combatant setup pulls party from the Monster table and the enemy from WS-A's
`generate_wild` when available; otherwise fabricates a minimal enemy so WS-B can
develop independently. Helpers here (load_combatants, build_encounter_state) are
reused by the debate router.
"""
from __future__ import annotations

import asyncio
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
    get_mp_map,
    get_redis,
    get_transcript,
    k_hp,
    k_judge,
    k_meta,
    k_momentum,
    k_mp,
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
        # Gacha-wave stats — flow into compute_damage + MP economy.
        atk=int(getattr(m, "atk", 10) or 10),
        def_=int(getattr(m, "def_", 10) or 10),
        max_mp=int(getattr(m, "max_mp", 50) or 50),
        domain=str(getattr(m, "domain", "GENERAL") or "GENERAL"),
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
                        # Gacha-wave stats persisted on the roster so load_combatants
                        # rehydrates them after the first round (otherwise the
                        # second round's compute_damage would see neutral defaults
                        # and the MP cap would silently reset to 50).
                        "atk": c.atk,
                        "def": c.def_,
                        "max_mp": c.max_mp,
                        "domain": c.domain,
                    }
                    for c in combatants
                ]
            ),
        },
    )
    for c in combatants:
        pipe.hset(k_hp(eid), c.monster_id, c.hp)
        # Gacha Wave B: every combatant starts at full MP. End-of-round regen
        # (+10) and skill-use deductions keep the cache in sync after that.
        pipe.hset(k_mp(eid), c.monster_id, c.max_mp)
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
                # Gacha-wave: rehydrate stats from the roster (default to neutral
                # values for any encounter created before the stats were persisted).
                atk=int(c.get("atk", 10) or 10),
                def_=int(c.get("def", 10) or 10),
                max_mp=int(c.get("max_mp", 50) or 50),
                domain=str(c.get("domain", "GENERAL") or "GENERAL"),
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

    # Gacha Wave B: surface MP (+ ATK/DEF/domain) on the encounter snapshot so
    # the WS clients can paint the blue MP bar from `state` alone (no extra poll).
    mp_map = await get_mp_map(eid)
    combatant_states = [
        CombatantState(
            monster_id=c.monster_id,
            name=c.name,
            type=c.type,
            role=c.role,  # type: ignore[arg-type]
            hp=c.hp,
            max_hp=c.max_hp,
            mp=int(mp_map.get(c.monster_id, c.max_mp)),
            max_mp=c.max_mp,
            atk=c.atk,
            def_=c.def_,
            domain=c.domain,
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


# ---- Idempotency ------------------------------------------------------------


async def _find_active_encounter(
    session: AsyncSession, run_id: str, wild_id: str
) -> Encounter | None:
    """Return the most recent ONGOING encounter for (run_id, wild_id), or None.

    Used to make create_encounter idempotent: a roaming enemy can re-collide with
    the player while the first create POST is still in flight, so a second POST for
    the same wild must return the existing battle rather than spawn a duplicate.
    Matches on run_id (indexed) and membership of wild_id in the JSONB enemy_ids.
    """
    res = await session.execute(
        select(Encounter)
        .where(
            Encounter.run_id == run_id,
            Encounter.result == EncounterResult.ongoing,
        )
        .order_by(Encounter.created_at.desc())
    )
    for enc in res.scalars().all():
        if wild_id in (enc.enemy_ids or []):
            return enc
    return None


# ---- Endpoints --------------------------------------------------------------


@router.post("", response_model=EncounterState)
@router.post("/", response_model=EncounterState)
async def create_encounter(
    req: CreateEncounterRequest, session: AsyncSession = Depends(get_session)
) -> EncounterState:
    run = await session.get(Run, req.run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")

    # Validate that an explicitly-requested wild_id actually belongs to THIS run
    # before doing anything else (don't let a stray id pull a monster from another
    # run / fabricate around it). Unknown wild for the run -> 404.
    if req.wild_id:
        wild = await session.get(Monster, req.wild_id)
        if wild is None or wild.run_id != req.run_id:
            raise HTTPException(status_code=404, detail="wild not found for run")

    # IDEMPOTENCY: roaming enemies can re-collide while a create POST is still in
    # flight, producing duplicate ongoing encounters for the same (run_id, wild).
    # If an active (ongoing) encounter already exists for this run+wild, return it
    # instead of creating a second one. Only meaningful when a wild_id is given
    # (otherwise each call is an intentional fresh random encounter).
    if req.wild_id:
        existing = await _find_active_encounter(session, req.run_id, req.wild_id)
        if existing is not None:
            return await build_encounter_state(existing.id)

    party_monsters = await _resolve_party(session, req.run_id)
    enemy_monsters = await _resolve_enemies(session, req.run_id, req)

    party = [_to_combatant(m, "party") for m in party_monsters]
    enemies = [_to_combatant(m, "enemy") for m in enemy_monsters]
    combatants = party + enemies

    # Topic is randomized PER BATTLE within the run's THEME. The player picks a
    # theme at run start; each battle draws a random topic inside it. Unknown/
    # empty theme falls back to the full catalog (pick_random_topic never raises).
    from app.debate.topics import pick_random_topic

    eid = str(uuid.uuid4())
    # Seed off the encounter id so the per-battle topic is stable on retry but
    # still varies across battles within the run/theme.
    topic_seed = uuid.UUID(eid).int & 0x7FFFFFFF
    topic = pick_random_topic(seed=topic_seed, theme=getattr(run, "theme", None))
    enc = Encounter(
        id=eid,
        run_id=req.run_id,
        topic=topic,
        enemy_ids=[m.id for m in enemy_monsters],
        party_ids=[m.id for m in party_monsters],
        result=EncounterResult.ongoing,
    )
    session.add(enc)
    await session.commit()

    await seed_redis(eid, req.run_id, topic, combatants)
    await set_meta(eid, phase="debating")

    # Latency fix + A1/A2 opening pre-gen, all on ONE background task so the single
    # Ollama slot is never double-hit.
    #   A1 — prewarm_models(topic, enemy_model) folds the opening pre-gen for THIS
    #        battle's drawn topic INTO the prewarm task (it runs the pregen first,
    #        then the throwaway warm-ups), so the first enemy turn is a pure cache
    #        retrieval during the encounter-load idle window.
    #   A2 — pregenerate_theme_openings warms EVERY topic in the run's theme so the
    #        NEXT battle (which draws a different random topic within the same theme,
    #        see topic_seed above) also hits the cache. The per-battle topic is
    #        seeded off the encounter UUID, so warming only the single drawn topic
    #        would almost never hit again — pre-baking the whole theme is what makes
    #        the cache actually pay off across a run.
    # Both are awaited SEQUENTIALLY inside one task: never two concurrent calls to
    # the single Ollama slot.
    try:
        from app.debate.materialize import pregenerate_theme_openings
        from app.debate.orchestrator import prewarm_models

        enemy_model = next((m.model for m in enemy_monsters if m.model), None)
        theme = getattr(run, "theme", None)

        async def _warm() -> None:
            # This-battle opening first (lowest-latency win), folded into prewarm.
            await prewarm_models(topic=topic, enemy_model=enemy_model)
            # Then warm the rest of the theme for subsequent battles (one topic at
            # a time, so still no concurrent slot pressure).
            await pregenerate_theme_openings(theme, enemy_model)

        asyncio.create_task(_warm())
    except Exception:  # noqa: BLE001 — prewarm is best-effort, never block encounter creation
        pass

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
