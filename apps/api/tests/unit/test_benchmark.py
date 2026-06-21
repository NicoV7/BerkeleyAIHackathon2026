"""Unit tests for app.training.benchmark (Wave A — measurable train delta).

All tests are pure + offline: ``selfplay.play`` is monkeypatched so no Ollama /
gateway is touched. We assert the four properties the harness exists to provide:

  * N-run averaging produces the mean judge score.
  * Determinism: a fixed per-run-index seed feeds reproducible inputs into
    selfplay (same seed -> same topic/model/genome on each run index).
  * win_rate is the fraction of runs scoring strictly above the win threshold.
  * The baseline cache avoids re-running self-play for the same
    (monster_id, genome_version) key, and invalidates when the version bumps.

Style: Arrange-Act-Assert with descriptive names.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.training import benchmark as B

pytestmark = pytest.mark.asyncio


def _genome() -> dict[str, Any]:
    return {
        "harness": {"system_prompt": "You are a debater.", "directives": []},
        "persona": {"name": "Aristotle", "tone": "calm and methodical"},
        "skill_prompt_fragments": [],
        "gambit_rules": [],
        "skills": [],
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    """Each test starts with an empty baseline cache."""
    B.clear_cache()
    yield
    B.clear_cache()


# --------------------------------------------------------------------------- #
# Averaging over N runs
# --------------------------------------------------------------------------- #


async def test_run_benchmark_averages_judge_score_over_n_runs(monkeypatch):
    # Arrange: scripted scores per call -> mean = (40+60+80)/3 = 60.
    scores = iter([40.0, 60.0, 80.0])

    async def fake_play(genome, **kwargs):
        return {"score": next(scores), "transcript": [], "source": "stub"}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act
    out = await B.run_benchmark(_genome(), n_runs=3)

    # Assert: judge_score is the average; win_rate counts runs > 50 (60 & 80 -> 2/3).
    assert out["judge_score"] == pytest.approx(60.0)
    assert out["win_rate"] == pytest.approx(2 / 3)


async def test_run_benchmark_runs_play_exactly_n_times(monkeypatch):
    # Arrange
    calls: list[dict[str, Any]] = []

    async def fake_play(genome, **kwargs):
        calls.append(kwargs)
        return {"score": 55.0, "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act
    await B.run_benchmark(_genome(), n_runs=5)

    # Assert
    assert len(calls) == 5


# --------------------------------------------------------------------------- #
# Determinism: fixed per-index seed -> reproducible inputs
# --------------------------------------------------------------------------- #


async def test_run_benchmark_is_deterministic_across_invocations(monkeypatch):
    # Arrange: score depends on the RNG state at call time, so identical seeding
    # per run index must yield identical sequences across two invocations.
    import random

    async def fake_play(genome, **kwargs):
        return {"score": float(random.randint(0, 100)), "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act
    a = await B.run_benchmark(_genome(), n_runs=4)
    b = await B.run_benchmark(_genome(), n_runs=4)

    # Assert: same seeds -> same averaged result run-to-run.
    assert a == b


async def test_run_benchmark_pins_judge_model(monkeypatch):
    # Arrange: capture the model selfplay is invoked with.
    seen: list[str | None] = []

    async def fake_play(genome, **kwargs):
        seen.append(kwargs.get("model"))
        return {"score": 50.0, "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act
    await B.run_benchmark(_genome(), n_runs=2, judge_model="strong-judge")

    # Assert: the stronger judge model is threaded through to self-play.
    assert seen == ["strong-judge", "strong-judge"]


# --------------------------------------------------------------------------- #
# win_rate computation
# --------------------------------------------------------------------------- #


async def test_win_rate_is_fraction_of_runs_above_threshold(monkeypatch):
    # Arrange: 51 wins (>50), exactly 50 does NOT count, 49 loses -> 1/3.
    scores = iter([51.0, 50.0, 49.0])

    async def fake_play(genome, **kwargs):
        return {"score": next(scores), "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act
    out = await B.run_benchmark(_genome(), n_runs=3)

    # Assert
    assert out["win_rate"] == pytest.approx(1 / 3)
    assert 0.0 <= out["win_rate"] <= 1.0


async def test_win_rate_all_wins_and_all_losses(monkeypatch):
    # Arrange / Act: all high.
    async def all_high(genome, **kwargs):
        return {"score": 90.0, "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", all_high)
    high = await B.run_benchmark(_genome(), n_runs=3)

    # ...all low.
    async def all_low(genome, **kwargs):
        return {"score": 10.0, "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", all_low)
    low = await B.run_benchmark(_genome(), n_runs=3)

    # Assert
    assert high["win_rate"] == 1.0
    assert low["win_rate"] == 0.0


async def test_score_is_clamped_into_0_100(monkeypatch):
    # Arrange: a misbehaving judge returns out-of-range scores.
    scores = iter([-20.0, 150.0])

    async def fake_play(genome, **kwargs):
        return {"score": next(scores), "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act
    out = await B.run_benchmark(_genome(), n_runs=2)

    # Assert: clamped to [0, 100] -> mean of (0, 100) = 50.
    assert out["judge_score"] == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# Graceful degradation
# --------------------------------------------------------------------------- #


async def test_failed_play_degrades_to_neutral_score(monkeypatch):
    # Arrange: self-play raises (gateway down).
    async def boom(genome, **kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(B.selfplay, "play", boom)

    # Act: must not raise; failed runs count as neutral 50 (not a win).
    out = await B.run_benchmark(_genome(), n_runs=3)

    # Assert
    assert out["judge_score"] == pytest.approx(50.0)
    assert out["win_rate"] == 0.0


# --------------------------------------------------------------------------- #
# Baseline caching
# --------------------------------------------------------------------------- #


async def test_baseline_cache_avoids_rerunning_selfplay(monkeypatch):
    # Arrange: count how many times self-play is invoked.
    count = {"n": 0}

    async def fake_play(genome, **kwargs):
        count["n"] += 1
        return {"score": 70.0, "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act: two cached calls for the same (monster, version).
    first = await B.baseline_benchmark(
        _genome(), monster_id="m1", genome_version=1, n_runs=3
    )
    second = await B.baseline_benchmark(
        _genome(), monster_id="m1", genome_version=1, n_runs=3
    )

    # Assert: identical result, self-play only ran for the FIRST call (3 runs).
    assert first == second
    assert count["n"] == 3


async def test_baseline_cache_invalidates_on_version_bump(monkeypatch):
    # Arrange
    count = {"n": 0}

    async def fake_play(genome, **kwargs):
        count["n"] += 1
        return {"score": 70.0, "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act: same monster, different genome_version -> different cache key.
    await B.baseline_benchmark(_genome(), monster_id="m1", genome_version=1, n_runs=2)
    await B.baseline_benchmark(_genome(), monster_id="m1", genome_version=2, n_runs=2)

    # Assert: both versions ran (2 + 2 self-play calls), no stale reuse.
    assert count["n"] == 4


async def test_run_benchmark_without_cache_keys_does_not_cache(monkeypatch):
    # Arrange
    count = {"n": 0}

    async def fake_play(genome, **kwargs):
        count["n"] += 1
        return {"score": 70.0, "transcript": []}

    monkeypatch.setattr(B.selfplay, "play", fake_play)

    # Act: no monster_id/genome_version -> use_cache has nothing to key on.
    await B.run_benchmark(_genome(), n_runs=2, use_cache=True)
    await B.run_benchmark(_genome(), n_runs=2, use_cache=True)

    # Assert: ran both times (no caching without a key).
    assert count["n"] == 4
