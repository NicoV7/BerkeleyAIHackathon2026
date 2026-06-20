"""GRPO-HITL — group-relative preference optimization with a human in the loop.

Flow:
  start_grpo(session, monster)  ->  PreferenceBatch
    - sample K=3 genome mutations (op selection biased by a per-monster bandit)
    - roll each out once via self-play -> transcript + judge score
    - register an in-memory job (dict) keyed by job_id holding the variants,
      their genomes, ops, and the baseline genome
    - return a PreferenceBatch (variants + transcripts + judge scores)

  apply_preference(job_id, ranking)  ->  dict
    - ranking is variant_ids best->worst (the human's preference)
    - compute group-relative advantage:
          rank_score  = normalized position (best=1.0 .. worst=0.0)
          judge_norm  = judge_score / 100
          combined    = 0.6*rank_score + 0.4*judge_norm
          advantage   = combined - group_mean(combined)
    - adopt the argmax-advantage variant's genome onto the monster
    - persist TrainingArtifact(kind='grpo'); update the mutation-op bandit
      (increment weight of the winning variant's op)

Jobs and the bandit are process-local dicts — fine for the hackathon.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from app.schemas import PreferenceBatch, PreferenceVariant, Utterance
from app.training import genome as genome_mod
from app.training import selfplay

K_VARIANTS = 3

# In-memory job store: job_id -> {monster_id, base_genome, variants:[...],
#                                 status, kind, score_delta}
_JOBS: dict[str, dict[str, Any]] = {}

# Per-monster mutation-op bandit: monster_id -> {op: weight}
_BANDIT: dict[str, dict[str, float]] = {}


def _bandit_for(monster_id: str) -> dict[str, float]:
    return _BANDIT.setdefault(
        monster_id, {op: 1.0 for op in genome_mod.OPERATORS}
    )


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    return _JOBS.get(job_id)


async def start_grpo(
    session: Any,
    monster: Any,
    *,
    topic: str | None = None,
    model: str = selfplay.DEFAULT_MODEL,
    k: int = K_VARIANTS,
) -> PreferenceBatch:
    """Sample K mutations, roll out, register a job, return a PreferenceBatch."""
    topic = topic or _topic_for(monster)
    base_genome = genome_mod.read_genome(monster)
    weights = _bandit_for(monster.id)

    sampled = genome_mod.sample_mutations(base_genome, k, weights=weights)

    job_id = str(uuid.uuid4())
    variants_meta: list[dict[str, Any]] = []
    pref_variants: list[PreferenceVariant] = []

    for variant_genome, op in sampled:
        roll = await selfplay.play(
            variant_genome, topic=topic, rounds=1, model=model,
            party_monster=monster,
        )
        variant_id = str(uuid.uuid4())
        score = float(roll["score"])
        utterances = [_to_utterance(u) for u in roll["transcript"]]

        variants_meta.append(
            {
                "variant_id": variant_id,
                "genome": variant_genome,
                "op": op,
                "judge_score": score,
            }
        )
        pref_variants.append(
            PreferenceVariant(
                variant_id=variant_id,
                transcript=utterances,
                judge_score=score,
            )
        )

    _JOBS[job_id] = {
        "job_id": job_id,
        "monster_id": monster.id,
        "kind": "grpo",
        "status": "awaiting_preference",
        "topic": topic,
        "base_genome": base_genome,
        "variants": variants_meta,
        "score_delta": None,
    }

    return PreferenceBatch(job_id=job_id, monster_id=monster.id, variants=pref_variants)


def apply_preference(
    session: Any,
    job_id: str,
    ranking: list[str],
    *,
    monster: Any | None = None,
) -> dict[str, Any]:
    """Apply the human ranking: compute advantages, adopt the winner, persist.

    `monster` may be passed (router already loaded it) or fetched by caller.
    Returns a summary dict {adopted_variant_id, score_delta, advantages, ...}.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise KeyError(f"Unknown GRPO job: {job_id}")

    variants: list[dict[str, Any]] = job["variants"]
    by_id = {v["variant_id"]: v for v in variants}

    # Rank score: best (index 0) -> 1.0, worst -> 0.0. Variants not in `ranking`
    # default to mid-rank so a partial ranking still works.
    n = len(variants)
    rank_pos: dict[str, int] = {}
    for pos, vid in enumerate(ranking):
        if vid in by_id:
            rank_pos[vid] = pos
    next_pos = len(rank_pos)
    for v in variants:
        if v["variant_id"] not in rank_pos:
            rank_pos[v["variant_id"]] = next_pos
            next_pos += 1

    def rank_score(vid: str) -> float:
        if n <= 1:
            return 1.0
        return 1.0 - (rank_pos[vid] / (n - 1))

    combined: dict[str, float] = {}
    for v in variants:
        vid = v["variant_id"]
        judge_norm = float(v["judge_score"]) / 100.0
        combined[vid] = 0.6 * rank_score(vid) + 0.4 * judge_norm

    group_mean = sum(combined.values()) / max(len(combined), 1)
    advantages = {vid: combined[vid] - group_mean for vid in combined}

    # Argmax advantage -> adopted variant.
    adopted_id = max(advantages, key=advantages.get)
    adopted = by_id[adopted_id]
    adopted_genome = adopted["genome"]
    adopted_op = adopted["op"]

    # Score delta vs group mean judge score (a meaningful "did we improve" signal).
    mean_judge = sum(float(v["judge_score"]) for v in variants) / max(len(variants), 1)
    score_delta = float(adopted["judge_score"]) - mean_judge

    # Update the bandit: reward the winning op, mildly decay the rest.
    bandit = _bandit_for(job["monster_id"])
    bandit[adopted_op] = bandit.get(adopted_op, 1.0) + 1.0

    # Persist: adopt genome + TrainingArtifact(kind='grpo').
    if monster is not None:
        genome_mod.apply_genome(
            session,
            monster,
            adopted_genome,
            kind="grpo",
            score_delta=score_delta,
            accepted=True,
            before=job["base_genome"],
        )

    job["status"] = "done"
    job["score_delta"] = score_delta
    job["adopted_variant_id"] = adopted_id

    return {
        "job_id": job_id,
        "adopted_variant_id": adopted_id,
        "adopted_op": adopted_op,
        "score_delta": score_delta,
        "advantages": advantages,
        "group_mean": group_mean,
        "bandit": dict(bandit),
        "genome": adopted_genome,
    }


# --------------------------------------------------------------------- helpers


def _to_utterance(u: dict[str, Any]) -> Utterance:
    return Utterance(
        turn=int(u.get("turn", 0)),
        actor_id=str(u.get("actor_id", "?")),
        actor_role=u.get("actor_role", "party"),
        skill_used=u.get("skill_used"),
        text=str(u.get("text", "")),
        ts=float(u.get("ts", 0.0)),
    )


def _topic_for(monster: Any) -> str:
    persona = getattr(monster, "persona", None) or {}
    return persona.get("topic") or "Social media does more harm than good."
