"""End-to-end battle latency harness (Track A, Wave A1 deliverable).

`bench_judge.py` measures the judge in isolation and CANNOT validate the felt
post-submit latency the player experiences. This harness drives the REAL human
round (`run_human_round_stream`) over Redis and reports the metrics from the
design doc's Success Criteria:

  * submit -> first-score      (player submit -> first `verdict` event)
  * submit -> damage-applied   (player submit -> first `hp` event)
  * cache-MISS opening time    (generate the enemy opening once)
  * cache-HIT  opening time    (retrieve the same opening from Redis)

It seeds a throwaway encounter directly in Redis (no HTTP / no DB), runs the
human round, and drains the event stream with timestamps. Requires a running
Redis + Ollama, same as a live battle (everything-local constraint).

Usage (inside the api container, apps/api on sys.path):
    python -m scripts.bench_e2e [--topic "..."] [--runs 2] [--model gemma3:1b]

Prints ONE JSON object on stdout (json.loads-parseable). Round 1 is the
opening cache-MISS (first time this topic is seen); subsequent rounds are HITs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import traceback
import uuid
from typing import Any

# Intended to run inside the api container with apps/api on sys.path.
from app.debate.materialize import (  # noqa: E402
    PROMPT_VERSION,
    get_cached_opening,
    get_or_create_opening,
    topic_hash,
)
from app.debate.orchestrator import Combatant, run_human_round_stream  # noqa: E402
from app.redis_state import (  # noqa: E402
    ENCOUNTER_TTL_SECONDS,
    encounter_keys,
    get_redis,
    k_hp,
    k_meta,
    k_momentum,
    k_opening,
    k_queue,
)

DEFAULT_TOPIC = "Pineapple belongs on pizza."
PLAYER_TEXT = (
    "I argue FOR this: balance, not tradition, is the only honest test of a topping, "
    "and the sweet-salt-acid contrast carries the case."
)


def _mk_combatants(model: str) -> tuple[Combatant, Combatant]:
    player = Combatant(
        monster_id=f"p-{uuid.uuid4().hex[:8]}",
        name="Rookie Debater",
        type="LOGOS",
        role="party",
        hp=100,
        max_hp=100,
        level=2,
        owner="player",
        skills=["Steelman"],
        model=model,
    )
    enemy = Combatant(
        monster_id=f"e-{uuid.uuid4().hex[:8]}",
        name="Wild Sophist",
        type="LOGOS",
        role="enemy",
        hp=90,
        max_hp=90,
        level=2,
        owner="wild",
        skills=["Reframe"],
        model=model,
    )
    return player, enemy


async def _seed_encounter(eid: str, topic: str, combatants: list[Combatant]) -> None:
    """Minimal direct-to-Redis encounter seed (no DB) so the round can run."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.delete(*encounter_keys(eid))
    pipe.hset(
        k_meta(eid),
        mapping={
            "id": eid,
            "run_id": "bench",
            "topic": topic,
            "turn_no": 0,
            "phase": "debating",
            "status": "ongoing",
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
    pipe.rpush(k_queue(eid), *[c.monster_id for c in combatants])
    pipe.hset(k_momentum(eid), mapping={"party": 1.0, "enemy": 1.0})
    for key in encounter_keys(eid):
        pipe.expire(key, ENCOUNTER_TTL_SECONDS)
    await pipe.execute()


async def _time_opening(topic: str, model: str) -> dict[str, Any]:
    """Force a cache-MISS (clear key) then a cache-HIT, timing each."""
    r = get_redis()
    await r.delete(k_opening(topic_hash(topic), PROMPT_VERSION))

    t0 = time.monotonic()
    miss_text, hit0 = await get_or_create_opening(topic, model)
    miss_s = time.monotonic() - t0

    t1 = time.monotonic()
    _hit_text, hit1 = await get_or_create_opening(topic, model)
    hit_s = time.monotonic() - t1

    return {
        "opening_miss_s": round(miss_s, 3),
        "opening_hit_s": round(hit_s, 4),
        "miss_reported_hit": hit0,  # expected False
        "second_reported_hit": hit1,  # expected True
        "opening_preview": (miss_text or "")[:120],
    }


async def _run_round(eid: str, topic: str, combatants: list[Combatant]) -> dict[str, Any]:
    """Drive ONE human round, timestamping the events that define felt latency."""
    momentum = {"party": 1.0, "enemy": 1.0}
    submit_t = time.monotonic()
    first_score_s: float | None = None
    damage_applied_s: float | None = None
    enemy_utt_s: float | None = None
    utt_count = 0

    async for ev in run_human_round_stream(
        eid, topic, combatants, "bench", 0, momentum, PLAYER_TEXT, "Steelman"
    ):
        now = time.monotonic() - submit_t
        if ev.kind == "utterance":
            utt_count += 1
            if utt_count == 2:  # 1 = player echo, 2 = enemy line
                enemy_utt_s = now
        elif ev.kind == "verdict" and first_score_s is None:
            first_score_s = now
        elif ev.kind == "hp" and damage_applied_s is None:
            damage_applied_s = now

    return {
        "submit_to_enemy_utterance_s": round(enemy_utt_s, 3) if enemy_utt_s is not None else None,
        "submit_to_first_score_s": round(first_score_s, 3) if first_score_s is not None else None,
        "submit_to_damage_applied_s": round(damage_applied_s, 3) if damage_applied_s is not None else None,
    }


async def bench(topic: str, runs: int, model: str) -> dict[str, Any]:
    # Opening cache timing (miss then hit) — independent of the round loop.
    opening = await _time_opening(topic, model)

    round_metrics: list[dict[str, Any]] = []
    for i in range(max(1, runs)):
        player, enemy = _mk_combatants(model)
        eid = f"bench-{uuid.uuid4().hex[:12]}"
        await _seed_encounter(eid, topic, [player, enemy])
        try:
            m = await _run_round(eid, topic, [player, enemy])
        except Exception as e:  # noqa: BLE001
            m = {"error": f"{type(e).__name__}: {e}"}
        finally:
            await get_redis().delete(*encounter_keys(eid))
        # Round 0 saw the opening as a fresh MISS-then-HIT (warmed above), so every
        # bench round here is an opening cache HIT on the round critical path.
        m["round"] = i
        round_metrics.append(m)

    def _agg(key: str) -> float | None:
        vals = [r[key] for r in round_metrics if isinstance(r.get(key), (int, float))]
        return round(statistics.median(vals), 3) if vals else None

    return {
        "topic": topic,
        "topic_hash": topic_hash(topic),
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "runs": len(round_metrics),
        **opening,
        "median_submit_to_enemy_utterance_s": _agg("submit_to_enemy_utterance_s"),
        "median_submit_to_first_score_s": _agg("submit_to_first_score_s"),
        "median_submit_to_damage_applied_s": _agg("submit_to_damage_applied_s"),
        "per_round": round_metrics,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--topic", default=DEFAULT_TOPIC, help="Debate topic to bench")
    p.add_argument("--runs", type=int, default=2, help="Human rounds to time (default 2)")
    p.add_argument("--model", default="gemma3:1b", help="Actor/enemy model (default gemma3:1b)")
    args = p.parse_args()

    try:
        result = asyncio.run(bench(args.topic, args.runs, args.model))
    except Exception:  # noqa: BLE001
        print(json.dumps({"topic": args.topic, "fatal_error": traceback.format_exc()}))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
