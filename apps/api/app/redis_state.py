"""Redis client + the FROZEN key schema for live encounters (Wave 0 contract).

The shared conversation transcript lives here so every agent (party + enemies)
is aware of everything said by everyone. Redis is the source of truth DURING an
encounter; the debate engine snapshots to Postgres on completion.

Key layout (per encounter_id):
  enc:{id}:meta       hash   topic, turn_no, phase, current_actor, status
  enc:{id}:transcript list   JSON utterances {turn, actor_id, actor_role, skill_used, text, ts}
  enc:{id}:hp         hash   monster_id -> current_hp
  enc:{id}:queue      list   turn order (monster_ids) for the round
  enc:{id}:judge      list   JSON verdicts {turn, target, score, rationale, damage}
  enc:{id}:momentum   hash   side -> momentum float

Helpers here are intentionally thin; the debate engine (WS-B) builds richer
operations on top. Keep the key builders and JSON shapes stable.
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis

from app.config import settings

ENCOUNTER_TTL_SECONDS = 2 * 60 * 60  # 2h

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


# ---- Key builders (the contract) ----


def k_meta(eid: str) -> str:
    return f"enc:{eid}:meta"


def k_transcript(eid: str) -> str:
    return f"enc:{eid}:transcript"


def k_hp(eid: str) -> str:
    return f"enc:{eid}:hp"


def k_queue(eid: str) -> str:
    return f"enc:{eid}:queue"


def k_judge(eid: str) -> str:
    return f"enc:{eid}:judge"


def k_momentum(eid: str) -> str:
    return f"enc:{eid}:momentum"


def encounter_keys(eid: str) -> list[str]:
    return [k_meta(eid), k_transcript(eid), k_hp(eid), k_queue(eid), k_judge(eid), k_momentum(eid)]


# ---- Thin helpers ----


async def append_utterance(eid: str, utterance: dict[str, Any]) -> None:
    r = get_redis()
    await r.rpush(k_transcript(eid), json.dumps(utterance))
    await r.expire(k_transcript(eid), ENCOUNTER_TTL_SECONDS)


async def get_transcript(eid: str) -> list[dict[str, Any]]:
    r = get_redis()
    raw = await r.lrange(k_transcript(eid), 0, -1)
    return [json.loads(x) for x in raw]


async def set_hp(eid: str, monster_id: str, hp: int) -> None:
    r = get_redis()
    await r.hset(k_hp(eid), monster_id, hp)
    await r.expire(k_hp(eid), ENCOUNTER_TTL_SECONDS)


async def get_hp_map(eid: str) -> dict[str, int]:
    r = get_redis()
    raw = await r.hgetall(k_hp(eid))
    return {m: int(v) for m, v in raw.items()}


async def clear_encounter(eid: str) -> None:
    r = get_redis()
    await r.delete(*encounter_keys(eid))


async def clear_conversation(eid: str) -> None:
    """Evict only the heavy conversation keys (transcript + judge verdicts)
    after a battle is durably persisted, to avoid context pollution. The small
    meta/hp/momentum keys are left to expire via TTL so the encounter stays
    queryable and repeat calls get a clean terminal phase."""
    r = get_redis()
    await r.delete(k_transcript(eid), k_judge(eid))


async def ping() -> bool:
    return bool(await get_redis().ping())
