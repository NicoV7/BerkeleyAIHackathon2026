"""Unit tests for persona/harness loading into battle prompts."""
from __future__ import annotations

import random

from app.config import settings
from app.debate import orchestrator as orch
from app.debate.orchestrator import Combatant
from app.party import generator
from app.party.persona import (
    BATTLE_REACTION_STATES,
    ENEMY_DIRECTIVES,
    ENEMY_SKILL_FRAGMENTS,
    compress_battle_utterance,
    ensure_battle_sentence_floor,
    ensure_battle_reactions,
    harness_prompt_line,
    normalize_harness,
    normalize_persona,
    sanitize_battle_utterance,
)
from app.routers.encounter import _fabricate_enemy


def test_normalize_persona_preserves_hydrated_and_legacy_fields() -> None:
    # Arrange
    raw = {
        "tagline": "Questions everything.",
        "views": "Evidence beats vibes.",
        "quotes": ["Know thyself."],
        "domain_keywords": ("logic", "ethics"),
    }

    # Act
    persona = normalize_persona(raw, fallback_name="Socrates")

    # Assert
    assert persona["name"] == "Socrates"
    assert persona["voice"] == "Questions everything."
    assert persona["views"] == ["Evidence beats vibes."]
    assert persona["quotes"] == ["Know thyself."]
    assert persona["domain_keywords"] == ["logic", "ethics"]


def test_actor_messages_include_trained_harness_before_stance_anchor() -> None:
    # Arrange
    actor = Combatant(
        monster_id="enemy-1",
        name="Wild Sophist",
        type="CHAOS",
        role="enemy",
        hp=90,
        max_hp=90,
        persona={"tagline": "No premise is safe.", "views": ["frames decide fights"]},
        harness={
            "system": "TRAINED SYSTEM: answer the latest claim fast.",
            "directives": ["Use the opponent's wording as evidence."],
        },
    )

    # Act
    messages = orch._build_actor_messages(
        actor,
        "Remote work improves focus.",
        [],
        {},
        [],
        {"enemy-1": "Wild Sophist"},
    )
    system = messages[0]["content"]

    # Assert
    assert "TRAINED SYSTEM" in system
    assert "Use the opponent's wording as evidence." in system
    assert "voice: No premise is safe." in system
    assert system.index("Your trained battle harness") < system.index("YOUR ASSIGNED SIDE")
    assert "YOUR ASSIGNED SIDE: AGAINST" in system


def test_generated_wild_enemy_uses_pareto_actor_and_enemy_harness() -> None:
    # Arrange / Act
    monster = generator.build_wild_monster(random.Random(12), "run-test", depth=0)

    # Assert
    assert monster.model == settings.actor_model
    assert set(monster.persona["battle_reactions"]) == set(BATTLE_REACTION_STATES)
    assert all(2 <= len(lines) <= 3 for lines in monster.persona["battle_reactions"].values())
    for directive in ENEMY_DIRECTIVES:
        assert directive in monster.harness["directives"]
    for fragment in ENEMY_SKILL_FRAGMENTS:
        assert fragment in monster.harness["skill_prompt_fragments"]
    assert any("rebutting the party" in f for f in monster.harness["skill_prompt_fragments"])


def test_enemy_harness_is_thin_and_skills_are_fat() -> None:
    # Arrange / Act
    harness = normalize_harness(
        {"system_prompt": "Thin enemy battle harness.", "directives": []},
        role="enemy",
    )
    rendered = harness_prompt_line(harness)

    # Assert
    assert len(harness["directives"]) <= 10
    for fragment in ENEMY_SKILL_FRAGMENTS:
        assert fragment in harness["skill_prompt_fragments"]
    assert ENEMY_SKILL_FRAGMENTS[0] in rendered
    assert ENEMY_SKILL_FRAGMENTS[0] not in harness["directives"]


def test_battle_reactions_reflect_personality_and_role() -> None:
    # Arrange
    raw = {
        "tone": "sardonic",
        "voice": "A journalist who traffics in provocative questions.",
        "quirks": "speaks in rhetorical questions",
    }

    # Act
    persona = ensure_battle_reactions(raw, "CHAOS", role="enemy", fallback_name="Quibblon")

    # Assert
    reactions = persona["battle_reactions"]
    assert set(reactions) == set(BATTLE_REACTION_STATES)
    assert all(2 <= len(lines) <= 3 for lines in reactions.values())
    joined = " ".join(line for lines in reactions.values() for line in lines)
    assert "sardonic" in joined.lower()
    assert "probing" in joined.lower()
    assert "reframe" in joined.lower()


def test_fabricated_enemy_uses_normalized_fast_enemy_harness() -> None:
    # Arrange / Act
    monster = _fabricate_enemy("run-test")
    harness = normalize_harness(monster.harness, role="enemy")

    # Assert
    assert monster.model == settings.actor_model
    assert monster.persona["voice"] == "A wandering contrarian."
    for directive in ENEMY_DIRECTIVES:
        assert directive in harness["directives"]
    for fragment in ENEMY_SKILL_FRAGMENTS:
        assert fragment in harness["skill_prompt_fragments"]


def test_compress_battle_utterance_keeps_complete_short_sentences() -> None:
    # Arrange
    sentences = [
        "Remote work improves output when teams use clear ownership.",
        (
            "This sentence is intentionally too long and should be removed because "
            "keeping it would either ramble or get chopped into an unfinished fragment."
        ),
        "Async check-ins preserve momentum.",
    ]

    # Act
    text = compress_battle_utterance(sentences, max_sentences=4)

    # Assert
    assert text == (
        "Remote work improves output when teams use clear ownership. "
        "Async check-ins preserve momentum."
    )
    assert "unfinished fragment" not in text


def test_sanitize_preserves_complete_long_first_sentence() -> None:
    # Arrange
    raw = (
        "The claim that tools cannot restore trust is incomplete because teams can pair video rituals with ownership logs. "
        "This second sentence is intentionally too long and should be dropped instead of being cut into a fragment that sounds unfinished during battle."
    )

    # Act
    text = sanitize_battle_utterance(raw)

    # Assert
    assert text.endswith("ownership logs.")
    assert "is incomplete" in text
    assert "dropped" not in text


def test_sanitize_keeps_decimal_percentages_together() -> None:
    # Arrange / Act
    text = sanitize_battle_utterance(
        "Remote work raised output by 4. 8% in the study. That gain helps teams ship more."
    )

    # Assert
    assert "4.8%" in text


def test_sentence_floor_adds_role_specific_support_sentence() -> None:
    party = ensure_battle_sentence_floor(
        "Remote teams ship more work when focus time increases.",
        role="party",
    )
    enemy = ensure_battle_sentence_floor(
        "Remote work weakens team trust.",
        role="enemy",
    )

    assert party.count(".") == 2
    assert "team throughput" in party
    assert enemy.count(".") == 2
    assert "coordination, trust" in enemy
