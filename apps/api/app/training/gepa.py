"""GEPA — reflective prompt/genome evolution (WS-F).

`run_gepa(session, monster, rounds)`:
  1. Try DSPy's GEPA optimizer inside try/except. DSPy is an OPTIONAL dep and is
     NOT installed in the running container, so on ImportError or ANY failure we
     fall back to a hand-rolled reflective loop. The FALLBACK is the primary
     deliverable.

  Fallback loop, each round:
    - sample a handful of genome mutations (bandit-unweighted here)
    - self-play score each variant
    - pick the best; if it beats the incumbent, an LLM critiques the *losing*
      incumbent transcript and proposes a sharper system-prompt directive, which
      seeds the next round
    - keep the best genome seen

  Returns (new_genome, score_delta). Persists the winning genome + a
  TrainingArtifact(kind='gepa') via genome.apply_genome (caller commits).
"""
from __future__ import annotations

from typing import Any

from app.gateway.gateway import gateway
from app.training import genome as genome_mod
from app.training import selfplay

VARIANTS_PER_ROUND = 3


async def run_gepa(
    session: Any,
    monster: Any,
    rounds: int = 3,
    *,
    topic: str | None = None,
    model: str = selfplay.DEFAULT_MODEL,
    persist: bool = True,
) -> tuple[dict[str, Any], float]:
    """Evolve the monster's genome. Returns (best_genome, score_delta)."""
    topic = topic or _topic_for(monster)

    # --- DSPy GEPA attempt (bonus path) ---
    try:
        return await _run_dspy_gepa(session, monster, rounds, topic=topic, model=model)
    except Exception:  # noqa: BLE001 — ImportError or any DSPy failure -> fallback
        pass

    return await _run_fallback_gepa(
        session, monster, rounds, topic=topic, model=model, persist=persist
    )


# --------------------------------------------------------------- DSPy (bonus)


async def _run_dspy_gepa(
    session: Any, monster: Any, rounds: int, *, topic: str, model: str
) -> tuple[dict[str, Any], float]:
    import dspy  # noqa: F401  — raises ImportError when absent -> caller falls back

    # DSPy is not installed in this environment; the exact GEPA API is "verify at
    # runtime". Rather than ship an unverified integration, we deliberately defer
    # to the reflective fallback. If a verified DSPy GEPA wiring lands later it
    # replaces this body. Force the fallback for now:
    raise RuntimeError("DSPy GEPA wiring deferred to reflective fallback")


# ------------------------------------------------------------ fallback (main)


async def _run_fallback_gepa(
    session: Any,
    monster: Any,
    rounds: int,
    *,
    topic: str,
    model: str,
    persist: bool,
) -> tuple[dict[str, Any], float]:
    base_genome = genome_mod.read_genome(monster)

    baseline = await selfplay.play(
        base_genome, topic=topic, rounds=1, model=model,
        party_monster=monster,
    )
    baseline_score = baseline["score"]

    best_genome = base_genome
    best_score = baseline_score
    best_critique_directive: str | None = None

    for _ in range(max(1, rounds)):
        seed = best_genome
        if best_critique_directive:
            # fold the LLM's proposed directive into the seed for this round
            seed = genome_mod.read_genome(monster) if False else _with_directive(
                best_genome, best_critique_directive
            )

        variants = genome_mod.sample_mutations(seed, VARIANTS_PER_ROUND)
        round_best = best_genome
        round_best_score = best_score
        round_worst_transcript: list[dict[str, Any]] = []

        for variant_genome, _op in variants:
            r = await selfplay.play(
                variant_genome, topic=topic, rounds=1, model=model,
                party_monster=monster,
            )
            if r["score"] > round_best_score:
                round_best_score = r["score"]
                round_best = variant_genome
            else:
                round_worst_transcript = r["transcript"]

        if round_best_score > best_score:
            best_score = round_best_score
            best_genome = round_best

        # Reflect: LLM critiques a losing transcript -> a sharper directive.
        if round_worst_transcript:
            best_critique_directive = await _critique(
                topic, round_worst_transcript, model
            )

    score_delta = best_score - baseline_score

    if persist:
        genome_mod.apply_genome(
            session,
            monster,
            best_genome,
            kind="gepa",
            score_delta=score_delta,
            accepted=score_delta >= 0,
            before=base_genome,
        )

    return best_genome, score_delta


def _with_directive(genome: dict[str, Any], directive: str) -> dict[str, Any]:
    import copy

    g = copy.deepcopy(genome)
    dirs = g.setdefault("harness", {}).setdefault("directives", [])
    if directive and directive not in dirs:
        dirs.append(directive)
    return g


async def _critique(
    topic: str, transcript: list[dict[str, Any]], model: str
) -> str:
    convo = "\n".join(
        f"{u['actor_role'].upper()}: {u['text']}"
        for u in transcript
        if u.get("actor_role") != "judge"
    )[:2000]
    prompt = (
        f"A debater performed weakly on the topic: {topic}\n\n"
        f"Transcript:\n{convo}\n\n"
        "In ONE imperative sentence, give a coaching directive that would most "
        "improve the PARTY debater's next attempt. Reply with only that sentence."
    )
    try:
        out = await gateway.complete(
            [{"role": "user", "content": prompt}],
            model=model,
            temperature=0.5,
            max_tokens=60,
        )
        line = (out or "").strip().splitlines()[0].strip() if out else ""
        return line[:200]
    except Exception:  # noqa: BLE001
        return ""


def _topic_for(monster: Any) -> str:
    persona = getattr(monster, "persona", None) or {}
    return persona.get("topic") or "Social media does more harm than good."
