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
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Encounter, EncounterResult
from app.db.session import SessionLocal, get_session
from app.debate.orchestrator import run_round_stream
from app.redis_state import get_transcript, k_meta
from app.routers.encounter import (
    build_encounter_state,
    get_meta,
    load_combatants,
    load_momentum,
    set_meta,
)
from app.schemas import AutoRequest, JudgeVerdict, TurnRequest, TurnResult, Utterance

router = APIRouter(prefix="/api/encounters", tags=["debate"])

# Wall-clock guards: a single round shouldn't run forever on a slow local model,
# and /auto shouldn't keep starting rounds past a total budget.
ROUND_TIMEOUT_S = 120.0
AUTO_BUDGET_S = 240.0


# ---- Finalize (idempotent) --------------------------------------------------


async def _finalize(eid: str, result: EncounterResult) -> None:
    """Persist the battle durably, write per-party BATTLE memories, then evict
    the conversation from Redis. Idempotent: a second call is a no-op.

    Order matters: we read the transcript/verdicts/HP out of Redis FIRST, snapshot
    them onto the Encounter row and into each party member's memory (so GEPA/RAG
    have real battle data that outlives the cache), and only then evict the heavy
    Redis keys to avoid context pollution.
    """
    from app.redis_state import clear_conversation, get_hp_map, get_redis, k_judge

    transcript = await get_transcript(eid)
    r = get_redis()
    verdicts = [json.loads(v) for v in await r.lrange(k_judge(eid), 0, -1)]
    final_hp = await get_hp_map(eid)
    meta = await r.hgetall(k_meta(eid))
    topic = meta.get("topic", "")
    roster = json.loads(meta.get("combatants", "[]"))

    async with SessionLocal() as session:
        enc = await session.get(Encounter, eid)
        if not enc:
            return
        if enc.result != EncounterResult.ongoing:
            return  # already finalized
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

    # Conversation is durably stored — free the cache.
    await clear_conversation(eid)


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
    lines = [f"{names.get(u['actor_id'], u['actor_role'])}: {u['text']}" for u in transcript]
    convo = "\n".join(lines)

    for pid in (enc.party_ids or []):
        my_hp = final_hp.get(pid)
        my_lines = [u["text"] for u in transcript if u["actor_id"] == pid]
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


async def _run_one_round(
    eid: str, active_party_id: str | None = None
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

    async for ev in run_round_stream(
        eid, topic, combatants, run_id, start_turn, momentum,
        active_party_id=active_party_id,
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


def _utt_fields(d: dict) -> dict:
    return {
        "turn": d["turn"],
        "actor_id": d["actor_id"],
        "actor_role": d["actor_role"],
        "skill_used": d.get("skill_used"),
        "text": d["text"],
        "ts": d["ts"],
    }


def _to_verdict(d: dict) -> JudgeVerdict:
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
async def take_turn(
    eid: str,
    req: TurnRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> TurnResult:
    active_party_id = req.actor_id if req else None
    try:
        new_utts, new_verdicts, phase_event = await asyncio.wait_for(
            _run_one_round(eid, active_party_id), timeout=ROUND_TIMEOUT_S
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
            rounds = max(1, min(int(msg.get("rounds", 1)), 12))
            # The party agent the player picked (None = agents auto-pick).
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

                final_phase = {"phase": "debating", "capturable_ids": [], "turn_no": start_turn}
                async for ev in run_round_stream(
                    eid, topic, combatants, run_id, start_turn, momentum,
                    active_party_id=active_party_id,
                ):
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
                    await _finalize(eid, _PHASE_TO_RESULT[final_phase["phase"]])
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
