"""Unit tests for ``app.debate.gambits.choose_action`` (T1 backend unit).

Pure-logic tests — NO database, NO network, NO live stack.

``choose_action(monster, battle_state)`` reads the monster's ordered gambit
rules from the ``monster._gambits`` attribute (per the STABLE contract in the
plan + module docstring). Each rule is a duck-typed object exposing:
  * ``priority`` : int    — rules evaluated in ASCENDING priority order
  * ``enabled``  : bool   — disabled rules are skipped
  * ``condition``: dict   — {kind, op, value}; empty dict == unconditional
  * ``action``   : dict   — {kind, ...}; returned verbatim on first match

When no rule matches, or the gambit list is empty/absent, the engine returns
``{"kind": "default"}``.

To stay DB-free we use a tiny ``_FakeMonster`` / ``_FakeRule`` instead of the
SQLAlchemy ORM. We deliberately do NOT import any DB models so collection and
execution are safe on a bare host while the implementation fleet edits source.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.debate.gambits import choose_action


# --------------------------------------------------------------------------- #
# Test doubles (duck-typed; no ORM, no DB)
# --------------------------------------------------------------------------- #


class _FakeRule:
    """Minimal stand-in for a GambitRuleModel row."""

    def __init__(
        self,
        condition: dict[str, Any],
        action: dict[str, Any],
        priority: int = 0,
        enabled: bool = True,
    ) -> None:
        self.condition = condition
        self.action = action
        self.priority = priority
        self.enabled = enabled


class _FakeMonster:
    """Minimal stand-in for a Monster ORM instance.

    ``choose_action`` only reads ``monster._gambits`` and (when ``self_id`` is
    missing from battle_state) ``monster.id``.
    """

    def __init__(self, gambits: list[_FakeRule] | None, monster_id: str = "m-self") -> None:
        self._gambits = gambits
        self.id = monster_id


def _battle_state(**overrides: Any) -> dict[str, Any]:
    """Build a battle_state dict per the STABLE contract.

    Defaults describe a healthy, neutral situation so that individual tests can
    flip exactly one signal and assert on the resulting choice.
    """
    state: dict[str, Any] = {
        "hp": {"m-self": 100, "ally-1": 100, "enemy-1": 100},
        "max_hp": {"m-self": 100, "ally-1": 100, "enemy-1": 100},
        "last_verdict_score": 0.0,
        "turn_no": 0,
        "topic": "Should pineapple go on pizza?",
        "momentum": {"party": 0.0, "enemy": 0.0},
        "self_id": "m-self",
        "ally_ids": ["ally-1"],
        "enemy_ids": ["enemy-1"],
    }
    state.update(overrides)
    return state


# --------------------------------------------------------------------------- #
# Priority ordering + first-match-wins
# --------------------------------------------------------------------------- #


def test_rules_evaluated_in_ascending_priority_order():
    # Arrange: two rules whose conditions BOTH match; lower priority must win.
    low_priority_winner = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "<=", "value": 100},
        action={"kind": "use_skill", "skill_id": "winner"},
        priority=1,
    )
    high_priority_loser = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "<=", "value": 100},
        action={"kind": "use_skill", "skill_id": "loser"},
        priority=2,
    )
    # Insert out of order to prove the engine sorts rather than trusts list order.
    monster = _FakeMonster([high_priority_loser, low_priority_winner])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert: the ascending-priority (priority=1) rule's action is returned.
    assert action == {"kind": "use_skill", "skill_id": "winner"}


def test_first_matching_rule_wins_even_when_later_rule_also_matches():
    # Arrange: first rule matches only when HP is low; second always matches.
    panic_rule = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "<", "value": 30},
        action={"kind": "tone", "value": "defensive"},
        priority=1,
    )
    fallback_rule = _FakeRule(
        condition={},  # unconditional → always true
        action={"kind": "tone", "value": "aggressive"},
        priority=2,
    )
    monster = _FakeMonster([panic_rule, fallback_rule])

    # Act: self HP is low (20%), so the panic rule should fire first.
    low_hp = _battle_state(hp={"m-self": 20, "ally-1": 100, "enemy-1": 100})
    action = choose_action(monster, low_hp)

    # Assert
    assert action == {"kind": "tone", "value": "defensive"}


def test_first_non_matching_rule_falls_through_to_next_matching_rule():
    # Arrange: high-priority rule does NOT match; lower-priority rule does.
    never_matches = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "<", "value": 10},
        action={"kind": "use_skill", "skill_id": "panic"},
        priority=1,
    )
    always_matches = _FakeRule(
        condition={},
        action={"kind": "use_skill", "skill_id": "steady"},
        priority=2,
    )
    monster = _FakeMonster([never_matches, always_matches])

    # Act: full HP → first rule's condition fails, fall through to second.
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "use_skill", "skill_id": "steady"}


# --------------------------------------------------------------------------- #
# Disabled rules are skipped
# --------------------------------------------------------------------------- #


def test_disabled_rule_is_skipped_even_if_it_would_match_first():
    # Arrange: a disabled high-priority rule that would otherwise win.
    disabled_winner = _FakeRule(
        condition={},
        action={"kind": "use_skill", "skill_id": "disabled"},
        priority=1,
        enabled=False,
    )
    enabled_fallback = _FakeRule(
        condition={},
        action={"kind": "use_skill", "skill_id": "enabled"},
        priority=2,
        enabled=True,
    )
    monster = _FakeMonster([disabled_winner, enabled_fallback])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert: the disabled rule is ignored; the enabled fallback fires.
    assert action == {"kind": "use_skill", "skill_id": "enabled"}


def test_all_rules_disabled_returns_default():
    # Arrange
    rules = [
        _FakeRule(condition={}, action={"kind": "tone", "value": "x"}, enabled=False),
        _FakeRule(condition={}, action={"kind": "tone", "value": "y"}, enabled=False),
    ]
    monster = _FakeMonster(rules)

    # Act
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "default"}


# --------------------------------------------------------------------------- #
# Empty / absent gambit list → default
# --------------------------------------------------------------------------- #


def test_empty_gambit_list_returns_default():
    # Arrange
    monster = _FakeMonster([])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "default"}


def test_none_gambit_list_returns_default():
    # Arrange: ``_gambits`` is None (e.g. never populated).
    monster = _FakeMonster(None)

    # Act
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "default"}


def test_monster_without_gambits_attribute_returns_default():
    # Arrange: an object lacking ``_gambits`` entirely (older model version).
    class _Bare:
        id = "bare"

    # Act
    action = choose_action(_Bare(), _battle_state())

    # Assert
    assert action == {"kind": "default"}


def test_no_rule_matches_returns_default():
    # Arrange: a single rule whose condition can never be satisfied at full HP.
    rule = _FakeRule(
        condition={"kind": "enemy_hp_pct", "op": "<", "value": 1},
        action={"kind": "use_skill", "skill_id": "finisher"},
    )
    monster = _FakeMonster([rule])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "default"}


# --------------------------------------------------------------------------- #
# Condition-kind coverage (one representative match per kind)
# --------------------------------------------------------------------------- #


def test_self_hp_pct_condition_matches_when_below_threshold():
    # Arrange
    rule = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "<", "value": 50},
        action={"kind": "tone", "value": "defensive"},
    )
    monster = _FakeMonster([rule])
    state = _battle_state(hp={"m-self": 40, "ally-1": 100, "enemy-1": 100})

    # Act
    action = choose_action(monster, state)

    # Assert: 40/100 = 40% < 50 → match.
    assert action == {"kind": "tone", "value": "defensive"}


def test_ally_hp_pct_uses_lowest_ally():
    # Arrange: two allies; the lowest (30%) must drive the comparison.
    rule = _FakeRule(
        condition={"kind": "ally_hp_pct", "op": "<", "value": 50},
        action={"kind": "use_skill", "skill_id": "rally"},
    )
    monster = _FakeMonster([rule])
    state = _battle_state(
        hp={"m-self": 100, "ally-1": 30, "ally-2": 90, "enemy-1": 100},
        max_hp={"m-self": 100, "ally-1": 100, "ally-2": 100, "enemy-1": 100},
        ally_ids=["ally-1", "ally-2"],
    )

    # Act
    action = choose_action(monster, state)

    # Assert
    assert action == {"kind": "use_skill", "skill_id": "rally"}


def test_enemy_hp_pct_condition_matches_for_finisher():
    # Arrange
    rule = _FakeRule(
        condition={"kind": "enemy_hp_pct", "op": "<=", "value": 25},
        action={"kind": "use_skill", "skill_id": "finisher"},
    )
    monster = _FakeMonster([rule])
    state = _battle_state(
        hp={"m-self": 100, "ally-1": 100, "enemy-1": 20},
    )

    # Act
    action = choose_action(monster, state)

    # Assert: enemy at 20% <= 25 → finisher.
    assert action == {"kind": "use_skill", "skill_id": "finisher"}


def test_last_verdict_score_condition_matches_when_losing():
    # Arrange: negative verdict score means the enemy is doing well.
    rule = _FakeRule(
        condition={"kind": "last_verdict_score", "op": "<", "value": 0},
        action={"kind": "tone", "value": "aggressive"},
    )
    monster = _FakeMonster([rule])
    state = _battle_state(last_verdict_score=-3.5)

    # Act
    action = choose_action(monster, state)

    # Assert
    assert action == {"kind": "tone", "value": "aggressive"}


def test_turn_no_condition_matches_on_opening_turn():
    # Arrange
    rule = _FakeRule(
        condition={"kind": "turn_no", "op": "==", "value": 0},
        action={"kind": "tone", "value": "opening"},
    )
    monster = _FakeMonster([rule])

    # Act
    action = choose_action(monster, _battle_state(turn_no=0))

    # Assert
    assert action == {"kind": "tone", "value": "opening"}


def test_topic_keyword_contains_condition_matches():
    # Arrange
    rule = _FakeRule(
        condition={"kind": "topic_keyword", "op": "contains", "value": "pineapple"},
        action={"kind": "use_skill", "skill_id": "food_expert"},
    )
    monster = _FakeMonster([rule])

    # Act: default topic contains "pineapple" (case-insensitive substring).
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "use_skill", "skill_id": "food_expert"}


def test_momentum_condition_matches_when_party_leads():
    # Arrange: party momentum minus enemy momentum must exceed threshold.
    rule = _FakeRule(
        condition={"kind": "momentum", "op": ">", "value": 5},
        action={"kind": "tone", "value": "press_advantage"},
    )
    monster = _FakeMonster([rule])
    state = _battle_state(momentum={"party": 10.0, "enemy": 2.0})

    # Act: 10 - 2 = 8 > 5 → match.
    action = choose_action(monster, state)

    # Assert
    assert action == {"kind": "tone", "value": "press_advantage"}


# --------------------------------------------------------------------------- #
# self_id seeding from monster.id
# --------------------------------------------------------------------------- #


def test_self_id_is_seeded_from_monster_when_absent_from_state():
    # Arrange: state omits self_id; engine must fall back to monster.id ("hero").
    rule = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "<", "value": 50},
        action={"kind": "tone", "value": "defensive"},
    )
    monster = _FakeMonster([rule], monster_id="hero")
    state = _battle_state(
        hp={"hero": 10, "ally-1": 100, "enemy-1": 100},
        max_hp={"hero": 100, "ally-1": 100, "enemy-1": 100},
    )
    state.pop("self_id")

    # Act
    action = choose_action(monster, state)

    # Assert: hero at 10% < 50 → defensive, proving self_id resolved to "hero".
    assert action == {"kind": "tone", "value": "defensive"}


# --------------------------------------------------------------------------- #
# Robustness — never raises (import-safe / call-safe)
# --------------------------------------------------------------------------- #


def test_unknown_condition_kind_is_skipped_not_raised():
    # Arrange: an unknown kind should be treated as non-matching, falling
    # through to the default rather than raising.
    bad = _FakeRule(
        condition={"kind": "does_not_exist", "op": "<", "value": 1},
        action={"kind": "use_skill", "skill_id": "ghost"},
    )
    monster = _FakeMonster([bad])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "default"}


def test_unknown_operator_is_skipped_not_raised():
    # Arrange: an unrecognized operator must not blow up the engine.
    bad = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "~=", "value": 50},
        action={"kind": "use_skill", "skill_id": "ghost"},
    )
    monster = _FakeMonster([bad])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "default"}


def test_malformed_condition_value_does_not_raise():
    # Arrange: a non-numeric value for a numeric kind would raise on float(),
    # but the engine guards each rule and continues to the default.
    bad = _FakeRule(
        condition={"kind": "self_hp_pct", "op": "<", "value": "not-a-number"},
        action={"kind": "use_skill", "skill_id": "ghost"},
    )
    monster = _FakeMonster([bad])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert: error swallowed, rule skipped → default.
    assert action == {"kind": "default"}


def test_missing_hp_maps_do_not_raise_and_treat_hp_as_full():
    # Arrange: battle_state without hp/max_hp; _hp_pct defaults to 100%.
    rule = _FakeRule(
        condition={"kind": "self_hp_pct", "op": ">=", "value": 100},
        action={"kind": "tone", "value": "confident"},
    )
    monster = _FakeMonster([rule])
    bare_state: dict[str, Any] = {"self_id": "m-self"}

    # Act
    action = choose_action(monster, bare_state)

    # Assert: missing HP treated as 100% → matches >=100.
    assert action == {"kind": "tone", "value": "confident"}


def test_empty_battle_state_returns_a_dict_with_kind_and_does_not_raise():
    # Arrange: an unconditional rule with a totally empty battle_state.
    rule = _FakeRule(condition={}, action={"kind": "tone", "value": "neutral"})
    monster = _FakeMonster([rule])

    # Act
    action = choose_action(monster, {})

    # Assert: still resolves the unconditional rule; always returns a dict.
    assert isinstance(action, dict)
    assert action["kind"] == "tone"


def test_rule_with_empty_action_falls_back_to_default():
    # Arrange: a matching rule whose action is empty → engine returns default.
    rule = _FakeRule(condition={}, action={})
    monster = _FakeMonster([rule])

    # Act
    action = choose_action(monster, _battle_state())

    # Assert
    assert action == {"kind": "default"}


def test_returned_action_is_a_copy_not_the_rule_object():
    # Arrange: mutating the returned dict must not corrupt the rule's action.
    original_action = {"kind": "use_skill", "skill_id": "ember"}
    rule = _FakeRule(condition={}, action=original_action)
    monster = _FakeMonster([rule])

    # Act
    action = choose_action(monster, _battle_state())
    action["skill_id"] = "MUTATED"

    # Assert: the rule's stored action is unchanged.
    assert original_action == {"kind": "use_skill", "skill_id": "ember"}


@pytest.mark.parametrize(
    "battle_state",
    [
        {},
        {"self_id": "m-self"},
        {"hp": {}, "max_hp": {}},
        {"momentum": {}, "topic": ""},
        {"last_verdict_score": None},  # forces a guarded float(None) failure path
    ],
)
def test_choose_action_never_raises_across_varied_states(battle_state):
    # Arrange: a small rule set spanning several condition kinds.
    rules = [
        _FakeRule(
            condition={"kind": "last_verdict_score", "op": "<", "value": 0},
            action={"kind": "tone", "value": "aggressive"},
            priority=1,
        ),
        _FakeRule(
            condition={"kind": "self_hp_pct", "op": "<", "value": 30},
            action={"kind": "tone", "value": "defensive"},
            priority=2,
        ),
        _FakeRule(
            condition={},
            action={"kind": "use_skill", "skill_id": "steady"},
            priority=3,
        ),
    ]
    monster = _FakeMonster(rules)

    # Act / Assert: must always return a dict with a "kind", never raise.
    action = choose_action(monster, battle_state)
    assert isinstance(action, dict)
    assert "kind" in action


def test_choose_action_is_importable_without_side_effects():
    # Arrange / Act: re-import the module to confirm import alone never raises.
    import importlib

    import app.debate.gambits as gambits_mod

    reloaded = importlib.reload(gambits_mod)

    # Assert
    assert callable(reloaded.choose_action)
