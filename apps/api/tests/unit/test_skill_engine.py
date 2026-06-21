"""Unit tests for the SKILL.MD system + type-as-domain registry.

Covers three concerns with NO DB / Redis / network:
  * ``skill_engine.skill_instructions`` — slug match, by-type fallback, "" for
    garbage, and the headline guarantee that it NEVER raises.
  * ``archetypes`` type->domain->skills registry — covers all six DebateTypes.
  * ``generator`` — a generated monster of a given TYPE gets that domain's moves
    (a PATHOS monster gets pathos-domain skills), and existing behaviour holds.
"""
from __future__ import annotations

import random

import pytest

from app.db.models import DebateType
from app.debate.skill_engine import (
    reload_skills,
    skill_instructions,
    slugify,
)
from app.party import archetypes
from app.party import generator


# --------------------------------------------------------------------------- #
# Interface import contract (orchestrator imports this exact symbol)
# --------------------------------------------------------------------------- #
def test_public_import_contract():
    from app.debate.skill_engine import skill_instructions as si  # noqa: F401

    assert callable(si)


# --------------------------------------------------------------------------- #
# slugify
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Credential Drop", "credential_drop"),
        ("Steel Man", "steel_man"),
        ("Emotional Appeal", "emotional_appeal"),
        ("Leading Question", "leading_question"),
        ("  Analogy   Strike  ", "analogy_strike"),
        ("", ""),
        (None, ""),
    ],
)
def test_slugify(name, expected):
    assert slugify(name) == expected


# --------------------------------------------------------------------------- #
# skill_instructions: known move -> non-empty body
# --------------------------------------------------------------------------- #
def test_known_move_returns_nonempty_text():
    text = skill_instructions("Credential Drop")
    assert text and isinstance(text, str)
    assert len(text) > 50
    # Front-matter must be stripped — no leading YAML fence.
    assert not text.lstrip().startswith("---")
    # Body should mention the move (case-insensitive).
    assert "credential" in text.lower()


@pytest.mark.parametrize(
    "move",
    [
        "Logical Thrust", "Steel Man", "Emotional Appeal", "Anecdote",
        "Authority Cite", "Credential Drop", "Reframe Attack", "Whataboutism",
        "Socratic Probe", "Leading Question", "Rhetorical Flourish", "Analogy Strike",
    ],
)
def test_every_catalog_move_has_an_md(move):
    assert skill_instructions(move).strip(), f"{move} should resolve to a .md body"


def test_skill_name_is_case_insensitive():
    a = skill_instructions("credential drop")
    b = skill_instructions("Credential Drop")
    assert a and a == b


# --------------------------------------------------------------------------- #
# skill_instructions: fallbacks + never-raise
# --------------------------------------------------------------------------- #
def test_garbage_skill_returns_empty():
    assert skill_instructions("Totally Not A Real Move") == ""
    assert skill_instructions("!!!###") == ""


def test_garbage_skill_with_type_falls_back_to_domain():
    text = skill_instructions("Totally Not A Real Move", "PATHOS")
    assert text
    assert "PATHOS" in text
    # Falls back to the domain's signature moves.
    assert "Emotional Appeal" in text or "Anecdote" in text


def test_none_name_with_type_uses_domain():
    text = skill_instructions(None, "LOGOS")
    assert text and "LOGOS" in text


def test_none_everything_returns_empty():
    assert skill_instructions(None, None) == ""
    assert skill_instructions(None) == ""


def test_unknown_type_returns_empty():
    assert skill_instructions(None, "NONSENSE") == ""


def test_accepts_debate_type_enum_for_fallback():
    text = skill_instructions("bogus", DebateType.ethos)
    assert text and "ETHOS" in text


def test_never_raises_on_weird_input():
    for bad in [123, [], {}, object(), 0, ""]:
        # Should swallow everything and return a string.
        out = skill_instructions(bad)  # type: ignore[arg-type]
        assert isinstance(out, str)


def test_reload_is_idempotent():
    first = skill_instructions("Steel Man")
    reload_skills()
    second = skill_instructions("Steel Man")
    assert first == second and first


# --------------------------------------------------------------------------- #
# Type -> domain -> skills registry (covers ALL six DebateTypes)
# --------------------------------------------------------------------------- #
def test_registry_covers_all_debate_types():
    for dt in DebateType:
        assert dt.value in archetypes.TYPE_DOMAINS, f"{dt.value} missing from registry"


def test_every_domain_has_description_and_skills():
    for value, domain in archetypes.TYPE_DOMAINS.items():
        assert domain["description"].strip(), f"{value} needs a description"
        sigs = domain["signature_skills"]
        assert sigs, f"{value} needs signature skills"
        # Every signature move must resolve to a real .md instruction.
        for name in sigs:
            assert skill_instructions(name).strip(), f"{name} ({value}) has no .md"


def test_domain_for_type_accepts_enum_and_string():
    by_enum = archetypes.domain_for_type(DebateType.pathos)
    by_str = archetypes.domain_for_type("pathos")
    assert by_enum == by_str
    assert "Emotional Appeal" in by_enum["signature_skills"]


def test_domain_for_unknown_type_is_safe():
    d = archetypes.domain_for_type("WAT")
    assert d == {"description": "", "signature_skills": []}
    assert archetypes.signature_skills_for_type(None) == []


def test_signature_skills_partition_cleanly():
    # No move belongs to two different domains (clean type partition).
    seen: dict[str, str] = {}
    for value, domain in archetypes.TYPE_DOMAINS.items():
        for name in domain["signature_skills"]:
            assert name not in seen, f"{name} claimed by {seen.get(name)} and {value}"
            seen[name] = value


# --------------------------------------------------------------------------- #
# Generator assigns TYPE-appropriate (domain) skills
# --------------------------------------------------------------------------- #
def _domain_skill_names(dtype: DebateType) -> set[str]:
    return set(archetypes.signature_skills_for_type(dtype))


def test_generated_monster_first_skill_is_from_its_domain():
    # build_wild_monster picks the archetype's primary type; the FIRST skill is
    # always drawn from that type's own domain.
    for s in range(40):
        m = generator.build_wild_monster(random.Random(s), "run-x", depth=0)
        assert m.skills, "wild monster must carry skills"
        domain_names = _domain_skill_names(m.type)
        first = m.skills[0]
        assert first["name"] in domain_names, (
            f"first skill {first['name']} not in {m.type.value} domain {domain_names}"
        )


def test_pathos_monster_gets_pathos_domain_skills():
    pathos_names = _domain_skill_names(DebateType.pathos)
    assert pathos_names == {"Emotional Appeal", "Anecdote"}
    # Drive _pick_skills directly with a PATHOS type across many rng states.
    found_pathos = False
    for s in range(60):
        skills = generator._pick_skills(random.Random(s), DebateType.pathos, n=2)
        # The lead skill is always a pathos-domain move.
        assert skills[0]["name"] in pathos_names
        if any(sk["name"] in pathos_names for sk in skills):
            found_pathos = True
    assert found_pathos


@pytest.mark.parametrize("dt", list(DebateType))
def test_pick_skills_lead_move_is_in_domain_for_all_types(dt):
    for s in range(20):
        skills = generator._pick_skills(random.Random(s), dt, n=2)
        assert skills, f"{dt.value} produced no skills"
        assert skills[0]["name"] in _domain_skill_names(dt)
        # Shape stays backward compatible (name/type/power/description present).
        for sk in skills:
            assert {"name", "type", "power"} <= set(sk)


def test_pick_skills_returns_requested_count():
    skills = generator._pick_skills(random.Random(1), DebateType.logos, n=2)
    assert len(skills) == 2
    assert skills[0] != skills[1]
