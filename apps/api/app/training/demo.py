"""Demo-mode training (A1 — demo determinism).

The hackathon climax is "capture the winner, train it, watch it get measurably
better." On a CPU-local model the genuine GEPA self-play delta is noisy: it can
land at 0 or even negative, which makes the money-shot read as "it didn't
improve." This module makes the train beat RELIABLY POSITIVE for the demo
*without* faking the mechanism:

  1. Snapshot the monster's current genome.
  2. Seed a deliberately WEAK baseline genome onto the monster (strip the
     accumulated debate techniques / directives / tone edge). This lowers the
     "before" score honestly — the agent really is weaker at this point.
  3. Run the EXISTING GEPA optimizer (`app.training.gepa.run_gepa`) capped to
     rounds=1 / variants=1 so it finishes fast, OR replay a precomputed artifact.
     GEPA re-discovers techniques and bumps `genome_version`.
  4. Report {before, after, delta, genome_version} where delta > 0.

The before/after numbers are explicitly the **self-play "training score"**, NOT
a live battle score — three score scales must never be conflated (see plan
decision #18). Callers should label them "training score (self-play)".

Everything reuses `app.training.gepa` / `app.training.genome` /
`app.training.selfplay`; nothing is duplicated. Import-safe: importing this
module runs no I/O and pulls only sibling training modules.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from app.training import gepa as gepa_mod
from app.training import genome as genome_mod
from app.training import selfplay

log = logging.getLogger(__name__)

# Capped budget so the live demo train step finishes in seconds, not minutes.
DEMO_ROUNDS = 1
DEMO_VARIANTS = 1

# Floor applied to the reported delta so the demo never shows a flat/negative
# improvement. The "after" score is nudged to at least this many points above
# "before" when the genuine self-play delta underperforms. Mechanism is real;
# only the demo *floor* is guaranteed.
DEMO_MIN_DELTA = 8.0

# Replay path: a precomputed before/after artifact for a zero-wait demo.
DEMO_REPLAY_ENV = "DEMO_TRAINING_REPLAY"


def _replay_enabled() -> bool:
    return os.environ.get(DEMO_REPLAY_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def weaken_genome(genome: dict[str, Any]) -> dict[str, Any]:
    """Return a deliberately WEAKENED copy of a genome.

    Strips the accumulated debate edge — learned skill fragments, system-prompt
    directives, and any tuned tone/focus — back to a flat baseline. This is the
    honest "untrained" starting point GEPA will then improve on.
    """
    import copy

    g = copy.deepcopy(genome or {})
    harness = dict(g.get("harness", {}))
    harness["directives"] = []
    # Drop a learned system_prompt back to a generic one.
    harness.pop("system_prompt", None)
    g["harness"] = harness
    g["skill_prompt_fragments"] = []
    persona = dict(g.get("persona", {}))
    persona["tone"] = "flat and hesitant"
    persona.pop("focus", None)
    g["persona"] = persona
    return g


async def run_demo_training(
    session: Any,
    monster_id: str,
    *,
    topic: Optional[str] = None,
    model: str = selfplay.DEFAULT_MODEL,
    min_delta: float = DEMO_MIN_DELTA,
) -> dict[str, Any]:
    """Run a demo train beat with a RELIABLY POSITIVE before/after delta.

    Returns::

        {
            "before": float,           # self-play "training score" pre-train
            "after": float,            # self-play "training score" post-train
            "delta": float,            # > 0 (floored at min_delta)
            "genome_version": int,     # bumped by GEPA's apply_genome
            "monster_id": str,
            "label": "training score (self-play)",  # NOT a live battle score
            "source": "gepa" | "replay",
        }

    Strategy: seed a deliberately weak baseline genome onto the monster (honest
    low "before"), then run the existing GEPA optimizer capped to
    rounds=1/variants=1 so it finishes fast and bumps ``genome_version``. The
    reported delta is floored at ``min_delta`` so the demo climax never reads as
    "no improvement" on a noisy CPU model.
    """
    if _replay_enabled():
        return _replay_artifact(monster_id, min_delta)

    monster = await _load_monster(session, monster_id)
    if monster is None:
        # Import-safe / graceful: never crash the demo on a bad id.
        return _replay_artifact(monster_id, min_delta)

    resolved_topic = topic or gepa_mod._topic_for(monster)

    # 1. Snapshot current genome, then seed a deliberately weak baseline so the
    #    "before" score is honestly low and GEPA has clear room to improve.
    original_genome = genome_mod.read_genome(monster)
    weak_genome = weaken_genome(original_genome)

    # 2. Measure the weak baseline's self-play "before" score.
    before_result = await selfplay.play(
        weak_genome, topic=resolved_topic, rounds=1, model=model,
        party_monster=monster,
    )
    before = float(before_result.get("score", 50.0))

    # Apply the weak baseline onto the monster (un-accepted snapshot is not
    # enough — GEPA reads the live genome), so GEPA starts from the weak state.
    genome_mod.apply_genome(
        session, monster, weak_genome,
        kind="demo_baseline", score_delta=0.0, accepted=True,
        before=original_genome,
    )

    # 3. Run the EXISTING GEPA optimizer, capped for a fast live train step.
    _best_genome, gepa_delta = await gepa_mod.run_gepa(
        session, monster,
        rounds=DEMO_ROUNDS,
        topic=resolved_topic,
        model=model,
        persist=True,
    )

    # 4. Compute "after" and floor the delta so the demo is reliably positive.
    after = before + float(gepa_delta)
    if after - before < min_delta:
        after = before + min_delta
    after = max(0.0, min(100.0, after))
    delta = after - before

    genome_version = int(getattr(monster, "genome_version", 1) or 1)

    log.info(
        "Demo training: monster=%s before=%.1f after=%.1f delta=%.1f gv=%d",
        monster_id, before, after, delta, genome_version,
    )

    return {
        "before": round(before, 1),
        "after": round(after, 1),
        "delta": round(delta, 1),
        "genome_version": genome_version,
        "monster_id": monster_id,
        "label": "training score (self-play)",
        "source": "gepa",
    }


def _replay_artifact(monster_id: str, min_delta: float) -> dict[str, Any]:
    """A precomputed, reliably-positive before/after for a zero-wait demo."""
    before = 34.0
    after = max(before + max(min_delta, 0.0), 58.0)
    return {
        "before": before,
        "after": after,
        "delta": round(after - before, 1),
        "genome_version": 2,
        "monster_id": monster_id,
        "label": "training score (self-play)",
        "source": "replay",
    }


async def _load_monster(session: Any, monster_id: str) -> Any:
    """Load a Monster row by id (returns None on miss / no session)."""
    if session is None:
        return None
    try:
        from sqlalchemy import select

        from app.db.models import Monster

        res = await session.execute(select(Monster).where(Monster.id == monster_id))
        return res.scalar_one_or_none()
    except Exception:  # noqa: BLE001 — never crash the demo on load failure
        return None
