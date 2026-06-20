"""Unit tests for app.debate.damage — pure functions, no DB, no live stack.

Covers compute_damage() and the TYPE_CHART / type_multiplier() helper:
  * score <= 50 deals 0 damage (only above-average arguments hurt)
  * score > 50 scales linearly with the clamped base
  * type-effectiveness multiplier (super-effective / resisted / neutral)
  * level_scale clamping to [0.5, 2.0]
  * multi-defender split (round(compute_damage / n_defenders))

These are pure-function tests: collection and execution always pass on the
host, independent of Postgres or the running API.
"""
from __future__ import annotations

import pytest

from app.debate.damage import TYPE_CHART, compute_damage, type_multiplier


# --------------------------------------------------------------------------- #
# score threshold: only above-average arguments deal damage
# --------------------------------------------------------------------------- #
class TestScoreThreshold:
    def test_score_below_fifty_deals_zero_damage(self) -> None:
        # Arrange
        score = 30.0
        # Act
        dmg = compute_damage(score=score)
        # Assert
        assert dmg == 0

    def test_score_exactly_fifty_deals_zero_damage(self) -> None:
        # Arrange
        score = 50.0  # base = clamp(50-50) = 0 -> no damage
        # Act
        dmg = compute_damage(score=score)
        # Assert
        assert dmg == 0

    @pytest.mark.parametrize("score", [0.0, 1.0, 25.0, 49.9])
    def test_any_score_at_or_under_fifty_is_zero(self, score: float) -> None:
        # Arrange / Act
        dmg = compute_damage(score=score)
        # Assert
        assert dmg == 0

    def test_score_just_above_fifty_deals_positive_damage(self) -> None:
        # Arrange
        score = 51.0  # base = 1.0, all multipliers neutral
        # Act
        dmg = compute_damage(score=score)
        # Assert
        assert dmg == 1


# --------------------------------------------------------------------------- #
# score scaling above the threshold
# --------------------------------------------------------------------------- #
class TestScoreScaling:
    def test_score_scales_linearly_with_base_when_neutral(self) -> None:
        # Arrange: neutral multipliers -> damage == base == score - 50
        # Act / Assert
        assert compute_damage(score=60.0) == 10
        assert compute_damage(score=75.0) == 25
        assert compute_damage(score=100.0) == 50

    def test_base_is_clamped_to_fifty_for_scores_above_hundred(self) -> None:
        # Arrange
        # Act: a score of 200 still clamps base to 50 with neutral multipliers
        dmg = compute_damage(score=200.0)
        # Assert
        assert dmg == 50

    def test_higher_score_never_yields_less_damage(self) -> None:
        # Arrange / Act
        low = compute_damage(score=55.0)
        high = compute_damage(score=80.0)
        # Assert
        assert high > low


# --------------------------------------------------------------------------- #
# type-effectiveness multiplier
# --------------------------------------------------------------------------- #
class TestTypeMultiplier:
    def test_super_effective_pairing_returns_one_point_five(self) -> None:
        # Arrange / Act / Assert
        assert type_multiplier("LOGOS", "PATHOS") == 1.5

    def test_resisted_pairing_returns_three_quarters(self) -> None:
        # Arrange / Act / Assert
        assert type_multiplier("LOGOS", "ETHOS") == 0.75

    def test_unlisted_pairing_is_neutral(self) -> None:
        # Arrange: LOGOS has no entry vs SOCRATIC in the chart
        # Act / Assert
        assert type_multiplier("LOGOS", "SOCRATIC") == 1.0

    def test_lowercase_types_are_normalized_to_upper(self) -> None:
        # Arrange / Act / Assert
        assert type_multiplier("logos", "pathos") == 1.5

    @pytest.mark.parametrize(
        "attacker,defender",
        [(None, "PATHOS"), ("LOGOS", None), (None, None), ("", "PATHOS")],
    )
    def test_missing_type_is_neutral(self, attacker, defender) -> None:
        # Arrange / Act / Assert
        assert type_multiplier(attacker, defender) == 1.0

    def test_type_chart_rows_are_self_consistent_dicts(self) -> None:
        # Arrange / Act / Assert: every row maps strings to numeric multipliers
        for attacker, row in TYPE_CHART.items():
            assert isinstance(attacker, str)
            for defender, mult in row.items():
                assert isinstance(defender, str)
                assert isinstance(mult, (int, float))
                assert mult > 0


class TestTypeMultiplierInDamage:
    def test_super_effective_amplifies_damage(self) -> None:
        # Arrange: base 20, x1.5 super-effective
        # Act
        dmg = compute_damage(score=70.0, attacker_type="LOGOS", defender_type="PATHOS")
        # Assert: round(20 * 1.5) == 30
        assert dmg == 30

    def test_resisted_reduces_damage(self) -> None:
        # Arrange: base 20, x0.75 resisted
        # Act
        dmg = compute_damage(score=70.0, attacker_type="LOGOS", defender_type="ETHOS")
        # Assert: round(20 * 0.75) == 15
        assert dmg == 15

    def test_neutral_type_leaves_base_unchanged(self) -> None:
        # Arrange: base 20, no type advantage listed
        # Act
        dmg = compute_damage(score=70.0, attacker_type="LOGOS", defender_type="SOCRATIC")
        # Assert
        assert dmg == 20


# --------------------------------------------------------------------------- #
# level_scale clamping: 1 + (atk - def) * 0.05, clamped to [0.5, 2.0]
# --------------------------------------------------------------------------- #
class TestLevelScaleClamp:
    def test_equal_levels_leave_damage_unchanged(self) -> None:
        # Arrange: level_scale = 1.0
        # Act
        dmg = compute_damage(score=70.0, attacker_level=5, defender_level=5)
        # Assert: base 20 * 1.0
        assert dmg == 20

    def test_higher_attacker_level_increases_damage(self) -> None:
        # Arrange: +10 levels -> 1 + 10*0.05 = 1.5
        # Act
        dmg = compute_damage(score=70.0, attacker_level=11, defender_level=1)
        # Assert: round(20 * 1.5) == 30
        assert dmg == 30

    def test_level_scale_clamped_high_at_two(self) -> None:
        # Arrange: +100 levels would be 6.0 but clamps to 2.0
        # Act
        dmg = compute_damage(score=70.0, attacker_level=101, defender_level=1)
        # Assert: round(20 * 2.0) == 40
        assert dmg == 40

    def test_level_scale_clamped_low_at_half(self) -> None:
        # Arrange: -100 levels would be -4.0 but clamps to 0.5
        # Act
        dmg = compute_damage(score=70.0, attacker_level=1, defender_level=101)
        # Assert: round(20 * 0.5) == 10
        assert dmg == 10

    def test_extreme_negative_delta_never_goes_below_half(self) -> None:
        # Arrange: enormous defender level
        # Act
        dmg = compute_damage(score=70.0, attacker_level=1, defender_level=10_000)
        # Assert: floor is base * 0.5 == 10, never zero from the level term alone
        assert dmg == 10


# --------------------------------------------------------------------------- #
# skill / momentum multipliers stay non-negative
# --------------------------------------------------------------------------- #
class TestMultiplierFloors:
    def test_negative_skill_mult_is_floored_to_zero(self) -> None:
        # Arrange / Act
        dmg = compute_damage(score=80.0, skill_mult=-3.0)
        # Assert
        assert dmg == 0

    def test_negative_momentum_is_floored_to_zero(self) -> None:
        # Arrange / Act
        dmg = compute_damage(score=80.0, momentum=-1.0)
        # Assert
        assert dmg == 0

    def test_momentum_scales_damage(self) -> None:
        # Arrange: base 20 * 1.3 momentum
        # Act
        dmg = compute_damage(score=70.0, momentum=1.3)
        # Assert: round(20 * 1.3) == 26
        assert dmg == 26

    def test_damage_is_always_non_negative_integer(self) -> None:
        # Arrange / Act
        dmg = compute_damage(score=63.0, attacker_type="LOGOS", defender_type="ETHOS")
        # Assert
        assert isinstance(dmg, int)
        assert dmg >= 0


# --------------------------------------------------------------------------- #
# multi-defender split: total damage is divided across living defenders
# (mirrors orchestrator._apply_round_damage: round(compute_damage / n))
# --------------------------------------------------------------------------- #
def _split_per_defender(total: int, n_defenders: int) -> int:
    """Pure model of the orchestrator's per-target split."""
    return max(0, round(total / n_defenders))


class TestMultiDefenderSplit:
    def test_single_defender_takes_full_damage(self) -> None:
        # Arrange
        total = compute_damage(score=70.0)  # 20
        # Act
        per = _split_per_defender(total, 1)
        # Assert
        assert per == 20

    def test_two_defenders_each_take_half(self) -> None:
        # Arrange
        total = compute_damage(score=70.0)  # 20
        # Act
        per = _split_per_defender(total, 2)
        # Assert
        assert per == 10

    def test_three_defenders_split_with_rounding(self) -> None:
        # Arrange: 20 / 3 = 6.667 -> round -> 7 each
        total = compute_damage(score=70.0)  # 20
        # Act
        per = _split_per_defender(total, 3)
        # Assert
        assert per == 7

    def test_split_never_exceeds_total_per_target(self) -> None:
        # Arrange
        total = compute_damage(score=100.0)  # 50
        # Act / Assert: each defender's share <= the undivided total
        for n in (1, 2, 4, 8):
            assert _split_per_defender(total, n) <= total

    def test_zero_total_splits_to_zero(self) -> None:
        # Arrange: a weak argument deals 0, split stays 0
        total = compute_damage(score=40.0)  # 0
        # Act
        per = _split_per_defender(total, 3)
        # Assert
        assert per == 0
