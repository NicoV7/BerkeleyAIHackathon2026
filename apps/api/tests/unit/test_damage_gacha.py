"""Gacha-wave unit tests for ``compute_damage`` — the ATK/DEF/domain terms.

Wave 0 extended ``compute_damage`` with three additive kwargs:

  * ``attacker_atk`` / ``defender_def`` — stat ratio (atk / max(def, 1))
  * ``domain_match``                    — topic-domain multiplier (0.5..2.0)

These tests assert the formula reduces cleanly to the pre-gacha shape with the
defaults (10 / 10 / 1.0), scales linearly when stats diverge, applies the
domain-match nudge, and is safe against a zero-DEF defender (the ``max(1)``
guard prevents a divide-by-zero).

Pure-function: no DB, no Redis, no live stack — runs in plain pytest.
"""
from __future__ import annotations

import pytest

from app.debate.damage import compute_damage


# --------------------------------------------------------------------------- #
# Defaults — pre-gacha behavior is preserved when no new kwargs are passed.
# --------------------------------------------------------------------------- #
class TestGachaDefaultsAreNeutral:
    def test_default_atk_def_match_yields_unchanged_base(self) -> None:
        # Arrange: score 70 -> base 20, all multipliers neutral.
        # Act
        dmg = compute_damage(score=70.0)
        # Assert: identical to the pre-gacha number (20).
        assert dmg == 20

    def test_explicit_neutral_kwargs_match_implicit_defaults(self) -> None:
        # Arrange / Act
        implicit = compute_damage(score=80.0)
        explicit = compute_damage(
            score=80.0, attacker_atk=10, defender_def=10, domain_match=1.0
        )
        # Assert
        assert implicit == explicit


# --------------------------------------------------------------------------- #
# Stat ratio: attacker_atk / max(defender_def, 1)
# --------------------------------------------------------------------------- #
class TestStatRatio:
    def test_double_atk_doubles_damage(self) -> None:
        # Arrange: atk=20 / def=10 -> ratio 2.0; base 20 -> 40
        # Act
        dmg = compute_damage(score=70.0, attacker_atk=20, defender_def=10)
        # Assert
        assert dmg == 40

    def test_half_atk_halves_damage(self) -> None:
        # Arrange: atk=5 / def=10 -> ratio 0.5; base 20 -> 10
        # Act
        dmg = compute_damage(score=70.0, attacker_atk=5, defender_def=10)
        # Assert
        assert dmg == 10

    def test_higher_def_reduces_damage(self) -> None:
        # Arrange: atk=10 / def=20 -> ratio 0.5; base 20 -> 10
        # Act
        dmg = compute_damage(score=70.0, attacker_atk=10, defender_def=20)
        # Assert
        assert dmg == 10

    def test_zero_def_protected_by_max_one(self) -> None:
        # Arrange: def=0 must not divide-by-zero. Guard clamps to 1, so
        # atk=10 / max(0,1) = 10 -> ratio 10; base 20 -> 200 then *neutral mults.
        # Act
        dmg = compute_damage(score=70.0, attacker_atk=10, defender_def=0)
        # Assert: the guard kept the math finite and integer.
        assert isinstance(dmg, int)
        assert dmg == 200

    def test_negative_atk_floors_to_zero_damage(self) -> None:
        # Arrange: a stripped attacker (atk < 0) -> ratio 0 -> 0 damage.
        # Act
        dmg = compute_damage(score=70.0, attacker_atk=-5, defender_def=10)
        # Assert
        assert dmg == 0


# --------------------------------------------------------------------------- #
# Domain match multiplier (clamped to [0.5, 2.0]).
# --------------------------------------------------------------------------- #
class TestDomainMatch:
    def test_match_adds_twenty_percent(self) -> None:
        # Arrange: base 20 * 1.2 = 24
        # Act
        dmg = compute_damage(score=70.0, domain_match=1.2)
        # Assert
        assert dmg == 24

    def test_mismatch_penalizes_ten_percent(self) -> None:
        # Arrange: base 20 * 0.9 = 18
        # Act
        dmg = compute_damage(score=70.0, domain_match=0.9)
        # Assert
        assert dmg == 18

    def test_extreme_domain_match_clamped_high(self) -> None:
        # Arrange: 5.0 clamps to 2.0 -> base 20 * 2.0 = 40
        # Act
        dmg = compute_damage(score=70.0, domain_match=5.0)
        # Assert
        assert dmg == 40

    def test_extreme_domain_match_clamped_low(self) -> None:
        # Arrange: 0.0 clamps to 0.5 -> base 20 * 0.5 = 10
        # Act
        dmg = compute_damage(score=70.0, domain_match=0.0)
        # Assert
        assert dmg == 10


# --------------------------------------------------------------------------- #
# Composition: all three terms together still produce a deterministic integer.
# --------------------------------------------------------------------------- #
class TestComposition:
    def test_atk_def_and_domain_compose_multiplicatively(self) -> None:
        # Arrange: base 20 * (20/10) * 1.2 = 48
        # Act
        dmg = compute_damage(
            score=70.0, attacker_atk=20, defender_def=10, domain_match=1.2
        )
        # Assert
        assert dmg == 48

    @pytest.mark.parametrize(
        "atk,def_,dom,expected",
        [
            (10, 10, 1.0, 20),  # neutral baseline
            (20, 10, 1.0, 40),  # stat advantage
            (10, 10, 0.9, 18),  # domain mismatch
            (20, 10, 1.2, 48),  # both bonuses stack
            (5, 20, 0.9, 4),    # double penalty: base 20 * 0.25 * 0.9 = 4.5 -> round 4
        ],
    )
    def test_parametric_compose(
        self, atk: int, def_: int, dom: float, expected: int
    ) -> None:
        # Arrange / Act
        dmg = compute_damage(
            score=70.0, attacker_atk=atk, defender_def=def_, domain_match=dom
        )
        # Assert
        assert dmg == expected
