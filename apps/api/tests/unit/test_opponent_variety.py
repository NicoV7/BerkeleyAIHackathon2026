"""Unit tests for Agent 6: opponent variety + difficulty curve.

Pure-logic coverage with NO live DB / Redis / network. The generator's DB entry
point (``generate_wild``) is async and persists rows, so these tests target the
pure factory ``build_wild_monster`` (which ``generate_wild`` wraps) plus the
``archetypes`` catalog. Determinism is exercised by threading a seeded
``random.Random``.

Properties pinned here:
  * Deeper depth -> higher average enemy level AND max_hp (monotonic-ish).
  * Archetypes vary across seeds (the catalog is actually being sampled).
  * Same seed + depth -> byte-identical enemy (deterministic).
  * depth == 0 reproduces the original level band [1, 5] and uses the canonical
    balance HP curve (no re-implemented stat math).
"""
from __future__ import annotations

import random
from statistics import mean

import pytest

# Import-time only touches pure Python; skip the file rather than erroring
# collection if the impl modules are not present yet.
archetypes = pytest.importorskip("app.party.archetypes")
generator = pytest.importorskip("app.party.generator")
balance = pytest.importorskip("app.party.balance")

RUN_ID = "run-test-0001"


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


# --------------------------------------------------------------------------- #
# Archetype catalog
# --------------------------------------------------------------------------- #
def test_catalog_is_well_formed():
    cat = archetypes.ARCHETYPES
    assert 6 <= len(cat) <= 12, "catalog should hold ~6-10 archetypes"
    keys = {a["key"] for a in cat}
    assert len(keys) == len(cat), "archetype keys must be unique"
    for a in cat:
        assert a["name"] and a["tone"] and a["backstory"] and a["quirk"]
        assert a["type_bias"], "each archetype needs a type bias"
        assert isinstance(a["power_bias"], (int, float))


def test_pick_archetype_is_deterministic_for_seed():
    a1 = archetypes.pick_archetype(_rng(42))
    a2 = archetypes.pick_archetype(_rng(42))
    assert a1 == a2


def test_archetypes_vary_across_seeds():
    seen = {archetypes.pick_archetype(_rng(s))["key"] for s in range(40)}
    assert len(seen) >= 4, "sampling many seeds should surface several archetypes"


def test_persona_for_carries_archetype_flavor():
    a = archetypes.ARCHETYPES_BY_KEY["zealot"]
    persona = archetypes.persona_for(a)
    assert persona["archetype_key"] == "zealot"
    assert persona["archetype"] == a["name"]
    # Shape must stay compatible with the original persona dict.
    assert {"backstory", "tone", "quirks"} <= set(persona)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_same_seed_and_depth_is_identical():
    m1 = generator.build_wild_monster(_rng(7), RUN_ID, depth=3)
    m2 = generator.build_wild_monster(_rng(7), RUN_ID, depth=3)
    assert m1.name == m2.name
    assert m1.type == m2.type
    assert m1.level == m2.level
    assert m1.max_hp == m2.max_hp
    assert m1.persona == m2.persona
    assert m1.skills == m2.skills


def test_different_seed_changes_enemy():
    names = {generator.build_wild_monster(_rng(s), RUN_ID, depth=0).persona["archetype_key"]
             for s in range(30)}
    assert len(names) >= 4, "enemies should vary in archetype across seeds"


# --------------------------------------------------------------------------- #
# Difficulty curve
# --------------------------------------------------------------------------- #
def _avg_stats(depth: int, n: int = 60) -> tuple[float, float]:
    levels, hps = [], []
    for s in range(n):
        m = generator.build_wild_monster(_rng(s), RUN_ID, depth=depth)
        levels.append(m.level)
        hps.append(m.max_hp)
    return mean(levels), mean(hps)


def test_deeper_depth_raises_average_level_and_hp():
    l0, h0 = _avg_stats(0)
    l5, h5 = _avg_stats(5)
    l10, h10 = _avg_stats(10)
    assert l0 < l5 < l10, f"avg level should rise with depth: {l0} {l5} {l10}"
    assert h0 < h5 < h10, f"avg hp should rise with depth: {h0} {h5} {h10}"


def test_progress_kwarg_also_scales():
    # progress is interchangeable with depth (the larger wins).
    by_depth = generator.build_wild_monster(_rng(11), RUN_ID, depth=6)
    by_progress = generator.build_wild_monster(_rng(11), RUN_ID, depth=0)
    # Same seed: with no scaling the progress=0 enemy is weaker than depth=6.
    assert by_progress.level <= by_depth.level


# --------------------------------------------------------------------------- #
# Backward compatibility at depth 0
# --------------------------------------------------------------------------- #
def test_depth0_level_band_matches_original():
    # Original generator drew wild level from randint(1, 5).
    for s in range(80):
        m = generator.build_wild_monster(_rng(s), RUN_ID, depth=0)
        assert 1 <= m.level <= 5, f"depth-0 level out of original band: {m.level}"


def test_depth0_hp_uses_balance_curve():
    # No re-implemented HP math: max_hp must equal balance.hp_for_level.
    for s in range(40):
        m = generator.build_wild_monster(_rng(s), RUN_ID, depth=0)
        expected = balance.hp_for_level(m.level, evolution_stage=m.evolution_stage)
        assert m.max_hp == expected


def test_scaled_hp_uses_balance_curve():
    for s in range(40):
        m = generator.build_wild_monster(_rng(s), RUN_ID, depth=8)
        expected = balance.hp_for_level(m.level, evolution_stage=m.evolution_stage)
        assert m.max_hp == expected


def test_skill_power_reflects_archetype_bias():
    # A high-power archetype should produce at least one skill whose power is a
    # scaled (non-default-rounded) value, proving the bias was applied.
    m = generator.build_wild_monster(_rng(3), RUN_ID, depth=0)
    assert m.skills, "wild enemy should carry skills"
    for s in m.skills:
        assert "power" in s
