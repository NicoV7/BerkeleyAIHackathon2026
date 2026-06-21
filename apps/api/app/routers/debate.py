"""Debate router (WS-B): advance the turn-based battle.

POST /api/encounters/{id}/turn    run ONE round, return TurnResult.
POST /api/encounters/{id}/auto    run N rounds (or until a side falls).
WS   /api/encounters/{id}/stream  stream utterance/verdict/hp/phase events live.
POST /api/encounters/{id}/flee    end the encounter as a flee.

All paths drive the same engine (orchestrator.run_round_stream) over the Redis
state seeded by the encounter router, and finalize to Postgres idempotently on
win/loss/flee.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Encounter, EncounterResult, SummonItem
from app.db.session import SessionLocal, get_session
from app.debate.orchestrator import _active_party, _lead, run_human_round_stream, run_round_stream
from app.debate.skill_engine import skill_cost, skill_metadata
from app.redis_state import (
    append_effect,
    append_utterance,
    get_mp_map,
    get_transcript,
    k_judge,
    k_meta,
    set_hp,
    set_mp,
)
from app.routers.encounter import (
    build_encounter_state,
    get_meta,
    load_combatants,
    load_momentum,
    set_meta,
)
from app.schemas import (
    AssistRequest,
    AssistResult,
    AutoRequest,
    JudgeVerdict,
    MemoryRecallResult,
    PlayerArgueRequest,
    TurnResult,
    Utterance,
)

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/encounters", tags=["debate"])

# Wall-clock guards: a single round shouldn't run forever on a slow local model,
# and /auto shouldn't keep starting rounds past a total budget.
ROUND_TIMEOUT_S = 120.0
AUTO_BUDGET_S = 240.0


async def _check_skill_mp(
    eid: str,
    skill_id: str | None,
    actor_id: str | None = None,
) -> None:
    """Reject the request with 400 when the lead party monster can't afford the skill.

    Gacha Wave B: cheap pre-flight on every /argue + /assist (and the WS argue
    action). The cost is read from the .md front-matter via ``skill_cost``;
    current MP is read from ``enc:{eid}:mp``. Free skills (cost 0) and the
    "no skill selected" case are no-ops. Lead-party MP is resolved through the
    same path the orchestrator uses so the gate is consistent. Best-effort: any
    failure (missing Redis key, missing combatants) leaves the request to the
    orchestrator's own defensive check.
    """
    if not skill_id:
        return
    cost = skill_cost(skill_id)
    if cost <= 0:
        return
    try:
        from app.routers.encounter import load_combatants

        combatants = await load_combatants(eid)
        actor = _active_party(combatants, actor_id)
        if actor is None:
            return
        mp_map = await get_mp_map(eid)
        # Default to the combatant's max_mp so a fresh encounter (cache miss)
        # still validates against a real ceiling.
        cur = int(mp_map.get(actor.monster_id, actor.max_mp))
        if cur < cost:
            raise HTTPException(
                status_code=400,
                detail=f"insufficient MP for {skill_id}: {cur}/{cost}",
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — never 500 on a defensive gate
        return


def _skill_effect_payload(
    *,
    skill: dict[str, Any],
    source: Any,
    target: Any,
    turn_no: int,
    message: str,
) -> dict[str, Any]:
    """Build the visible WS payload for a non-advancing skill."""
    return {
        "skill_id": skill.get("name"),
        "skill_name": skill.get("name"),
        "effect_kind": skill.get("effect_kind", "intel_preview"),
        "source_id": getattr(source, "monster_id", None),
        "source_name": getattr(source, "name", "Agent"),
        "target_id": getattr(target, "monster_id", None),
        "target_name": getattr(target, "name", "Opponent"),
        "duration_turns": int(skill.get("duration_turns", 0) or 0),
        "turn_no": turn_no,
        "message": message,
        "modifiers": skill.get("modifiers") if isinstance(skill.get("modifiers"), dict) else {},
        "server_ts": time.time(),
    }


def _intel_text(topic: str, enemy: Any, transcript: list[dict], skill: dict[str, Any]) -> str:
    """Return a fast tactical read for the enemy's likely next angle."""
    enemy_type = str(getattr(enemy, "type", "") or "").upper()
    type_angle = {
        "LOGOS": "They will likely press evidence quality, causal links, or missing warrants.",
        "PATHOS": "They will likely recenter human stakes and accuse your side of ignoring harm.",
        "ETHOS": "They will likely attack credibility, expertise, or who deserves trust.",
        "CHAOS": "They will likely scramble the frame and make your premise look brittle.",
        "SOCRATIC": "They will likely ask a narrowing question that forces a concession.",
        "RHETORIC": "They will likely win rhythm and framing with a memorable contrast.",
    }.get(enemy_type, "They will likely answer the latest claim and press one concrete weakness.")
    latest = ""
    for utt in reversed(transcript or []):
        if utt.get("actor_role") == "enemy" and utt.get("text"):
            latest = str(utt["text"]).strip()
            break
    counter = skill.get("modifiers", {}).get("angle") if isinstance(skill.get("modifiers"), dict) else ""
    counter_text = str(counter or "Answer with one concrete example before they widen the frame.")
    last_line = f" Last enemy line to watch: \"{latest[:140]}\"" if latest else ""
    return f"{type_angle} Counter-plan for '{topic}': {counter_text}.{last_line}"


async def _invoke_non_advancing_skill(
    eid: str,
    skill_id: str | None,
    actor_id: str | None,
) -> list[dict[str, Any]]:
    """Spend MP for a non-turn skill and return WS messages to emit."""
    if not skill_id:
        raise HTTPException(status_code=400, detail="skill_id is required")
    spec = skill_metadata(skill_id)
    if not spec or spec.get("effect_kind") != "intel_preview":
        raise HTTPException(status_code=400, detail=f"{skill_id} is not a preview skill")

    await _check_skill_mp(eid, skill_id, actor_id)
    meta = await get_meta(eid)
    combatants = await load_combatants(eid)
    actor = _active_party(combatants, actor_id)
    enemy = _lead(combatants, "enemy")
    if actor is None or enemy is None:
        raise HTTPException(status_code=409, detail="no active party/enemy for skill")

    events: list[dict[str, Any]] = []
    cost = skill_cost(skill_id)
    if cost > 0:
        mp_map = await get_mp_map(eid)
        cur_mp = int(mp_map.get(actor.monster_id, actor.max_mp))
        next_mp = max(0, cur_mp - cost)
        await set_mp(eid, actor.monster_id, next_mp)
        events.append(
            {
                "type": "mp",
                "data": {
                    "monster_id": actor.monster_id,
                    "mp": next_mp,
                    "max_mp": actor.max_mp,
                    "server_ts": time.time(),
                },
            }
        )

    turn_no = int(meta.get("turn_no", 0) or 0)
    effect = _skill_effect_payload(
        skill=spec,
        source=actor,
        target=enemy,
        turn_no=turn_no,
        message=f"{actor.name} reads {enemy.name}'s next angle with {skill_id}.",
    )
    await append_effect(eid, effect)
    transcript = await get_transcript(eid)
    events.append({"type": "skill_effect", "data": effect})
    events.append(
        {
            "type": "intel_preview",
            "data": {
                "skill_id": skill_id,
                "source_id": actor.monster_id,
                "target_id": enemy.monster_id,
                "turn_no": turn_no,
                "preview": _intel_text(meta.get("topic", ""), enemy, transcript, spec),
                "server_ts": time.time(),
            },
        }
    )
    return events


# ---- Finalize (idempotent) --------------------------------------------------


async def _finalize(eid: str, result: EncounterResult) -> list[dict]:
    """Persist the battle durably, write per-party BATTLE memories, then evict
    the conversation from Redis. Idempotent: a second call is a no-op.

    Order matters: we read the transcript/verdicts/HP out of Redis FIRST, snapshot
    them onto the Encounter row and into each party member's memory (so GEPA/RAG
    have real battle data that outlives the cache), and only then evict the heavy
    Redis keys to avoid context pollution.

    Returns a list of LevelUp event payloads (one per party monster that
    levelled this finalize). Callers that have a WS open (the /stream handler)
    forward these as ``{"type": "LevelUp", ...}`` messages so the frontend
    cinematic fires; REST callers may safely ignore the return value. A second
    (idempotent) finalize returns ``[]``.
    """
    from app.redis_state import clear_conversation, get_hp_map, get_redis, k_judge

    transcript = await get_transcript(eid)
    r = get_redis()
    verdicts = [json.loads(v) for v in await r.lrange(k_judge(eid), 0, -1)]
    final_hp = await get_hp_map(eid)
    meta = await r.hgetall(k_meta(eid))
    topic = meta.get("topic", "")
    roster = json.loads(meta.get("combatants", "[]"))

    level_up_events: list[dict] = []

    async with SessionLocal() as session:
        enc = await session.get(Encounter, eid)
        if not enc:
            return []
        if enc.result != EncounterResult.ongoing:
            return []  # already finalized
        enc.result = result
        enc.transcript = transcript
        enc.verdicts = verdicts
        enc.final_hp = final_hp
        enc.transcript_ref = None
        session.add(enc)
        await session.commit()

        # Write one BATTLE memory per PARTY member (durable, embedded for RAG +
        # available to GEPA training). Best-effort: never let this fail the battle.
        await _write_battle_memories(session, enc, topic, transcript, final_hp, roster, result)

        # Wave A: on a win, roll for a SummonItem drop. The TurnResult schema
        # does not carry this field today, so we only log it — the frontend
        # picks the drop up via `GET /api/runs/{id}/summons`.
        if result == EncounterResult.win:
            dropped = await _maybe_drop_summon_item(session, enc.run_id)
            if dropped is not None:
                log.info(
                    "gacha drop: encounter=%s run=%s item=%s tier=%s",
                    eid, enc.run_id, dropped.id, dropped.tier,
                )

        # WS-1 economy: award coins on a win / capture. This is guarded by the
        # SAME idempotency as everything above — the `enc.result != ongoing`
        # early-return at the top means a retried finalize never double-awards.
        # The credit is an atomic in-place SQL UPDATE on the run row (no
        # read-then-write) so it composes with the wallet/shop debit path.
        if result in (EncounterResult.win, EncounterResult.capture):
            awarded = await _award_coins(session, enc, roster, result)
            if awarded:
                log.info(
                    "economy: coins awarded encounter=%s run=%s amount=%d",
                    eid, enc.run_id, awarded,
                )

        # Wave D: award XP + apply per-level stat gains. Best-effort —
        # progression must never block finalize. Each party monster that
        # levels emits a LevelUp event the WS /stream handler forwards to the
        # frontend cinematic.
        try:
            level_up_events = await _award_party_xp(session, enc, roster, result)
        except Exception:  # noqa: BLE001
            level_up_events = []

    # WS-2 living layer: on a win, emit an ``enemy_killed`` world event for each
    # defeated enemy so ``hunt_enemy`` quests complete (and pay out). Best-effort:
    # the quest/event log is decoupled from the battle, so a failure here never
    # affects the finalize result. Done OUTSIDE the DB session block above — the
    # event log is Redis-backed and quest rewards open their own session.
    if result == EncounterResult.win:
        try:
            await _emit_enemy_killed(enc.run_id, roster)
        except Exception:  # noqa: BLE001 — world-event emit must never break finalize
            pass

    # Conversation is durably stored — free the cache.
    await clear_conversation(eid)
    return level_up_events


async def _emit_enemy_killed(run_id: str, roster: list[dict]) -> None:
    """Emit ``enemy_killed`` for every enemy combatant, completing hunt quests.

    Each enemy combatant fires one event carrying both ``enemy_kind`` (the
    monster name, so a quest can target a kind) and ``monster_id`` (so a quest
    can target a specific spawned enemy). Quest completion + reward payout reuse
    the SAME helpers the world router uses, so there is no duplicated logic.
    """
    from app.world import event_log, quests

    enemies = [c for c in roster if c.get("role") == "enemy"]
    if not enemies:
        return
    for enemy in enemies:
        name = str(enemy.get("name") or enemy.get("monster_id") or "")
        monster_id = str(enemy.get("monster_id") or "")
        await event_log.append(
            run_id, "enemy_killed", enemy_kind=name, monster_id=monster_id
        )
        completed = await quests.maybe_complete_quests(
            run_id, "enemy_killed", enemy_kind=name, monster_id=monster_id
        )
        if completed:
            await _payout_quest_rewards(run_id, completed)


async def _payout_quest_rewards(run_id: str, completed: list[str]) -> None:
    """Pay coins/items for newly-completed quests via the economy award helper.

    Opens its own short-lived session (the finalize session is already closed by
    the time we emit world events) and commits once. Best-effort.
    """
    if not completed:
        return
    try:
        from app.economy.award import award as award_reward
        from app.world import quests

        specs = await quests.completed_reward_specs(run_id, completed)
        async with SessionLocal() as session:
            for qid in completed:
                spec = specs.get(qid) or {}
                if spec:
                    await award_reward(session, run_id, spec)
            await session.commit()
    except Exception as e:  # noqa: BLE001
        log.info("quest reward payout skipped (%s)", e)


async def _award_party_xp(
    session: AsyncSession,
    enc: Encounter,
    roster: list[dict],
    result: EncounterResult,
) -> list[dict]:
    """Apply XP rewards to every party monster and emit LevelUp events.

    XP scales with the highest enemy level in the roster (so a tougher fight
    gives more XP). A win pays full reward, a loss/flee pays the consolation
    multiplier — both routes through ``balance.xp_reward``. The dict returned
    by ``progress.award_xp`` is normalized into a WS-friendly LevelUp payload
    only when the monster actually levelled.
    """
    from app.party import balance, progress

    # Highest enemy level drives the reward curve; default to 1 if no enemies
    # are recorded in the roster (defensive).
    enemy_level = 1
    for c in roster:
        if c.get("role") == "enemy":
            try:
                lvl = int(c.get("level", 1) or 1)
                if lvl > enemy_level:
                    enemy_level = lvl
            except (TypeError, ValueError):
                continue

    won = result == EncounterResult.win
    amount = balance.xp_reward(enemy_level, won=won)
    if amount <= 0:
        return []

    from app.db.models import Monster

    events: list[dict] = []
    for pid in enc.party_ids or []:
        m = await session.get(Monster, pid)
        if m is None:
            continue
        outcome = progress.award_xp(session, m, amount)
        if not outcome.get("levelled"):
            continue
        events.append(
            {
                "type": "LevelUp",
                "monster_id": m.id,
                "new_level": int(outcome.get("new_level", m.level)),
                "stat_gains": outcome.get(
                    "stat_gains", {"atk": 0, "def": 0, "mp": 0, "hp": 0}
                ),
            }
        )

    try:
        await session.commit()
    except Exception:  # noqa: BLE001 — never let a commit failure 500 finalize
        await session.rollback()
        return []
    return events


# ---- Wave A: SummonItem post-battle drop -----------------------------------


def _gacha_drop_rate() -> float:
    """Drop rate is env-tunable (`GACHA_DROP_RATE`) with a 0.30 default."""
    raw = os.getenv("GACHA_DROP_RATE", "0.30")
    try:
        rate = float(raw)
    except ValueError:
        return 0.30
    return max(0.0, min(1.0, rate))


# Tier weights for the post-battle drop. Slightly stingier than the starter
# pull — most drops are common, legendary is a rare treat.
_DROP_TIER_WEIGHTS: dict[str, int] = {"common": 80, "rare": 18, "legendary": 2}


def _roll_drop_tier(rng: random.Random) -> str:
    total = sum(_DROP_TIER_WEIGHTS.values())
    pick = rng.uniform(0, total)
    acc = 0.0
    for tier, w in _DROP_TIER_WEIGHTS.items():
        acc += w
        if pick <= acc:
            return tier
    return "common"


# ---- WS-1: coin award on win / capture -------------------------------------

# Coin reward scaled by result and the toughest enemy faced. Env-tunable base so
# balancing is a one-line change; defaults give ~30 coins for a win, ~50 for a
# capture, plus a small per-enemy-level bonus.
_COIN_BASE_WIN = int(os.getenv("ECON_COIN_BASE_WIN", "30"))
_COIN_BASE_CAPTURE = int(os.getenv("ECON_COIN_BASE_CAPTURE", "50"))
_COIN_PER_ENEMY_LEVEL = int(os.getenv("ECON_COIN_PER_ENEMY_LEVEL", "5"))


def _coin_reward(roster: list[dict], result: EncounterResult) -> int:
    """Pure reward curve: base (by result) + per-enemy-level bonus."""
    enemy_level = 1
    for c in roster:
        if c.get("role") == "enemy":
            try:
                lvl = int(c.get("level", 1) or 1)
            except (TypeError, ValueError):
                continue
            enemy_level = max(enemy_level, lvl)
    base = _COIN_BASE_CAPTURE if result == EncounterResult.capture else _COIN_BASE_WIN
    return max(0, base + _COIN_PER_ENEMY_LEVEL * (enemy_level - 1))


async def _award_coins(
    session: AsyncSession,
    enc: Encounter,
    roster: list[dict],
    result: EncounterResult,
) -> int:
    """Credit coins to the run's wallet via an atomic in-place SQL UPDATE.

    No read-then-write — ``coins = coins + :amt`` composes safely with the
    wallet/shop debit path in the economy router. Idempotency is inherited from
    the caller's ``enc.result != ongoing`` guard (a retried finalize returns
    early before reaching here), so the credit fires exactly once per battle.
    Best-effort: a failure here never breaks finalize. Returns coins awarded.
    """
    from sqlalchemy import text

    amount = _coin_reward(roster, result)
    if amount <= 0:
        return 0
    try:
        await session.execute(
            text("UPDATE runs SET coins = coins + :amt WHERE id = :rid"),
            {"amt": amount, "rid": enc.run_id},
        )
        await session.commit()
        return amount
    except Exception:  # noqa: BLE001 — never let a coin credit 500 finalize
        await session.rollback()
        return 0


async def _maybe_drop_summon_item(
    session: AsyncSession, run_id: str
) -> SummonItem | None:
    """Roll the drop chance; on a hit, persist a SummonItem and return it."""
    try:
        rng = random.Random()
        if rng.random() > _gacha_drop_rate():
            return None
        item = SummonItem(run_id=run_id, tier=_roll_drop_tier(rng), consumed=False)
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item
    except Exception as e:  # noqa: BLE001
        log.info("gacha drop: skipped (%s)", e)
        return None


async def _write_battle_memories(
    session: AsyncSession,
    enc: Encounter,
    topic: str,
    transcript: list[dict],
    final_hp: dict[str, int],
    roster: list[dict],
    result: EncounterResult,
) -> None:
    try:
        from app.memory.store import write_event  # WS-D seam (optional)
    except Exception:  # noqa: BLE001
        return

    names = {c["monster_id"]: c.get("name", c["monster_id"]) for c in roster}
    outcome = {
        EncounterResult.win: "Your side WON the debate.",
        EncounterResult.loss: "Your side LOST the debate.",
        EncounterResult.flee: "You fled the debate.",
    }.get(result, "The debate ended.")

    # Render the full conversation once, labeled by speaker.
    debate_lines = [u for u in transcript if not u.get("reaction_state")]
    lines = [f"{names.get(u['actor_id'], u['actor_role'])}: {u['text']}" for u in debate_lines]
    convo = "\n".join(lines)

    for pid in (enc.party_ids or []):
        my_hp = final_hp.get(pid)
        my_lines = [u["text"] for u in debate_lines if u["actor_id"] == pid]
        content = (
            f"Debate on '{topic}'. {outcome} "
            f"Final HP: {my_hp}. My arguments: " + " | ".join(my_lines) +
            f"\n\nFull transcript:\n{convo}"
        )
        try:
            await write_event(
                session,
                monster_id=pid,
                run_id=enc.run_id,
                event_type="BATTLE",
                content=content,
                encounter_id=enc.id,
                salience=0.8 if result in (EncounterResult.win, EncounterResult.loss) else 0.4,
            )
        except Exception:  # noqa: BLE001
            continue


_PHASE_TO_RESULT = {
    "won": EncounterResult.win,
    "lost": EncounterResult.loss,
}


# ---- One round (collect events) --------------------------------------------


async def _run_one_round(eid: str) -> tuple[list[Utterance], list[JudgeVerdict], dict]:
    meta = await get_meta(eid)
    phase = meta.get("phase", "debating")
    if phase in ("won", "lost"):
        raise HTTPException(status_code=409, detail=f"encounter already {phase}")

    topic = meta.get("topic", "")
    run_id = meta.get("run_id", "")
    start_turn = int(meta.get("turn_no", 0) or 0)
    combatants = await load_combatants(eid)
    momentum = await load_momentum(eid)

    new_utts: list[Utterance] = []
    new_verdicts: list[JudgeVerdict] = []
    phase_event: dict = {"phase": "debating", "capturable_ids": [], "turn_no": start_turn}

    async for ev in run_round_stream(
        eid, topic, combatants, run_id, start_turn, momentum
    ):
        if ev.kind == "utterance":
            new_utts.append(Utterance(**_utt_fields(ev.data)))
        elif ev.kind == "verdict":
            new_verdicts.append(_to_verdict(ev.data))
        elif ev.kind == "phase":
            phase_event = ev.data

    # Persist meta (turn_no, phase, momentum).
    from app.redis_state import get_redis, k_momentum

    r = get_redis()
    await r.hset(
        k_momentum(eid),
        mapping={k: str(v) for k, v in momentum.items()},
    )
    await set_meta(eid, turn_no=phase_event.get("turn_no", start_turn), phase=phase_event["phase"])

    if phase_event["phase"] in _PHASE_TO_RESULT:
        await _finalize(eid, _PHASE_TO_RESULT[phase_event["phase"]])

    return new_utts, new_verdicts, phase_event


async def _run_one_human_round(
    eid: str,
    player_text: str,
    skill_id: str | None,
    actor_id: str | None = None,
) -> tuple[list[Utterance], list[JudgeVerdict], dict]:
    meta = await get_meta(eid)
    phase = meta.get("phase", "debating")
    if phase in ("won", "lost"):
        raise HTTPException(status_code=409, detail=f"encounter already {phase}")

    topic = meta.get("topic", "")
    run_id = meta.get("run_id", "")
    start_turn = int(meta.get("turn_no", 0) or 0)
    combatants = await load_combatants(eid)
    momentum = await load_momentum(eid)

    new_utts: list[Utterance] = []
    new_verdicts: list[JudgeVerdict] = []
    phase_event: dict = {"phase": "debating", "capturable_ids": [], "turn_no": start_turn}

    async for ev in run_human_round_stream(
        eid,
        topic,
        combatants,
        run_id,
        start_turn,
        momentum,
        player_text,
        skill_id,
        active_party_id=actor_id,
    ):
        if ev.kind == "utterance":
            new_utts.append(Utterance(**_utt_fields(ev.data)))
        elif ev.kind == "verdict":
            new_verdicts.append(_to_verdict(ev.data))
        elif ev.kind == "phase":
            phase_event = ev.data

    from app.redis_state import get_redis, k_momentum

    r = get_redis()
    await r.hset(k_momentum(eid), mapping={k: str(v) for k, v in momentum.items()})
    await set_meta(eid, turn_no=phase_event.get("turn_no", start_turn), phase=phase_event["phase"])

    if phase_event["phase"] in _PHASE_TO_RESULT:
        await _finalize(eid, _PHASE_TO_RESULT[phase_event["phase"]])

    return new_utts, new_verdicts, phase_event


def _utt_fields(d: dict) -> dict:
    return {
        "turn": d["turn"],
        "actor_id": d["actor_id"],
        "actor_role": d["actor_role"],
        "skill_used": d.get("skill_used"),
        "text": d["text"],
        "ts": d["ts"],
        "server_ts": d.get("server_ts"),
        "elapsed_ms": d.get("elapsed_ms"),
        "reaction_state": d.get("reaction_state"),
    }


def _to_verdict(d: dict) -> JudgeVerdict:
    # Additive fields from the Wave-1 WS-1 judge expansion (commit 352f8f4):
    # `why` is the hero-banner one-liner, `logic`/`persuasion` are sub-scores,
    # and `actor_id` ties the verdict back to the speaker being judged.
    # All four are Optional in the schema so older persisted verdicts still load.
    return JudgeVerdict(
        turn=d["turn"],
        target=d["target"],
        score=d["score"],
        rationale=d["rationale"],
        damage=d["damage"],
        why=d.get("why"),
        logic=d.get("logic"),
        persuasion=d.get("persuasion"),
        actor_id=d.get("actor_id"),
    )


# ---- Endpoints --------------------------------------------------------------


@router.post("/{eid}/turn", response_model=TurnResult)
async def take_turn(eid: str, session: AsyncSession = Depends(get_session)) -> TurnResult:
    try:
        new_utts, new_verdicts, phase_event = await asyncio.wait_for(
            _run_one_round(eid), timeout=ROUND_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="round timed out") from None
    state = await build_encounter_state(eid)
    return TurnResult(
        encounter=state,
        new_utterances=new_utts,
        new_verdicts=new_verdicts,
        capturable_ids=phase_event.get("capturable_ids", []),
    )


@router.post("/{eid}/auto", response_model=TurnResult)
async def auto(
    eid: str, req: AutoRequest, session: AsyncSession = Depends(get_session)
) -> TurnResult:
    all_utts: list[Utterance] = []
    all_verdicts: list[JudgeVerdict] = []
    capturable: list[str] = []
    rounds = max(1, min(req.rounds, 12))
    deadline = time.monotonic() + AUTO_BUDGET_S
    for _ in range(rounds):
        meta = await get_meta(eid)
        if meta.get("phase") in ("won", "lost"):
            break
        # Stop starting new rounds once the wall-clock budget is spent.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            utts, verdicts, phase_event = await asyncio.wait_for(
                _run_one_round(eid), timeout=min(ROUND_TIMEOUT_S, remaining)
            )
        except asyncio.TimeoutError:
            break  # return progress so far rather than hang the request
        all_utts.extend(utts)
        all_verdicts.extend(verdicts)
        capturable = phase_event.get("capturable_ids", [])
        if phase_event["phase"] in ("won", "lost"):
            break
    state = await build_encounter_state(eid)
    return TurnResult(
        encounter=state,
        new_utterances=all_utts,
        new_verdicts=all_verdicts,
        capturable_ids=capturable,
    )


@router.post("/{eid}/argue", response_model=TurnResult)
async def argue(
    eid: str, req: PlayerArgueRequest, session: AsyncSession = Depends(get_session)
) -> TurnResult:
    """Human-argues (WS-G): the player's typed argument is the lead party monster's
    turn; the lead enemy rebuts autonomously. REST fallback for the WS argue action."""
    # Gacha Wave B: reject up front when the player can't afford the chosen skill,
    # so the textarea + skill picker know to refund the MP / re-enable the button
    # before any LLM call burns latency.
    await _check_skill_mp(eid, req.skill_id, req.actor_id)
    try:
        new_utts, new_verdicts, phase_event = await asyncio.wait_for(
            _run_one_human_round(eid, req.text, req.skill_id, req.actor_id), timeout=ROUND_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="round timed out") from None
    state = await build_encounter_state(eid)
    return TurnResult(
        encounter=state,
        new_utterances=new_utts,
        new_verdicts=new_verdicts,
        capturable_ids=phase_event.get("capturable_ids", []),
    )


@router.post("/{eid}/assist", response_model=AssistResult)
async def assist(
    eid: str, req: AssistRequest, session: AsyncSession = Depends(get_session)
) -> AssistResult:
    """ARGUE COPILOT (player-first pivot): the lead party monster COACHES the
    player's drafted argument into a stronger one.

    This does NOT advance the round — it only suggests. The player then sends the
    improved text via the existing POST /{eid}/argue. The coach's quality is driven
    by the monster's TRAINED genome, so training the monster improves the help.

    404 if the encounter is missing. Wrapped in the same wall-clock guard as
    /argue; on timeout or any failure the coach degrades gracefully rather than
    500-ing.
    """
    await get_meta(eid)  # 404 if encounter missing
    # Gacha Wave B: same MP gate as /argue — the coach is allowed to suggest a
    # different skill, but if the player explicitly picked one they can't afford
    # we reject so the UI can refund the picker before the LLM call begins.
    await _check_skill_mp(eid, req.skill_id, None)
    from app.debate.coach import coach_argument

    try:
        return await asyncio.wait_for(
            coach_argument(session, eid, req.draft, req.skill_id),
            timeout=ROUND_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # The coach itself never raises; a timeout here means the model stalled.
        # Degrade to a graceful fallback so the player is never blocked.
        from app.debate.coach import _fallback_suggestion

        return AssistResult(
            encounter_id=eid,
            coach_monster_id=None,
            suggestions=[_fallback_suggestion(req.draft, "", req.skill_id)],
        )


# ---- Memory Recall (Wave C: the headline ability) --------------------------
#
# `POST /api/encounters/{eid}/memory-recall` lets the lead party monster *peek*
# the shared Redis transcript, quote an enemy line back word-for-word, and
# counter it in its own persona voice. Damage flows through the same
# `compute_damage` formula every other turn uses (so type chart, ATK/DEF
# ratio, level scaling, and `domain_match_mult` all still apply); the score is
# a fixed 80 baseline because this is an ability, not a judged turn.
#
# Hard contract:
#   * MP cost 60. Reads from `enc:{eid}:mp`; falls back to `monster.max_mp`
#     when the hash is empty (Wave B may not have populated it yet — keeps the
#     demo playable before integration).
#   * Cache miss / empty transcript / model timeout -> generic counter, half
#     MP refund (30), damage = 0. Never 500s.
#   * Writes the counter as a new Utterance to the transcript and the damage
#     to `enc:{eid}:hp` so the regular HP-update path observes it next round.

MEMORY_RECALL_MP_COST = 60
MEMORY_RECALL_MP_REFUND = 30  # half-cost refund on graceful fallback
MEMORY_RECALL_SCORE = 80.0  # fixed baseline through compute_damage
MEMORY_RECALL_SKILL_MULT = 1.6  # mirrors `power:` in memory_recall.md
MEMORY_RECALL_MODEL = "gemma3:1b"
MEMORY_RECALL_MAX_TOKENS = 150
MEMORY_RECALL_TEMPERATURE = 0.7
MEMORY_RECALL_TIMEOUT_S = 20.0
MEMORY_RECALL_TRANSCRIPT_SLICE = 5


def _voice_of(combatant: Any) -> str:  # type: ignore[name-defined]
    """Pick the lead monster's persona voice for the counter prompt.

    Prefer `persona.voice` (set by Wave A's Wikipedia hydration), fall back to
    `persona.tagline` (seed catalog default), then to the monster's name — so
    the prompt is always grounded even before hydration completes.
    """
    persona = getattr(combatant, "persona", None) or {}
    if isinstance(persona, dict):
        voice = persona.get("voice") or persona.get("tagline")
        if voice:
            return str(voice)
    return getattr(combatant, "name", "your monster") or "your monster"


def _pick_highlighted_line(
    transcript: list[dict], judge_verdicts: list[dict], party_ids: set[str]
) -> str:
    """Pick the enemy utterance to quote back.

    Prefer the enemy line whose verdict dealt the highest damage TO a party
    monster; fall back to the most-recent enemy utterance; finally to the last
    line of any kind. Defensive — never raises.
    """
    # Highest-damage path: walk verdicts in damage-desc order, return the
    # corresponding utterance text when target is on the party side.
    if judge_verdicts:
        try:
            ranked = sorted(
                (v for v in judge_verdicts if v.get("target") in party_ids),
                key=lambda v: float(v.get("damage", 0) or 0),
                reverse=True,
            )
        except Exception:  # noqa: BLE001
            ranked = []
        for v in ranked:
            turn = v.get("turn")
            actor = v.get("actor_id")
            # Match the enemy utterance at the same turn (the speaker is the
            # actor_id; for human rounds the verdict records `actor_id`).
            for u in transcript:
                if (
                    u.get("turn") == turn
                    and u.get("actor_role") == "enemy"
                    and u.get("text")
                ):
                    return str(u["text"]).strip()
                if actor and u.get("actor_id") == actor and u.get("text"):
                    return str(u["text"]).strip()

    # Fallback: most recent enemy utterance.
    for u in reversed(transcript or []):
        if u.get("actor_role") == "enemy" and u.get("text"):
            return str(u["text"]).strip()

    # Final fallback: the absolute last line (could be a judge or party line).
    for u in reversed(transcript or []):
        if u.get("text"):
            return str(u["text"]).strip()
    return ""


def _transcript_slice(transcript: list[dict], n: int = 5) -> list[str]:
    """Render the last N transcript lines as `"<actor_role>: <text>"` strings."""
    out: list[str] = []
    for u in transcript[-n:]:
        if u.get("reaction_state"):
            continue
        role = u.get("actor_role") or "?"
        text = (u.get("text") or "").strip()
        if not text:
            continue
        out.append(f"{role}: {text}")
    return out


@router.post("/{eid}/memory-recall", response_model=MemoryRecallResult)
async def memory_recall(
    eid: str, session: AsyncSession = Depends(get_session)
) -> MemoryRecallResult:
    """Memory Recall ability — peek the Redis transcript, quote the enemy back.

    See the module-level comment above for the full contract. Never 500s: any
    cache miss, empty transcript, or model failure degrades to a generic counter
    with a half-MP refund and `damage=0`.
    """
    # ---- Load encounter state ------------------------------------------------
    meta = await get_meta(eid)  # 404 if encounter missing
    phase = meta.get("phase", "debating")
    if phase in ("won", "lost"):
        raise HTTPException(status_code=409, detail=f"encounter already {phase}")
    topic = meta.get("topic", "") or ""
    combatants = await load_combatants(eid)

    coach = _lead(combatants, "party")
    enemy = _lead(combatants, "enemy")
    if coach is None or enemy is None:
        raise HTTPException(status_code=409, detail="no lead party/enemy to recall against")

    # ---- MP gate (Wave B may not have populated `enc:{eid}:mp` yet) ---------
    # If the hash is empty (or missing this combatant), default to the
    # monster's max_mp so the demo works even before Wave B integrates the MP
    # economy. Once Wave B writes initial MP at encounter start, this branch
    # naturally cedes to the real value.
    mp_map = await get_mp_map(eid)
    monster_max_mp = int(
        getattr(coach, "max_mp", None)
        or getattr(coach, "max_hp", None)  # extreme fallback for legacy combatants
        or MEMORY_RECALL_MP_COST
    )
    mp_before = int(mp_map.get(coach.monster_id, monster_max_mp))
    if mp_before < MEMORY_RECALL_MP_COST:
        raise HTTPException(
            status_code=400,
            detail=f"not enough MP for Memory Recall ({mp_before}/{MEMORY_RECALL_MP_COST})",
        )

    # ---- Pick the line to quote --------------------------------------------
    transcript = await get_transcript(eid)
    judge_verdicts: list[dict] = []
    try:
        from app.redis_state import get_redis

        r = get_redis()
        raw = await r.lrange(k_judge(eid), 0, -1)
        for blob in raw:
            try:
                judge_verdicts.append(json.loads(blob))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        judge_verdicts = []

    party_ids = {c.monster_id for c in combatants if c.role == "party"}
    highlighted_line = _pick_highlighted_line(transcript, judge_verdicts, party_ids)
    voice = _voice_of(coach)

    # ---- Cache-miss fallback (no usable line) ------------------------------
    if not highlighted_line:
        return await _memory_recall_fallback(
            eid=eid,
            coach=coach,
            voice=voice,
            mp_before=mp_before,
            transcript=transcript,
            highlighted_line="",
        )

    # ---- Build the counter prompt ------------------------------------------
    user_prompt = (
        f"You are {coach.name}, a debater with this voice: {voice}.\n"
        f'The enemy said: "{highlighted_line}"\n'
        "In one sentence, counter this specific argument and explicitly quote or\n"
        "reference their words back to them. Be sharp and in-character."
    )

    # ---- Generate (defensively) --------------------------------------------
    counter_text: str = ""
    try:
        from app.gateway.gateway import gateway

        raw_counter = await asyncio.wait_for(
            gateway.complete(
                [{"role": "user", "content": user_prompt}],
                model=MEMORY_RECALL_MODEL,
                temperature=MEMORY_RECALL_TEMPERATURE,
                max_tokens=MEMORY_RECALL_MAX_TOKENS,
                timeout=MEMORY_RECALL_TIMEOUT_S,
            ),
            timeout=MEMORY_RECALL_TIMEOUT_S + 2.0,
        )
        counter_text = (raw_counter or "").strip()
    except Exception:  # noqa: BLE001
        counter_text = ""

    # On a model timeout/failure: graceful fallback (half-MP refund, no damage).
    if not counter_text:
        return await _memory_recall_fallback(
            eid=eid,
            coach=coach,
            voice=voice,
            mp_before=mp_before,
            transcript=transcript,
            highlighted_line=highlighted_line,
        )

    # ---- Apply damage via the Wave-0-extended compute_damage --------------
    from app.debate.damage import compute_damage
    from app.debate.topics import domain_match_mult

    enemy_def = int(getattr(enemy, "def_", None) or 10)
    enemy_level = int(getattr(enemy, "level", None) or 1)
    coach_atk = int(getattr(coach, "atk", None) or 10)
    coach_level = int(getattr(coach, "level", None) or 1)
    coach_domain = str(getattr(coach, "domain", "GENERAL") or "GENERAL")

    damage = compute_damage(
        score=MEMORY_RECALL_SCORE,
        attacker_type=coach.type,
        defender_type=enemy.type,
        skill_mult=MEMORY_RECALL_SKILL_MULT,
        momentum=1.0,
        attacker_level=coach_level,
        defender_level=enemy_level,
        attacker_atk=coach_atk,
        defender_def=enemy_def,
        domain_match=domain_match_mult(coach_domain, topic),
    )
    damage = max(0, int(damage))

    # ---- Deduct MP + HP (via the existing set_mp / set_hp helpers) --------
    mp_after = max(0, mp_before - MEMORY_RECALL_MP_COST)
    await set_mp(eid, coach.monster_id, mp_after)

    new_enemy_hp = max(0, int(enemy.hp) - damage)
    await set_hp(eid, enemy.monster_id, new_enemy_hp)
    # Reflect the deduction on the in-memory combatant snapshot too (so any
    # downstream code that reads `combatants` sees the updated HP).
    enemy.hp = new_enemy_hp

    # ---- Append the counter as a new Utterance to the transcript ---------
    turn_no = int(meta.get("turn_no", 0) or 0)
    utterance = {
        "turn": turn_no,
        "actor_id": coach.monster_id,
        "actor_role": "party",
        "skill_used": "Memory Recall",
        "text": counter_text,
        "ts": time.time(),
    }
    await append_utterance(eid, utterance)

    # ---- Build response --------------------------------------------------
    refreshed_transcript = transcript + [utterance]
    return MemoryRecallResult(
        encounter_id=eid,
        coach_monster_id=coach.monster_id,
        transcript_slice=_transcript_slice(refreshed_transcript, MEMORY_RECALL_TRANSCRIPT_SLICE),
        highlighted_line=highlighted_line,
        counter_text=counter_text,
        mp_spent=MEMORY_RECALL_MP_COST,
        mp_remaining=mp_after,
        damage=damage,
    )


async def _memory_recall_fallback(
    *,
    eid: str,
    coach: Any,  # type: ignore[name-defined]
    voice: str,
    mp_before: int,
    transcript: list[dict],
    highlighted_line: str,
) -> MemoryRecallResult:
    """Graceful fallback: half-MP refund, no damage, generic counter.

    Used on cache miss (empty transcript) and on model timeout/failure. Never
    raises — keeps the contract that Memory Recall never 500s the request path.
    """
    counter = f"{getattr(coach, 'name', 'Your monster')} recalls: '{voice}' — but the moment passes."

    # Refund half the MP cost (deduct only `MP_REFUND`, not the full cost).
    mp_after = max(0, mp_before - MEMORY_RECALL_MP_REFUND)
    try:
        await set_mp(eid, coach.monster_id, mp_after)
    except Exception:  # noqa: BLE001
        # Even Redis being down must not 500 the response; skip the write.
        mp_after = mp_before  # unchanged in that case

    return MemoryRecallResult(
        encounter_id=eid,
        coach_monster_id=coach.monster_id,
        transcript_slice=_transcript_slice(transcript, MEMORY_RECALL_TRANSCRIPT_SLICE),
        highlighted_line=highlighted_line,
        counter_text=counter,
        mp_spent=MEMORY_RECALL_MP_REFUND,
        mp_remaining=mp_after,
        damage=0,
    )


@router.post("/{eid}/flee", response_model=TurnResult)
async def flee(eid: str, session: AsyncSession = Depends(get_session)) -> TurnResult:
    await get_meta(eid)  # 404 if missing
    await set_meta(eid, phase="lost", status="flee")
    await _finalize(eid, EncounterResult.flee)
    state = await build_encounter_state(eid)
    return TurnResult(encounter=state, new_utterances=[], new_verdicts=[], capturable_ids=[])


@router.websocket("/{eid}/stream")
async def stream(ws: WebSocket, eid: str) -> None:
    """Stream a debate live. Each client message {"rounds": N} runs N rounds;
    events (utterance/verdict/hp/phase) are pushed as JSON as they happen. If the
    client connects without sending, we run a single round by default."""
    await ws.accept()
    try:
        # Validate the encounter exists.
        try:
            meta = await get_meta(eid)
        except HTTPException:
            await ws.send_json({"type": "error", "data": {"detail": "encounter not found"}})
            await ws.close()
            return

        # Send initial snapshot.
        state = await build_encounter_state(eid)
        await ws.send_json({"type": "state", "data": state.model_dump()})

        while True:
            # Wait for a drive command (or default to 1 round if client closes input).
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                break

            # WS-G: a human-typed argument drives ONE human round (player turn +
            # autonomous enemy rebuttal). Otherwise {"rounds": N} drives the
            # autonomous loop as before.
            is_argue = isinstance(msg, dict) and msg.get("action") == "argue"
            is_skill = isinstance(msg, dict) and msg.get("action") == "skill"
            if is_skill:
                try:
                    for ev in await _invoke_non_advancing_skill(
                        eid,
                        msg.get("skill_id") or None,
                        msg.get("actor_id") or None,
                    ):
                        await ws.send_json(ev)
                except HTTPException as he:
                    await ws.send_json(
                        {
                            "type": "mp_insufficient"
                            if he.status_code == 400 and "MP" in str(he.detail)
                            else "error",
                            "data": {
                                "skill_id": msg.get("skill_id"),
                                "detail": he.detail,
                            },
                            "message": str(he.detail),
                        }
                    )
                await ws.send_json(
                    {"type": "round_done", "data": {"phase": (await get_meta(eid)).get("phase")}}
                )
                continue
            rounds = 1 if is_argue else max(1, min(int(msg.get("rounds", 1)), 12))
            # Optional pick-your-agent (commit 6b4ded9): the autonomous branch may
            # honor `actor_id` so the player can choose which party agent argues.
            # None falls back to the orchestrator's autonomous initiative.
            active_party_id = msg.get("actor_id") or None

            for _ in range(rounds):
                meta = await get_meta(eid)
                if meta.get("phase") in ("won", "lost"):
                    break
                topic = meta.get("topic", "")
                run_id = meta.get("run_id", "")
                start_turn = int(meta.get("turn_no", 0) or 0)
                combatants = await load_combatants(eid)
                momentum = await load_momentum(eid)

                if is_argue:
                    # Gacha Wave B MP gate (WS path): if the player can't afford
                    # the picked skill, surface a typed error and skip the round.
                    # Mirrors the 400 returned by POST /{eid}/argue.
                    skill_id_ws = msg.get("skill_id")
                    try:
                        await _check_skill_mp(eid, skill_id_ws, active_party_id)
                    except HTTPException as he:
                        # The outer loop will still emit `round_done` after this
                        # iteration, so the client sees one mp_insufficient and
                        # one terminal event (no duplicate round_done).
                        await ws.send_json(
                            {
                                "type": "mp_insufficient",
                                "data": {
                                    "skill_id": skill_id_ws,
                                    "detail": he.detail,
                                },
                            }
                        )
                        continue

                    stream = run_human_round_stream(
                        eid, topic, combatants, run_id, start_turn, momentum,
                        str(msg.get("text", "")), skill_id_ws,
                        active_party_id=active_party_id,
                    )
                else:
                    stream = run_round_stream(
                        eid, topic, combatants, run_id, start_turn, momentum,
                        active_party_id=active_party_id,
                    )

                final_phase = {"phase": "debating", "capturable_ids": [], "turn_no": start_turn}
                async for ev in stream:
                    await ws.send_json({"type": ev.kind, "data": ev.data})
                    if ev.kind == "phase":
                        final_phase = ev.data

                from app.redis_state import get_redis, k_momentum

                r = get_redis()
                await r.hset(k_momentum(eid), mapping={k: str(v) for k, v in momentum.items()})
                await set_meta(
                    eid,
                    turn_no=final_phase.get("turn_no", start_turn),
                    phase=final_phase["phase"],
                )
                if final_phase["phase"] in _PHASE_TO_RESULT:
                    level_ups = await _finalize(
                        eid, _PHASE_TO_RESULT[final_phase["phase"]]
                    )
                    # Forward each LevelUp event on the same WS channel as the
                    # existing hp/phase events; the frontend overlay listens
                    # for `{"type": "LevelUp", ...}`. Defensive: a send failure
                    # must not crash the WS loop.
                    for lu in level_ups:
                        try:
                            await ws.send_json(lu)
                        except Exception:  # noqa: BLE001
                            break
                    break

            await ws.send_json({"type": "round_done", "data": {"phase": (await get_meta(eid)).get("phase")}})
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "data": {"detail": str(e)}})
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
