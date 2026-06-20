"""B3 — capture -> train smoke test (Wave 1 risky-glue coverage).

The hackathon climax is "capture the winner, train it, watch it get measurably
better." This is the riskiest demo path (sync GEPA on CPU, a non-deterministic
delta, three different score scales). This module is a fast, hermetic smoke test
that proves the train beat's CONTRACT holds without standing up Postgres/Ollama:

  * ``app.training.demo.run_demo_training`` (A1) returns a well-shaped
    {before, after, delta, genome_version, source} payload with a non-negative,
    floored delta — exercised via its no-I/O replay artifact and via a fully
    stubbed self-play + GEPA run (no DB, no network).
  * ``app.training.gepa.run_gepa`` bumps ``genome_version`` when a variant beats
    the incumbent (the "it got better" proof), driven by a monkeypatched
    self-play scorer so no model is called.
  * The "no improvement" branch (delta <= 0 -> the honest "held its ground"
    UI state) is representable: a constant-score scorer yields delta == 0.

Collection is ALWAYS safe on the host: every assertion above runs in-process
against fake monster/session objects. The genuinely DB-dependent end-to-end
variant (a real Monster row + a live ``run_demo_training`` against Postgres) is
guarded by a connection probe and skipped when the DB is unavailable, so
``pytest --collect-only`` never imports or touches a database.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

# Import the modules under test at collection time. These are pure-Python imports
# (no I/O on import — see A1's module docstring), so collection stays host-safe.
demo = pytest.importorskip("app.training.demo")
gepa = pytest.importorskip("app.training.gepa")
selfplay = pytest.importorskip("app.training.selfplay")


# --------------------------------------------------------------------------- fakes


class FakeMonster:
    """A minimal Monster stand-in exposing the attrs read_genome / apply_genome
    touch. ``genome_version`` starts at 1 and is bumped in-memory by
    ``apply_genome`` (the bump we assert on)."""

    def __init__(self) -> None:
        self.id = "monster-test"
        self.harness: dict[str, Any] = {}
        self.persona: dict[str, Any] = {"name": "Testitron", "topic": "X is good"}
        self.skills: list[Any] = []
        self.genome_version = 1


class FakeSession:
    """A no-op async-ish session. ``apply_genome`` only calls ``add``; nothing is
    committed, so no DB is touched. ``run_gepa`` may call ``execute`` (to load
    battle memories) — we raise so the caller's try/except yields an empty list,
    short-circuiting any gateway/LLM use."""

    def add(self, *_a: Any, **_k: Any) -> None:  # noqa: D401
        return None

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no DB in smoke test")


def _const_scorer(score: float):
    async def _play(_genome: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return {"score": score, "transcript": []}

    return _play


def _improving_scorer(base: float = 50.0):
    """Score rises with the genome's accumulated edge so GEPA variants beat the
    flat incumbent — yielding a positive delta and a genome_version bump."""

    async def _play(genome: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        directives = (genome.get("harness", {}) or {}).get("directives", []) or []
        fragments = genome.get("skill_prompt_fragments", []) or []
        score = base + 10.0 * len(directives) + 5.0 * len(fragments)
        return {"score": score, "transcript": []}

    return _play


# ------------------------------------------------------ demo replay (no I/O at all)


def test_demo_replay_artifact_positive_delta_and_version_bump() -> None:
    """The zero-wait replay path returns a non-negative delta and a bumped
    genome_version (1 -> 2). No DB, no monkeypatching needed."""
    out = demo._replay_artifact("monster-test", demo.DEMO_MIN_DELTA)

    assert out["monster_id"] == "monster-test"
    assert out["source"] == "replay"
    assert out["delta"] >= 0.0
    assert out["delta"] >= demo.DEMO_MIN_DELTA
    assert out["after"] >= out["before"]
    # genome_version reflects a real bump past the starting version (1).
    assert out["genome_version"] >= 2
    # Score scales must not be conflated — the payload self-labels as self-play.
    assert "self-play" in out["label"]


def test_demo_run_via_replay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the replay env flag set, ``run_demo_training`` returns the reliably
    positive artifact without needing a session or DB."""
    monkeypatch.setenv(demo.DEMO_REPLAY_ENV, "1")
    out = asyncio.run(demo.run_demo_training(session=None, monster_id="m-replay"))

    assert out["source"] == "replay"
    assert out["delta"] >= 0.0
    assert out["genome_version"] >= 2
    assert out["after"] >= out["before"]


def test_demo_run_with_missing_monster_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad/missing monster id never crashes the demo: ``run_demo_training``
    gracefully degrades to the replay artifact (still a positive delta)."""
    monkeypatch.delenv(demo.DEMO_REPLAY_ENV, raising=False)
    # session=None -> _load_monster returns None -> replay fallback.
    out = asyncio.run(demo.run_demo_training(session=None, monster_id="nope"))

    assert out["delta"] >= 0.0
    assert out["genome_version"] >= 2


# ----------------------------------------------------- gepa (stubbed self-play)


def test_gepa_improvement_bumps_genome_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a variant beats the incumbent, GEPA reports a positive delta and
    ``apply_genome`` bumps the monster's genome_version (the 'it got better'
    proof). Driven by a stubbed scorer — no model, no DB."""
    monkeypatch.setattr(selfplay, "play", _improving_scorer())

    monster = FakeMonster()
    assert monster.genome_version == 1

    best_genome, delta = asyncio.run(
        gepa.run_gepa(FakeSession(), monster, rounds=1, topic="X is good", persist=True)
    )

    assert isinstance(best_genome, dict)
    assert delta >= 0.0  # never negative: best starts at the incumbent baseline
    assert delta > 0.0   # a winning variant existed -> strictly positive here
    # apply_genome (accepted, delta>=0) bumped the in-memory monster version.
    assert monster.genome_version == 2


def test_gepa_no_improvement_branch_representable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 'no improvement' branch (delta <= 0 -> honest 'held its ground' UI
    state) is representable: a constant scorer means no variant beats the
    incumbent, so delta == 0 and the genome is unchanged."""
    monkeypatch.setattr(selfplay, "play", _const_scorer(70.0))

    monster = FakeMonster()
    _best_genome, delta = asyncio.run(
        gepa.run_gepa(FakeSession(), monster, rounds=1, topic="X is good", persist=True)
    )

    assert delta <= 0.0
    assert delta == 0.0  # honest "held its ground" outcome


def test_demo_training_via_stubbed_gepa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise ``run_demo_training`` end-to-end against a fake monster/session
    with self-play stubbed, asserting the floored, non-negative delta and a
    genome_version bump — all without a DB or model.

    A weak baseline (flat) self-play scores low; the GEPA run improves it; the
    reported delta is floored at ``min_delta`` so the demo climax is reliably
    positive."""
    monkeypatch.delenv(demo.DEMO_REPLAY_ENV, raising=False)
    monkeypatch.setattr(selfplay, "play", _improving_scorer())

    monster = FakeMonster()
    # Avoid the SQLAlchemy/Monster import path in demo._load_monster by patching
    # it to return our fake monster directly (no DB).
    async def _fake_load(_session: Any, _mid: str) -> Any:
        return monster

    monkeypatch.setattr(demo, "_load_monster", _fake_load)

    out = asyncio.run(
        demo.run_demo_training(FakeSession(), monster.id, topic="X is good")
    )

    assert out["source"] == "gepa"
    assert out["delta"] >= 0.0
    assert out["delta"] >= demo.DEMO_MIN_DELTA  # floored, reliably positive
    assert out["after"] >= out["before"]
    assert 0.0 <= out["before"] <= 100.0
    assert 0.0 <= out["after"] <= 100.0
    # genome_version was bumped by GEPA's apply_genome (weak baseline + gepa run).
    assert out["genome_version"] >= 2
    assert "self-play" in out["label"]


# ----------------------------------------------- DB-dependent end-to-end (guarded)


def _db_available() -> bool:
    """Best-effort probe: can we actually reach the configured Postgres?

    Returns False (so the test SKIPS) when the DB url is unset, points at an
    unreachable host (e.g. the docker-only ``postgres`` hostname on the host),
    or the connection fails for any reason. Never raises — collection stays safe.
    """
    if os.environ.get("SKIP_DB_TESTS", "").strip().lower() in ("1", "true", "yes"):
        return False
    try:
        from app.config import settings
        from app.db.session import engine

        async def _ping() -> bool:
            from sqlalchemy import text

            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True

        # A docker-internal hostname won't resolve on the host: bail fast.
        if "postgres:5432" in (settings.database_url or "") and not os.environ.get(
            "DATABASE_URL"
        ):
            return False
        return asyncio.run(asyncio.wait_for(_ping(), timeout=3.0))
    except Exception:  # noqa: BLE001 — any failure -> DB unavailable -> skip
        return False


@pytest.mark.skipif(not _db_available(), reason="Postgres unavailable on host")
def test_capture_train_end_to_end_with_db() -> None:
    """Integration variant: create a Monster, run the real demo train beat
    against Postgres, and assert a non-negative delta + a genome_version bump.

    SKIPPED whenever the DB is unreachable (the host default), so this never
    blocks ``--collect-only`` or a host-only test run. Self-play still calls the
    real model here, which is why this is gated behind a live DB.
    """
    from app.db.models import Monster
    from app.db.session import SessionLocal

    async def _run() -> dict[str, Any]:
        async with SessionLocal() as session:
            monster = Monster(
                name="SmokeTestDebater",
                persona={"name": "SmokeTestDebater", "topic": "Testing is good"},
                harness={},
                skills=[],
            )
            session.add(monster)
            await session.commit()
            await session.refresh(monster)
            start_version = int(monster.genome_version or 1)

            out = await demo.run_demo_training(
                session, str(monster.id), topic="Testing is good"
            )
            await session.commit()
            await session.refresh(monster)
            out["_start_version"] = start_version
            out["_final_version"] = int(monster.genome_version or 1)
            return out

    out = asyncio.run(_run())

    assert out["delta"] >= 0.0
    assert out["after"] >= out["before"]
    # genome_version really advanced on the persisted row.
    assert out["_final_version"] > out["_start_version"]
    assert out["genome_version"] >= 2
