"""Unit tests for battle skill effect mechanics.

These cover pure effect behavior without Redis, WebSockets, or LLM calls:
output-limit statuses clamp the model token budget, and defense effects reduce
incoming damage through the normal verdict/damage path.
"""
from __future__ import annotations

from app.debate import orchestrator
from app.debate.orchestrator import Combatant, _apply_round_damage, _action_max_tokens


def _combatant(monster_id: str, role: str, dtype: str) -> Combatant:
    return Combatant(
        monster_id=monster_id,
        name=monster_id,
        type=dtype,
        role=role,
        hp=100,
        max_hp=100,
        atk=10,
        def_=10,
    )


def test_status_token_budget_reduces_generation_budget(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(orchestrator, "_actor_max_tokens", lambda: 120)

    # Act
    reduced = _action_max_tokens({"max_tokens": 60})
    tiny = _action_max_tokens({"max_tokens": 10})
    excessive = _action_max_tokens({"max_tokens": 999})

    # Assert
    assert reduced == 60
    assert tiny == 32
    assert excessive == 120


def test_defense_effect_reduces_incoming_damage(monkeypatch) -> None:
    # Arrange
    monkeypatch.setattr(orchestrator, "_battle_damage_multiplier", lambda: 1.0)
    party = _combatant("party", "party", "LOGOS")
    enemy = _combatant("enemy", "enemy", "PATHOS")

    # Act
    verdicts = _apply_round_damage(
        [party, enemy],
        [
            (party, 55.0, "ok", {}, {"attack_type": "LOGOS", "skill_mult": 1.0}),
            (
                enemy,
                80.0,
                "hit",
                {},
                {
                    "attack_type": "PATHOS",
                    "skill_mult": 1.0,
                    "target_defense_mult": 0.5,
                },
            ),
        ],
        {"party": 1.0, "enemy": 1.0},
        topic="Money can buy happiness.",
    )

    # Assert
    enemy_verdict = next(v for v in verdicts if v["actor_id"] == "enemy")
    assert enemy_verdict["damage"] > 0
    assert party.hp == 100 - enemy_verdict["damage"]
    assert enemy_verdict["damage"] < 30
