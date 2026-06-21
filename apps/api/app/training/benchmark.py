"""Benchmark harness (Wave A) — make the train -> rematch delta MEASURABLE.

The core differentiator of the Debate RPG is that training a monster visibly
improves it. But the judge is a CPU-Ollama model evaluating non-deterministic
debaters (``selfplay._local_self_play`` runs debaters at ``temperature=0.8``), so
a single self-play score is noisy and a raw before/after comparison is unreadable.

``run_benchmark`` denoises by:
  * Running self-play N times against a FIXED topic.
  * Seeding Python's global ``random`` with a deterministic, per-run-index seed
    before each run, so the genome sampling / any RNG inside the play path is
    reproducible run-to-run (same seed -> same inputs). Only the run index varies.
  * Averaging the judge scores over the N runs and reporting:
        win_rate    = fraction of runs whose party score > 50
        judge_score = mean party score (0..100)
  * Pinning the judge to a stronger model when available (``judge_model``,
    defaulting to ``settings.llm_judge_model``) so the "after" measurement is as
    legible as the local stack allows.

It also caches the per-(monster_id, genome_version) baseline in an in-process
dict so the "before" side of a Scorecard is computed once, not on every train
call (training mutates the genome and bumps ``genome_version``, so the cache key
naturally invalidates when the genome changes).

Public surface (stable):
    async def run_benchmark(genome, *, topic=None, n_runs=3, model="default",
                            judge_model=None, monster_id=None, genome_version=None,
                            use_cache=False) -> {"win_rate": float, "judge_score": float}
    async def baseline_benchmark(...)  -> cached convenience wrapper for the "before" side
    def clear_cache()                  -> reset the in-process baseline cache (tests)
"""
from __future__ import annotations

import random
from typing import Any, Optional

from app.config import settings
from app.training import selfplay

# Sensible defaults: 3 runs is enough to wash out most single-roll judge noise
# while staying cheap on a CPU stack.
DEFAULT_N_RUNS = 3
DEFAULT_TOPIC = "Social media does more harm than good."
WIN_THRESHOLD = 50.0  # party score strictly above this counts as a win
# Deterministic base seed; per-run seed = _SEED_BASE + run_index.
_SEED_BASE = 1000

# In-process baseline cache: (monster_id, genome_version) -> {"win_rate", "judge_score"}.
# Lets the "before" side of a Scorecard be reused across train calls without
# re-running self-play. Bumping genome_version (what training does) invalidates it.
_BASELINE_CACHE: dict[tuple[str, int], dict[str, float]] = {}


def clear_cache() -> None:
    """Drop all cached baselines (used by tests; safe to call any time)."""
    _BASELINE_CACHE.clear()


def _cache_key(monster_id: Optional[str], genome_version: Optional[int]) -> Optional[tuple[str, int]]:
    if monster_id is None or genome_version is None:
        return None
    return (str(monster_id), int(genome_version))


async def run_benchmark(
    genome: dict[str, Any],
    *,
    topic: Optional[str] = None,
    n_runs: int = DEFAULT_N_RUNS,
    model: str = selfplay.DEFAULT_MODEL,
    judge_model: Optional[str] = None,
    monster_id: Optional[str] = None,
    genome_version: Optional[int] = None,
    use_cache: bool = False,
) -> dict[str, float]:
    """Benchmark a genome by averaging N denoised self-play runs.

    Returns ``{"win_rate": float in 0..1, "judge_score": float in 0..100}``.

    Denoising: each run index ``i`` seeds the global RNG with ``_SEED_BASE + i``
    before calling ``selfplay.play`` so the inputs are reproducible run-to-run
    (determinism: same seed -> same inputs). Scores are averaged across runs and
    ``win_rate`` is the fraction of runs scoring above ``WIN_THRESHOLD``.

    ``judge_model`` pins the judge/self-play model to a stronger model when one is
    available; it defaults to ``settings.llm_judge_model``. (``selfplay.play``
    uses a single model for debaters + judge, so the strongest available model is
    the most legible choice.)

    When ``use_cache`` is True and both ``monster_id`` and ``genome_version`` are
    given, the result is read from / written to the in-process baseline cache so
    the "before" side isn't re-run on every train call.
    """
    n = max(1, int(n_runs))
    topic = topic or DEFAULT_TOPIC
    bench_model = judge_model or model or settings.llm_judge_model

    key = _cache_key(monster_id, genome_version)
    if use_cache and key is not None and key in _BASELINE_CACHE:
        return dict(_BASELINE_CACHE[key])

    scores: list[float] = []
    for i in range(n):
        # Fixed seed per run index -> reproducible inputs; vary only by index.
        random.seed(_SEED_BASE + i)
        try:
            result = await selfplay.play(
                genome,
                topic=topic,
                rounds=1,
                model=bench_model,
            )
            score = float(result.get("score", WIN_THRESHOLD))
        except Exception:  # noqa: BLE001 — graceful degradation: a failed run
            # is a neutral 50 rather than crashing the whole benchmark.
            score = WIN_THRESHOLD
        # Clamp into the documented 0..100 range so downstream math is sane.
        score = max(0.0, min(100.0, score))
        scores.append(score)

    judge_score = sum(scores) / len(scores)
    wins = sum(1 for s in scores if s > WIN_THRESHOLD)
    win_rate = wins / len(scores)

    out = {"win_rate": float(win_rate), "judge_score": float(judge_score)}

    if use_cache and key is not None:
        _BASELINE_CACHE[key] = dict(out)

    return out


async def baseline_benchmark(
    genome: dict[str, Any],
    *,
    monster_id: Optional[str],
    genome_version: Optional[int],
    topic: Optional[str] = None,
    n_runs: int = DEFAULT_N_RUNS,
    model: str = selfplay.DEFAULT_MODEL,
    judge_model: Optional[str] = None,
) -> dict[str, float]:
    """Cached convenience wrapper for the "before" side of a Scorecard.

    Identical to ``run_benchmark(..., use_cache=True)`` but with a name that makes
    intent obvious at the call site in the training router.
    """
    return await run_benchmark(
        genome,
        topic=topic,
        n_runs=n_runs,
        model=model,
        judge_model=judge_model,
        monster_id=monster_id,
        genome_version=genome_version,
        use_cache=True,
    )
