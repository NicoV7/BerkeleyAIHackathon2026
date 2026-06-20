"""Meta-tests: validate the shared test infrastructure in conftest.py.

These guard the fixtures themselves (gateway mock, factories, DB probe) so a
regression in the test harness surfaces as a failing test rather than silent,
mysterious breakage across the whole suite. No live services required.
"""
from __future__ import annotations

import pytest

from app.db.models import DebateType, Monster, MonsterOwner, Run, RunStatus


# --------------------------------------------------------------------------- #
# gateway_mock
# --------------------------------------------------------------------------- #


async def test_gateway_mock_complete_is_deterministic_and_records_calls(
    gateway_mock, sample_messages
):
    # Arrange
    import app.gateway.gateway as gw_module

    # Act
    first = await gw_module.gateway.complete(sample_messages, model="judge")
    second = await gw_module.gateway.complete(sample_messages, model="judge")

    # Assert
    assert first == second  # deterministic
    assert "Make your opening argument." in first
    assert len(gateway_mock.complete_calls) == 2
    assert gateway_mock.complete_calls[0]["model"] == "judge"


async def test_gateway_mock_complete_json_mode_returns_parseable_json(gateway_mock, sample_messages):
    # Arrange
    import json

    import app.gateway.gateway as gw_module

    # Act
    out = await gw_module.gateway.complete(sample_messages, json_mode=True)
    parsed = json.loads(out)

    # Assert
    assert parsed["score"] == 5
    assert "rationale" in parsed


async def test_gateway_mock_stream_yields_reconstructable_text(gateway_mock, sample_messages):
    # Arrange
    import app.gateway.gateway as gw_module

    # Act
    chunks = [c async for c in gw_module.gateway.stream(sample_messages)]

    # Assert
    assert chunks  # produced at least one chunk
    assert "argument." in "".join(chunks)
    assert len(gateway_mock.stream_calls) == 1


async def test_gateway_mock_embed_is_fixed_dimension_and_deterministic(gateway_mock):
    # Arrange / Act
    import app.gateway.gateway as gw_module

    vecs_a = await gw_module.gateway.embed(["hello", "world!!"])
    vecs_b = await gw_module.gateway.embed(["hello", "world!!"])

    # Assert
    assert len(vecs_a) == 2
    assert all(len(v) == gateway_mock.EMBED_DIM for v in vecs_a)
    assert vecs_a == vecs_b  # deterministic
    assert len(gateway_mock.embed_calls) == 2


# --------------------------------------------------------------------------- #
# factories
# --------------------------------------------------------------------------- #


def test_make_run_returns_run_with_defaults(make_run):
    # Act
    run = make_run()

    # Assert
    assert isinstance(run, Run)
    assert run.status == RunStatus.active
    assert run.id  # default_factory populated a uuid


def test_make_run_applies_overrides(make_run):
    # Act
    run = make_run(debate_topic="AI safety", seed=42)

    # Assert
    assert run.debate_topic == "AI safety"
    assert run.seed == 42


def test_make_monster_returns_self_consistent_monster(make_monster):
    # Act
    monster = make_monster()

    # Assert
    assert isinstance(monster, Monster)
    assert monster.owner == MonsterOwner.player
    assert monster.type == DebateType.logos
    assert monster.run_id  # auto-generated run id


def test_make_monster_honors_run_id_and_overrides(make_monster, make_run):
    # Arrange
    run = make_run()

    # Act
    monster = make_monster(run_id=run.id, name="Logosaurus", type=DebateType.chaos)

    # Assert
    assert monster.run_id == run.id
    assert monster.name == "Logosaurus"
    assert monster.type == DebateType.chaos


# --------------------------------------------------------------------------- #
# DB probe / require_db
# --------------------------------------------------------------------------- #


def test_db_available_is_boolean(db_available):
    # Assert: the probe always resolves to a concrete answer (never raises).
    assert isinstance(db_available, bool)


def test_require_db_skips_or_passes(require_db):
    # If this body runs at all, a host-reachable Postgres exists; otherwise the
    # require_db fixture skipped us. Either outcome keeps collection green.
    assert True
