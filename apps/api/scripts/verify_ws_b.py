"""WS-B verification: create encounter, run /turn + /auto, check WS + self-play.

Run with the api venv:
    .venv/bin/python scripts/verify_ws_b.py
Requires the live stack (API :8000, Redis, Ollama gemma3:1b).
"""
from __future__ import annotations

import asyncio
import json
import os

# This script runs on the HOST; the API container reaches Postgres via the
# docker hostname "postgres", but from the host it's exposed on localhost.
# Override before importing app modules so direct DB writes resolve.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://debate:debate@localhost:5432/debate",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import httpx

BASE = os.environ.get("API_BASE", "http://localhost:8000")
WS_BASE = BASE.replace("http", "ws", 1)
MODEL = "gemma3:1b"


async def make_run() -> str:
    """Create a Run + player Monster via raw SQL (avoids ORM tz-default quirk
    when writing from the host) so we don't depend on WS-A's /api/runs router."""
    import json as _json
    import uuid as _uuid

    from sqlalchemy import text

    from app.db.session import engine, init_db

    await init_db()
    run_id = str(_uuid.uuid4())
    monster_id = str(_uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, debate_topic, seed, player_x, player_y, status, created_at) "
                "VALUES (:id, :topic, 0, 0, 0, 'active', now())"
            ),
            {"id": run_id, "topic": "Should AI systems have the right to refuse tasks?"},
        )
        await conn.execute(
            text(
                "INSERT INTO monsters (id, run_id, owner, name, type, persona, harness, skills, "
                "genome_version, level, xp, max_hp, evolution_stage, model, created_at) "
                "VALUES (:id, :run_id, 'player', 'Logica', 'logos', "
                "CAST(:persona AS jsonb), CAST('{}' AS jsonb), CAST(:skills AS jsonb), "
                "1, 2, 0, 100, 0, :model, now())"
            ),
            {
                "id": monster_id,
                "run_id": run_id,
                "persona": _json.dumps({"style": "rigorous", "bio": "Champion of evidence."}),
                "skills": _json.dumps(["Steelman"]),
                "model": MODEL,
            },
        )
    return run_id


async def main() -> None:
    run_id = await make_run()
    print(f"run_id = {run_id}")

    async with httpx.AsyncClient(timeout=420.0) as c:
        # Health
        h = (await c.get(f"{BASE}/api/health")).json()
        print(f"health: {h['status']} gateway_models={h['gateway'].get('models')}")

        # Create encounter
        r = await c.post(f"{BASE}/api/encounters", json={"run_id": run_id})
        r.raise_for_status()
        enc = r.json()
        eid = enc["id"]
        print(f"\nencounter {eid} phase={enc['phase']}")
        for cb in enc["combatants"]:
            print(f"  {cb['role']:5} {cb['name']:16} {cb['type']:8} hp={cb['hp']}/{cb['max_hp']}")
        hp_before = {cb["monster_id"]: cb["hp"] for cb in enc["combatants"]}

        # /turn
        print("\n--- POST /turn ---")
        r = await c.post(f"{BASE}/api/encounters/{eid}/turn")
        r.raise_for_status()
        tr = r.json()
        for u in tr["new_utterances"]:
            print(f"  [{u['actor_role']}] {u['text'][:90]}")
        for v in tr["new_verdicts"]:
            print(f"  VERDICT score={v['score']} dmg={v['damage']} target={v['target'][:8]} :: {v['rationale'][:60]}")
        print("  HP after turn:")
        for cb in tr["encounter"]["combatants"]:
            delta = cb["hp"] - hp_before[cb["monster_id"]]
            print(f"    {cb['name']:16} hp={cb['hp']} (delta {delta})")
        print(f"  capturable_ids={tr['capturable_ids']} phase={tr['encounter']['phase']}")

        # /auto 2 rounds
        print("\n--- POST /auto rounds=2 ---")
        r = await c.post(f"{BASE}/api/encounters/{eid}/auto", json={"rounds": 2})
        r.raise_for_status()
        tr = r.json()
        print(f"  {len(tr['new_utterances'])} utterances, {len(tr['new_verdicts'])} verdicts")
        for v in tr["new_verdicts"]:
            print(f"  VERDICT score={v['score']} dmg={v['damage']}")
        print("  HP now:")
        for cb in tr["encounter"]["combatants"]:
            print(f"    {cb['name']:16} hp={cb['hp']}/{cb['max_hp']}")
        print(f"  phase={tr['encounter']['phase']} capturable={tr['capturable_ids']}")

        # GET encounter
        r = await c.get(f"{BASE}/api/encounters/{eid}")
        gs = r.json()
        print(f"\nGET /encounters/{eid}: transcript={len(gs['transcript'])} verdicts={len(gs['verdicts'])}")

    # WS /stream
    print("\n--- WS /stream (1 round) ---")
    try:
        import websockets

        async with websockets.connect(f"{WS_BASE}/api/encounters/{eid}/stream") as ws:
            first = json.loads(await ws.recv())
            print(f"  initial event kind={first['kind']}")
            await ws.send(json.dumps({"rounds": 1}))
            kinds = []
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                kinds.append(ev["kind"])
                if ev["kind"] == "utterance":
                    print(f"  ws utterance: {ev['data']['text'][:60]}")
                if ev["kind"] == "round_done":
                    break
            print(f"  ws event kinds: {kinds}")
    except ModuleNotFoundError:
        print("  (websockets lib not installed; skipping live WS test — endpoint still mounted)")
    except Exception as e:
        print(f"  WS error: {e}")

    # Headless self-play
    print("\n--- run_self_play (headless) ---")
    from app.debate.orchestrator import run_self_play

    party = {"id": "p1", "name": "Logica", "type": "LOGOS", "level": 2, "max_hp": 100, "model": MODEL,
             "persona": {"style": "rigorous"}, "skills": ["Steelman"]}
    spar = {"id": "e1", "name": "Sophist", "type": "CHAOS", "level": 2, "max_hp": 100, "model": MODEL,
            "persona": {"style": "provocative"}, "skills": ["Reframe"]}
    result = run_self_play(party, spar, "Is persuasion a form of manipulation?", rounds=2)
    print(f"  transcript turns: {len(result['transcript'])}")
    print(f"  verdicts: {len(result['verdicts'])}")
    print(f"  net_score={result['net_score']} result={result['result']}")
    print(f"  party_hp={result['party_hp']} sparring_hp={result['sparring_hp']}")
    if result["transcript"]:
        print(f"  sample: [{result['transcript'][0]['actor_role']}] {result['transcript'][0]['text'][:70]}")

    print("\nVERIFY DONE.")


if __name__ == "__main__":
    asyncio.run(main())
