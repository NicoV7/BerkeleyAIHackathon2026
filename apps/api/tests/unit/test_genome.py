"""Unit tests for app.training.genome (WS-F mutation + persistence DNA).

T1 backend unit coverage:
  * Pure dict transforms — read_genome / system_prompt / mutate / sample_mutations
    run with NO database and NO network (the operators are deterministic given an
    injected `random.Random`).
  * apply_genome's version-bump + artifact-write logic is exercised against an
    in-memory fake session / fake monster. The only DB-shaped dependency is the
    `TrainingArtifact` SQLModel class, which constructs fine off a live engine; if
    importing `app.db.models` fails while the implementation fleet is mid-edit, the
    DB-touching tests skip (so host collection always stays green).

Style: Arrange-Act-Assert with descriptive names.
"""
from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest

from app.training import genome as G


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _seeded_rng(seed: int = 1234) -> random.Random:
    """A deterministic RNG so operator outcomes are reproducible per test."""
    return random.Random(seed)


def _base_genome() -> dict[str, Any]:
    """A small but representative genome dict (the shape mutate() expects)."""
    return {
        "harness": {"system_prompt": "You are a debater.", "directives": []},
        "persona": {"name": "Aristotle", "tone": "calm and methodical", "type": "LOGOS"},
        "skill_prompt_fragments": [],
        "gambit_rules": [],
        "skills": [],
    }


# --------------------------------------------------------------------------- #
# read_genome
# --------------------------------------------------------------------------- #


def test_read_genome_extracts_fragments_and_gambits_out_of_harness():
    # Arrange: a monster-like object whose harness embeds fragments + gambits.
    monster = SimpleNamespace(
        harness={
            "system_prompt": "Argue well.",
            "skill_prompt_fragments": ["frag-a", "frag-b"],
            "gambit_rules": [{"priority": 0, "action": "x"}],
            "temperature": 0.7,
        },
        persona={"name": "Plato", "tone": "coldly precise"},
        skills=["catalog-skill"],
    )

    # Act
    g = G.read_genome(monster)

    # Assert: fragments/gambits are hoisted to top level and stripped from harness.
    assert g["skill_prompt_fragments"] == ["frag-a", "frag-b"]
    assert g["gambit_rules"] == [{"priority": 0, "action": "x"}]
    assert "skill_prompt_fragments" not in g["harness"]
    assert "gambit_rules" not in g["harness"]
    assert g["harness"]["temperature"] == 0.7
    assert g["persona"]["name"] == "Plato"
    assert g["skills"] == ["catalog-skill"]


def test_read_genome_returns_deep_copy_isolated_from_source():
    # Arrange
    monster = SimpleNamespace(
        harness={"skill_prompt_fragments": ["frag"], "gambit_rules": []},
        persona={"name": "X"},
        skills=[],
    )

    # Act: read, then mutate the returned structure.
    g = G.read_genome(monster)
    g["skill_prompt_fragments"].append("injected")
    g["persona"]["name"] = "Y"

    # Assert: the source monster is untouched (deep copy).
    assert monster.harness["skill_prompt_fragments"] == ["frag"]
    assert monster.persona["name"] == "X"


def test_read_genome_handles_dict_monster_and_missing_fields():
    # Arrange: a plain dict with no harness/persona/skills at all.
    monster = {}

    # Act
    g = G.read_genome(monster)

    # Assert: defaults to empty containers, never raises.
    assert g["harness"] == {}
    assert g["persona"] == {}
    assert g["skill_prompt_fragments"] == []
    assert g["gambit_rules"] == []
    assert g["skills"] == []


# --------------------------------------------------------------------------- #
# system_prompt
# --------------------------------------------------------------------------- #


def test_system_prompt_assembles_persona_tone_directives_and_fragments():
    # Arrange
    g = {
        "persona": {"name": "Cicero", "tone": "theatrical and bold", "backstory": "Rome's finest."},
        "harness": {
            "system_prompt": "You are Cicero, orator.",
            "directives": ["Win the framing first."],
        },
        "skill_prompt_fragments": ["Use a vivid analogy."],
    }

    # Act
    prompt = G.system_prompt(g)

    # Assert: every section is present and ordered base -> tone -> backstory ->
    # directives -> techniques.
    assert prompt.startswith("You are Cicero, orator.")
    assert "Your debating tone is theatrical and bold." in prompt
    assert "Rome's finest." in prompt
    assert "Win the framing first." in prompt
    assert "Techniques you favor:" in prompt
    assert "- Use a vivid analogy." in prompt
    # ordering sanity
    assert prompt.index("tone is") < prompt.index("Win the framing")
    assert prompt.index("Win the framing") < prompt.index("Techniques you favor:")


def test_system_prompt_uses_defaults_when_genome_is_empty():
    # Arrange: an empty genome must still yield a usable prompt.
    g: dict[str, Any] = {}

    # Act
    prompt = G.system_prompt(g)

    # Assert: falls back to default name/type/tone, omits the techniques header.
    assert "a debater" in prompt
    assert "LOGOS" in prompt
    assert "incisive and confident" in prompt
    assert "Techniques you favor:" not in prompt


def test_system_prompt_omits_techniques_header_without_fragments():
    # Arrange
    g = {"persona": {"name": "Q", "tone": "warm but firm"}, "harness": {}}

    # Act
    prompt = G.system_prompt(g)

    # Assert
    assert "Techniques you favor:" not in prompt
    assert "Your debating tone is warm but firm." in prompt


# --------------------------------------------------------------------------- #
# mutate — per-operator behavior
# --------------------------------------------------------------------------- #


def test_mutate_does_not_mutate_the_input_genome():
    # Arrange
    original = _base_genome()
    snapshot = G.copy.deepcopy(original)

    # Act
    new_g, op = G.mutate(original, "add_skill_fragment", _seeded_rng())

    # Assert: input untouched, a new object returned, op echoed back.
    assert original == snapshot
    assert new_g is not original
    assert op == "add_skill_fragment"


def test_mutate_tweak_system_prompt_appends_directive():
    # Arrange
    g = _base_genome()

    # Act
    new_g, op = G.mutate(g, "tweak_system_prompt", _seeded_rng())

    # Assert: a known directive is added to harness.directives.
    assert op == "tweak_system_prompt"
    dirs = new_g["harness"]["directives"]
    assert len(dirs) == 1
    assert dirs[0] in G._DIRECTIVES


def test_mutate_tweak_system_prompt_falls_back_to_base_when_directive_exists():
    # Arrange: pre-seed directives with EVERY directive so the chosen one is a dup,
    # forcing the else-branch that rewrites the base system_prompt.
    g = _base_genome()
    g["harness"]["directives"] = list(G._DIRECTIVES)
    g["harness"]["system_prompt"] = "Base."

    # Act
    new_g, _ = G.mutate(g, "tweak_system_prompt", _seeded_rng())

    # Assert: no new directive added; base prompt extended with the directive text.
    assert new_g["harness"]["directives"] == list(G._DIRECTIVES)
    assert new_g["harness"]["system_prompt"].startswith("Base.")
    assert len(new_g["harness"]["system_prompt"]) > len("Base.")


def test_mutate_shift_tone_changes_to_a_different_known_tone():
    # Arrange
    g = _base_genome()
    before = g["persona"]["tone"]

    # Act
    new_g, op = G.mutate(g, "shift_tone", _seeded_rng())

    # Assert
    assert op == "shift_tone"
    assert new_g["persona"]["tone"] in G._TONES
    assert new_g["persona"]["tone"] != before


def test_mutate_add_skill_fragment_appends_known_fragment():
    # Arrange
    g = _base_genome()

    # Act
    new_g, _ = G.mutate(g, "add_skill_fragment", _seeded_rng())

    # Assert
    assert len(new_g["skill_prompt_fragments"]) == 1
    assert new_g["skill_prompt_fragments"][0] in G._FRAGMENTS


def test_mutate_add_skill_fragment_is_noop_when_all_fragments_present():
    # Arrange: every fragment already present -> nothing left to add.
    g = _base_genome()
    g["skill_prompt_fragments"] = list(G._FRAGMENTS)

    # Act
    new_g, _ = G.mutate(g, "add_skill_fragment", _seeded_rng())

    # Assert: still all fragments, none duplicated.
    assert sorted(new_g["skill_prompt_fragments"]) == sorted(G._FRAGMENTS)


def test_mutate_sharpen_skill_fragment_rewrites_existing_fragment():
    # Arrange
    g = _base_genome()
    g["skill_prompt_fragments"] = ["Anchor every claim."]

    # Act
    new_g, _ = G.mutate(g, "sharpen_skill_fragment", _seeded_rng())

    # Assert: the fragment is sharpened with the press-advantage suffix.
    assert new_g["skill_prompt_fragments"][0].endswith("press the advantage immediately.")


def test_mutate_sharpen_skill_fragment_seeds_one_when_empty():
    # Arrange: empty fragments -> sharpen falls back to adding one.
    g = _base_genome()

    # Act
    new_g, _ = G.mutate(g, "sharpen_skill_fragment", _seeded_rng())

    # Assert
    assert len(new_g["skill_prompt_fragments"]) == 1
    assert new_g["skill_prompt_fragments"][0] in G._FRAGMENTS


def test_mutate_reprioritize_gambits_renumbers_priorities_zero_indexed():
    # Arrange: 3 gambit rules with arbitrary priorities.
    g = _base_genome()
    g["gambit_rules"] = [
        {"priority": 9, "action": "a"},
        {"priority": 5, "action": "b"},
        {"priority": 2, "action": "c"},
    ]

    # Act
    new_g, op = G.mutate(g, "reprioritize_gambits", _seeded_rng())

    # Assert: priorities become a contiguous 0..n-1 set (order shuffled).
    assert op == "reprioritize_gambits"
    priorities = sorted(r["priority"] for r in new_g["gambit_rules"])
    assert priorities == [0, 1, 2]
    actions = sorted(r["action"] for r in new_g["gambit_rules"])
    assert actions == ["a", "b", "c"]


def test_mutate_reprioritize_gambits_is_valid_noop_with_single_rule():
    # Arrange: fewer than 2 rules -> documented no-op variant.
    g = _base_genome()
    g["gambit_rules"] = [{"priority": 7, "action": "solo"}]

    # Act
    new_g, _ = G.mutate(g, "reprioritize_gambits", _seeded_rng())

    # Assert: unchanged, no crash.
    assert new_g["gambit_rules"] == [{"priority": 7, "action": "solo"}]


def test_mutate_tighten_persona_sets_a_known_focus():
    # Arrange
    g = _base_genome()

    # Act
    new_g, op = G.mutate(g, "tighten_persona", _seeded_rng())

    # Assert
    assert op == "tighten_persona"
    assert new_g["persona"]["focus"] in {
        "aggression",
        "precision",
        "framing",
        "empathy",
        "credibility",
    }


def test_mutate_rejects_unknown_operator():
    # Arrange
    g = _base_genome()

    # Act / Assert
    with pytest.raises(ValueError, match="Unknown mutation op"):
        G.mutate(g, "not_a_real_op", _seeded_rng())


def test_mutate_without_op_picks_from_operators_and_is_deterministic_with_seed():
    # Arrange: same seed -> same operator chosen.
    g = _base_genome()

    # Act
    _, op1 = G.mutate(g, None, random.Random(7))
    _, op2 = G.mutate(g, None, random.Random(7))

    # Assert
    assert op1 == op2
    assert op1 in G.OPERATORS


# --------------------------------------------------------------------------- #
# sample_mutations
# --------------------------------------------------------------------------- #


def test_sample_mutations_returns_exactly_k_variants():
    # Arrange
    g = _base_genome()

    # Act
    variants = G.sample_mutations(g, 5, _seeded_rng())

    # Assert: k pairs of (genome_dict, op), every op valid.
    assert len(variants) == 5
    for variant_genome, op in variants:
        assert isinstance(variant_genome, dict)
        assert op in G.OPERATORS


def test_sample_mutations_does_not_mutate_source_genome():
    # Arrange
    g = _base_genome()
    snapshot = G.copy.deepcopy(g)

    # Act
    G.sample_mutations(g, 4, _seeded_rng())

    # Assert
    assert g == snapshot


def test_sample_mutations_respects_weight_bias():
    # Arrange: pin all weight on one operator -> every drawn op should be it.
    g = _base_genome()
    weights = {"shift_tone": 1000.0}

    # Act
    variants = G.sample_mutations(g, 6, _seeded_rng(), weights=weights)

    # Assert
    assert all(op == "shift_tone" for _, op in variants)


def test_sample_mutations_zero_k_returns_empty():
    # Arrange
    g = _base_genome()

    # Act
    variants = G.sample_mutations(g, 0, _seeded_rng())

    # Assert
    assert variants == []


# --------------------------------------------------------------------------- #
# apply_genome — version bump + artifact write
#
# Uses an in-memory fake session/monster. The only real dependency is the
# TrainingArtifact SQLModel; if app.db.models can't be imported (impl fleet
# mid-edit), these skip so host collection stays green.
# --------------------------------------------------------------------------- #


def _models_importable() -> bool:
    try:
        import app.db.models  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_DB_MODELS = pytest.mark.skipif(
    not _models_importable(),
    reason="app.db.models not importable on host (impl fleet mid-edit); "
    "skipping apply_genome persistence tests.",
)


class _FakeSession:
    """Records .add() calls without any real DB/commit."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _fake_monster(**overrides: Any) -> SimpleNamespace:
    """A monster-shaped object good enough for read_genome + apply_genome."""
    base: dict[str, Any] = {
        "id": "monster-123",
        "harness": {"system_prompt": "old", "skill_prompt_fragments": [], "gambit_rules": []},
        "persona": {"name": "Old", "tone": "calm and methodical"},
        "skills": [],
        "genome_version": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@_DB_MODELS
def test_apply_genome_accepted_bumps_version_and_writes_back_harness():
    # Arrange
    session = _FakeSession()
    monster = _fake_monster(genome_version=3)
    new_genome = {
        "harness": {"system_prompt": "new directive set"},
        "persona": {"name": "New", "tone": "aggressive and relentless"},
        "skill_prompt_fragments": ["frag-x"],
        "gambit_rules": [{"priority": 0, "action": "press"}],
        "skills": [],
    }

    # Act
    artifact = G.apply_genome(
        session, monster, new_genome, kind="gepa", score_delta=2.5, accepted=True
    )

    # Assert: version bumped, fragments/gambits folded back into harness, persona set.
    assert monster.genome_version == 4
    assert monster.harness["system_prompt"] == "new directive set"
    assert monster.harness["skill_prompt_fragments"] == ["frag-x"]
    assert monster.harness["gambit_rules"] == [{"priority": 0, "action": "press"}]
    assert monster.persona == {"name": "New", "tone": "aggressive and relentless"}
    # artifact recorded the before/after + metadata
    assert artifact.monster_id == "monster-123"
    assert artifact.kind == "gepa"
    assert artifact.score_delta == 2.5
    assert artifact.accepted is True
    assert artifact.genome_after == new_genome
    # both monster (re-add) and artifact were added to the session
    assert artifact in session.added
    assert monster in session.added


@_DB_MODELS
def test_apply_genome_rejected_does_not_mutate_monster_but_still_records_artifact():
    # Arrange
    session = _FakeSession()
    monster = _fake_monster(genome_version=5)
    original_harness = dict(monster.harness)
    new_genome = {
        "harness": {"system_prompt": "rejected change"},
        "persona": {"name": "Nope"},
        "skill_prompt_fragments": ["frag-y"],
        "gambit_rules": [],
        "skills": [],
    }

    # Act
    artifact = G.apply_genome(session, monster, new_genome, accepted=False, score_delta=-1.0)

    # Assert: monster untouched, version NOT bumped, artifact still written + flagged.
    assert monster.genome_version == 5
    assert monster.harness == original_harness
    assert monster.persona == {"name": "Old", "tone": "calm and methodical"}
    assert artifact.accepted is False
    assert artifact.score_delta == -1.0
    assert artifact.genome_after == new_genome
    assert artifact in session.added
    assert monster not in session.added  # rejected -> monster not re-added


@_DB_MODELS
def test_apply_genome_uses_provided_before_snapshot():
    # Arrange: pass an explicit `before` snapshot distinct from current state.
    session = _FakeSession()
    monster = _fake_monster()
    before_snapshot = {"harness": {"system_prompt": "explicit-before"}, "persona": {}}
    new_genome = {"harness": {}, "persona": {}, "skill_prompt_fragments": [], "gambit_rules": []}

    # Act
    artifact = G.apply_genome(session, monster, new_genome, before=before_snapshot)

    # Assert: the artifact records the explicit before, not a re-read of the monster.
    assert artifact.genome_before == before_snapshot


@_DB_MODELS
def test_apply_genome_defaults_version_when_unset_then_bumps_to_two():
    # Arrange: genome_version is None -> treated as 1, bumped to 2.
    session = _FakeSession()
    monster = _fake_monster(genome_version=None)
    new_genome = {"harness": {}, "persona": {}, "skill_prompt_fragments": [], "gambit_rules": []}

    # Act
    G.apply_genome(session, monster, new_genome, accepted=True)

    # Assert
    assert monster.genome_version == 2
