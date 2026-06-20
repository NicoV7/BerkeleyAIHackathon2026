"""Self-play seam for training (WS-F).

Preferred source is WS-B's orchestrator:
    from app.debate.orchestrator import run_self_play
    run_self_play(party_monster, sparring_monster, topic, rounds) -> dict
        { "transcript": [Utterance-ish dicts], "score": float (0..100 for party) }

Until WS-B lands that, we provide a minimal local self-play that alternates
gateway.complete() calls between two personas and scores the party debater with a
single judge call. `play(...)` first tries WS-B's function, then falls back.

The returned dict shape (stable for WS-F):
    {
        "transcript": [ {turn, actor_id, actor_role, skill_used, text, ts}, ... ],
        "score": float,        # party debater quality, 0..100
        "source": "ws-b" | "local",
    }
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.gateway.gateway import gateway
from app.training import genome as genome_mod

DEFAULT_MODEL = "default"  # tests override with "gemma3:1b"


async def play(
    party_genome: dict[str, Any],
    *,
    topic: str,
    rounds: int = 2,
    sparring_genome: Optional[dict[str, Any]] = None,
    party_monster: Any = None,
    sparring_monster: Any = None,
    model: str = DEFAULT_MODEL,
    party_id: str = "party",
    enemy_id: str = "enemy",
) -> dict[str, Any]:
    """Run a self-play debate and score the party debater.

    If WS-B's `run_self_play(party_monster, sparring_monster, topic, rounds)` is
    importable AND we have monster objects, use it. Otherwise run the local
    genome-driven fallback (works with just genomes — no DB needed)."""
    # --- Try WS-B's orchestrator first ---
    try:
        from app.debate.orchestrator import run_self_play  # type: ignore

        if party_monster is not None and sparring_monster is not None:
            result = run_self_play(party_monster, sparring_monster, topic, rounds)
            if hasattr(result, "__await__"):
                result = await result  # support async impls
            return {
                "transcript": result.get("transcript", []),
                "score": float(result.get("score", 50.0)),
                "source": "ws-b",
            }
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 — any WS-B failure -> local fallback
        pass

    return await _local_self_play(
        party_genome,
        topic=topic,
        rounds=rounds,
        sparring_genome=sparring_genome,
        model=model,
        party_id=party_id,
        enemy_id=enemy_id,
    )


async def _local_self_play(
    party_genome: dict[str, Any],
    *,
    topic: str,
    rounds: int,
    sparring_genome: Optional[dict[str, Any]],
    model: str,
    party_id: str,
    enemy_id: str,
) -> dict[str, Any]:
    party_sys = genome_mod.system_prompt(party_genome)
    spar_sys = genome_mod.system_prompt(
        sparring_genome
        or {
            "persona": {"name": "Rival", "tone": "skeptical and sharp"},
            "harness": {"system_prompt": "You are a tough sparring debater."},
        }
    )

    transcript: list[dict[str, Any]] = []
    history: list[str] = []
    turn = 0

    party_stance = f"Argue FOR the proposition: {topic}."
    enemy_stance = f"Argue AGAINST the proposition: {topic}."

    for _ in range(max(1, rounds)):
        for actor_id, role, sys_prompt, stance in (
            (party_id, "party", party_sys, party_stance),
            (enemy_id, "enemy", spar_sys, enemy_stance),
        ):
            turn += 1
            ctx = "\n".join(history[-4:]) if history else "(opening statement)"
            user = (
                f"Debate topic: {topic}\n{stance}\n\n"
                f"Recent exchange:\n{ctx}\n\n"
                "Give your next debate turn in 2-3 punchy sentences. No preamble."
            )
            try:
                text = await gateway.complete(
                    [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user},
                    ],
                    model=model,
                    temperature=0.8,
                    max_tokens=160,
                )
            except Exception as e:  # noqa: BLE001
                text = f"(no response: {e})"
            text = (text or "").strip()
            transcript.append(
                {
                    "turn": turn,
                    "actor_id": actor_id,
                    "actor_role": role,
                    "skill_used": None,
                    "text": text,
                    "ts": time.time(),
                }
            )
            history.append(f"{role.upper()}: {text}")

    score = await _judge(topic, party_id, transcript, model)
    transcript.append(
        {
            "turn": turn + 1,
            "actor_id": "judge",
            "actor_role": "judge",
            "skill_used": None,
            "text": f"Party debater scored {score:.0f}/100.",
            "ts": time.time(),
        }
    )
    return {"transcript": transcript, "score": score, "source": "local"}


async def _judge(
    topic: str, party_id: str, transcript: list[dict[str, Any]], model: str
) -> float:
    convo = "\n".join(
        f"{u['actor_role'].upper()}: {u['text']}"
        for u in transcript
        if u["actor_role"] != "judge"
    )
    prompt = (
        f"You are an impartial debate judge for the topic: {topic}\n\n"
        f"Transcript:\n{convo}\n\n"
        "Rate ONLY the PARTY debater's overall persuasiveness from 0 to 100. "
        "Reply with just the integer, nothing else."
    )
    try:
        raw = await gateway.complete(
            [{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            max_tokens=8,
        )
        return _parse_score(raw)
    except Exception:  # noqa: BLE001
        return 50.0


def _parse_score(raw: str) -> float:
    import re

    m = re.search(r"\d{1,3}(?:\.\d+)?", raw or "")
    if not m:
        return 50.0
    v = float(m.group())
    return max(0.0, min(100.0, v))
