"""T1 backend unit — party progression + capture probability math.

Pure-logic coverage for two frozen game-rule modules, with NO live DB / Redis:

  * ``app.party.progress`` — ``award_xp`` / ``maybe_evolve``:
      level curve (xp_needed = 100 * level), +10 max_hp per level, skill-slot
      unlocks at levels 3/6/9, and evolution_stage bumps at levels 5/10.

  * ``app.party.capture`` — ``attempt_capture`` probability math:
      the clamp formula ``p = clamp(base + (1 - hp/max_hp) * scale, 0, 0.95)``
      and the "must be below 25% HP" capturable-window gate.

Everything here runs against lightweight fakes:
  * a ``FakeMonster`` plain object (no SQLModel / ORM session needed) for the
    synchronous progress functions, and
  * a ``FakeSession`` plus a monkeypatched Redis HP map + a stub ``Monster``
    row for the async capture function.

Collection is ALWAYS safe on a bare host: the modules under test import without
touching I/O, and the single DB-shaped concern (``attempt_capture`` reading
Redis) is fully neutralised by monkeypatching ``app.redis_state.get_hp_map``.
No Postgres, no Redis, no network.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

# Import-time only touches pure Python; if the impl fleet has not landed the
# module yet, skip the whole file rather than erroring collection.
progress = pytest.importorskip("app.party.progress")
capture = pytest.importorskip("app.party.capture")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeMonster:
    """Minimal stand-in for app.db.models.Monster for synchronous progress math.

    Only the attributes touched by award_xp / maybe_evolve are modelled.
    """

    def __init__(
        self,
        *,
        level: int = 1,
        xp: int = 0,
        max_hp: int = 100,
        evolution_stage: int = 0,
        skills: Optional[list] = None,
        id: str = "mon-test",
    ) -> None:
        self.level = level
        self.xp = xp
        self.max_hp = max_hp
        self.evolution_stage = evolution_stage
        self.skills = [] if skills is None else skills
        self.id = id


class FakeSession:
    """Captures .add() calls; award_xp/attempt_capture must NOT commit."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commit_called = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:  # pragma: no cover - presence is the assertion
        self.commit_called = True


# --------------------------------------------------------------------------- #
# progress.xp_needed — the level curve
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("level", "expected"),
    [(1, 100), (2, 200), (3, 300), (10, 1000)],
)
def test_xp_needed_is_one_hundred_times_current_level(level: int, expected: int) -> None:
    # Arrange / Act
    needed = progress.xp_needed(level)
    # Assert
    assert needed == expected
    assert needed == progress.XP_PER_LEVEL * level


# --------------------------------------------------------------------------- #
# progress.award_xp — basic accrual & single level-up
# --------------------------------------------------------------------------- #
def test_award_xp_below_threshold_accrues_without_levelling() -> None:
    # Arrange
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    session = FakeSession()
    # Act
    result = progress.award_xp(session, monster, 50)
    # Assert
    assert result["levelled"] is False
    assert monster.xp == 50
    assert monster.level == 1
    assert monster.max_hp == 100
    assert session.commit_called is False  # caller owns the transaction


def test_award_xp_at_exact_threshold_levels_up_once_and_adds_hp() -> None:
    # Arrange — exactly 100 XP is enough to clear level 1's curve.
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    session = FakeSession()
    # Act
    result = progress.award_xp(session, monster, 100)
    # Assert
    assert result["levelled"] is True
    assert result["new_level"] == 2
    assert monster.level == 2
    assert monster.xp == 0  # 100 awarded - 100 spent
    assert monster.max_hp == 100 + progress.HP_PER_LEVEL  # +10 hp/level
    assert monster in session.added


def test_award_xp_carries_remainder_after_level_up() -> None:
    # Arrange — 150 clears level 1 (100) leaving 50 toward level 2.
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    # Act
    result = progress.award_xp(FakeSession(), monster, 150)
    # Assert
    assert result["new_level"] == 2
    assert monster.xp == 50


def test_award_xp_none_monster_is_a_noop() -> None:
    # Arrange / Act
    result = progress.award_xp(FakeSession(), None, 999)
    # Assert
    assert result == {"levelled": False}


# --------------------------------------------------------------------------- #
# progress.award_xp — multi-level dumps and the +10 hp/level invariant
# --------------------------------------------------------------------------- #
def test_award_xp_large_dump_gains_multiple_levels_with_cumulative_hp() -> None:
    # Arrange — 100 + 200 = 300 XP spans level 1 -> 2 -> 3 (300 carries to L3 cost).
    monster = FakeMonster(level=1, xp=0, max_hp=100)
    # Act — need 100 (L1) + 200 (L2) = 300 to reach level 3.
    result = progress.award_xp(FakeSession(), monster, 300)
    # Assert
    assert monster.level == 3
    assert result["new_level"] == 3
    assert monster.xp == 0
    # +10 per level gained (two level-ups: 1->2, 2->3) = +20.
    assert monster.max_hp == 100 + 2 * progress.HP_PER_LEVEL


# --------------------------------------------------------------------------- #
# progress.award_xp — skill unlocks at levels 3 / 6 / 9
# --------------------------------------------------------------------------- #
def test_award_xp_unlocks_skill_when_reaching_level_three() -> None:
    # Arrange — start at level 2 so a single level-up lands exactly on 3.
    monster = FakeMonster(level=2, xp=0, max_hp=110, skills=[])
    # Act — level 2 needs 200 XP to reach level 3.
    result = progress.award_xp(FakeSession(), monster, 200)
    # Assert
    assert monster.level == 3
    assert "skill_L3" in monster.skills
    assert "skill_L3" in result["skills_unlocked"]


def test_award_xp_does_not_unlock_skill_at_non_milestone_level() -> None:
    # Arrange — reaching level 2 is not a milestone (only 3/6/9).
    monster = FakeMonster(level=1, xp=0, skills=[])
    # Act
    result = progress.award_xp(FakeSession(), monster, 100)
    # Assert
    assert monster.level == 2
    assert monster.skills == []
    assert result["skills_unlocked"] == []


def test_award_xp_skill_unlock_is_idempotent_for_existing_tag() -> None:
    # Arrange — monster already carries the L3 tag; reaching L3 must not dupe it.
    monster = FakeMonster(level=2, xp=0, skills=["skill_L3"])
    # Act
    result = progress.award_xp(FakeSession(), monster, 200)
    # Assert
    assert monster.skills.count("skill_L3") == 1
    assert result["skills_unlocked"] == []  # nothing newly unlocked


# --------------------------------------------------------------------------- #
# progress.maybe_evolve — evolution_stage thresholds at level 5 and 10
# --------------------------------------------------------------------------- #
def test_maybe_evolve_below_first_threshold_does_not_evolve() -> None:
    # Arrange
    monster = FakeMonster(level=4, evolution_stage=0, max_hp=130)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is False
    assert monster.evolution_stage == 0
    assert monster.max_hp == 130  # no evolution HP bonus applied


def test_maybe_evolve_at_level_five_advances_to_stage_one_with_hp_bonus() -> None:
    # Arrange
    monster = FakeMonster(level=5, evolution_stage=0, max_hp=140)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is True
    assert monster.evolution_stage == 1
    assert monster.max_hp == 140 + 20  # +20 evolution bonus


def test_maybe_evolve_at_level_ten_advances_to_stage_two() -> None:
    # Arrange — a monster at level 10 still on stage 0 jumps straight to stage 2.
    monster = FakeMonster(level=10, evolution_stage=0, max_hp=200)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is True
    assert monster.evolution_stage == 2
    assert monster.max_hp == 200 + 20


def test_maybe_evolve_is_idempotent_once_at_target_stage() -> None:
    # Arrange — already stage 1 at level 6; re-running must not re-bump or re-heal.
    monster = FakeMonster(level=6, evolution_stage=1, max_hp=160)
    # Act
    evolved = progress.maybe_evolve(monster)
    # Assert
    assert evolved is False
    assert monster.evolution_stage == 1
    assert monster.max_hp == 160


def test_award_xp_reports_evolved_when_crossing_level_five() -> None:
    # Arrange — start at level 4; one level-up to 5 should trip evolution.
    monster = FakeMonster(level=4, xp=0, evolution_stage=0, max_hp=130)
    # Act — level 4 needs 400 XP to reach level 5.
    result = progress.award_xp(FakeSession(), monster, 400)
    # Assert
    assert monster.level == 5
    assert result["evolved"] is True
    assert monster.evolution_stage == 1


# --------------------------------------------------------------------------- #
# capture._clamp — the probability clamp helper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "lo", "hi", "expected"),
    [
        (-0.5, 0.0, 0.95, 0.0),   # below floor -> floor
        (0.5, 0.0, 0.95, 0.5),    # inside range -> unchanged
        (1.5, 0.0, 0.95, 0.95),   # above ceiling -> ceiling
    ],
)
def test_clamp_bounds_value_into_range(
    value: float, lo: float, hi: float, expected: float
) -> None:
    # Arrange / Act
    clamped = capture._clamp(value, lo, hi)
    # Assert
    assert clamped == expected


def test_capture_probability_formula_matches_documented_clamp() -> None:
    """p = clamp(base + (1 - hp/max_hp) * scale, 0, 0.95).

    Reconstruct the exact formula from the frozen module constants so a future
    constant tweak fails loudly here.
    """
    # Arrange — wild at 10% HP.
    hp_fraction = 0.10
    raw = capture.CAPTURE_BASE + (1.0 - hp_fraction) * capture.CAPTURE_SCALE
    # Act
    p = capture._clamp(raw, 0.0, capture.CAPTURE_MAX_P)
    # Assert — base 0.15 + 0.9*0.80 = 0.87, under the 0.95 cap.
    assert raw == pytest.approx(0.87)
    assert p == pytest.approx(0.87)


def test_capture_probability_is_capped_at_max_for_near_zero_hp() -> None:
    # Arrange — at ~0% HP the raw probability exceeds the cap.
    hp_fraction = 0.0
    raw = capture.CAPTURE_BASE + (1.0 - hp_fraction) * capture.CAPTURE_SCALE
    # Act
    p = capture._clamp(raw, 0.0, capture.CAPTURE_MAX_P)
    # Assert — raw 0.95 sits exactly at the cap.
    assert raw == pytest.approx(0.95)
    assert p == pytest.approx(capture.CAPTURE_MAX_P)


def test_capturable_hp_fraction_gate_is_twenty_five_percent() -> None:
    # Arrange / Act / Assert — guard the documented "< 25%" window constant.
    assert capture.CAPTURABLE_HP_FRACTION == 0.25


# --------------------------------------------------------------------------- #
# capture.attempt_capture — async, with mocked Redis HP + stub Monster row
# --------------------------------------------------------------------------- #
class _StubResult:
    """Mimics SQLAlchemy execute() result for scalar_one_or_none()."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _CaptureFakeSession:
    """Async-execute session returning a preset wild Monster; records .add()."""

    def __init__(self, wild: Any) -> None:
        self._wild = wild
        self.added: list[Any] = []
        self.commit_called = False

    async def execute(self, *_args: Any, **_kwargs: Any) -> _StubResult:
        return _StubResult(self._wild)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:  # pragma: no cover
        self.commit_called = True


def _make_wild_monster(hp: int = 5, max_hp: int = 100):
    """Build a real (transient, unsaved) wild Monster row, or skip if unimportable."""
    models = pytest.importorskip("app.db.models")
    return models.Monster(
        run_id="run-test",
        owner=models.MonsterOwner.wild,
        name="WildArguer",
        type=models.DebateType.logos,
        persona={},
        harness={},
        skills=[],
        level=1,
        xp=0,
        max_hp=max_hp,
        evolution_stage=0,
    )


def _patch_hp_map(monkeypatch: pytest.MonkeyPatch, hp_map: dict[str, int]) -> None:
    """Neutralise Redis: attempt_capture does `from app.redis_state import get_hp_map`."""
    redis_state = pytest.importorskip("app.redis_state")

    async def _fake_get_hp_map(_eid: str) -> dict[str, int]:
        return hp_map

    monkeypatch.setattr(redis_state, "get_hp_map", _fake_get_hp_map)


def test_attempt_capture_rejects_when_wild_too_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — wild at 50% HP is above the 25% capturable window.
    wild = _make_wild_monster(max_hp=100)
    wild.id = "wild-1"
    session = _CaptureFakeSession(wild)
    _patch_hp_map(monkeypatch, {"wild-1": 50})
    # Act
    success, monster, message = asyncio.run(
        capture.attempt_capture(session, "enc-1", "wild-1")
    )
    # Assert
    assert success is False
    assert monster is None
    assert "too healthy" in message.lower()
    assert session.added == []  # ownership never flipped


def test_attempt_capture_forced_succeeds_inside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — wild at 5% HP (inside window); force bypasses the random roll.
    wild = _make_wild_monster(max_hp=100)
    wild.id = "wild-2"
    session = _CaptureFakeSession(wild)
    _patch_hp_map(monkeypatch, {"wild-2": 5})
    # Act
    success, monster, message = asyncio.run(
        capture.attempt_capture(session, "enc-2", "wild-2", force=True)
    )
    # Assert
    models = pytest.importorskip("app.db.models")
    assert success is True
    assert monster is wild
    assert wild.owner == models.MonsterOwner.player  # ownership flipped
    assert wild in session.added
    assert "captured" in message.lower()


def test_attempt_capture_force_still_enforces_hp_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — even forced, a healthy wild (50%) must be rejected by the gate.
    wild = _make_wild_monster(max_hp=100)
    wild.id = "wild-3"
    session = _CaptureFakeSession(wild)
    _patch_hp_map(monkeypatch, {"wild-3": 50})
    # Act
    success, monster, _message = asyncio.run(
        capture.attempt_capture(session, "enc-3", "wild-3", force=True)
    )
    # Assert
    assert success is False
    assert monster is None


def test_attempt_capture_seeded_roll_is_deterministic_at_low_hp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — at 1% HP, p ~= 0.94; a fixed seed makes the roll reproducible.
    wild = _make_wild_monster(max_hp=100)
    wild.id = "wild-4"
    _patch_hp_map(monkeypatch, {"wild-4": 1})
    # Act — two independent runs with the same seed must agree.
    first = asyncio.run(
        capture.attempt_capture(
            _CaptureFakeSession(wild), "enc-4", "wild-4", seed=12345
        )
    )
    # Reset ownership so the second run isn't short-circuited as "already captured".
    models = pytest.importorskip("app.db.models")
    wild.owner = models.MonsterOwner.wild
    second = asyncio.run(
        capture.attempt_capture(
            _CaptureFakeSession(wild), "enc-4", "wild-4", seed=12345
        )
    )
    # Assert — identical success outcome for identical seed.
    assert first[0] == second[0]


def test_attempt_capture_missing_monster_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — execute() yields no row.
    session = _CaptureFakeSession(None)
    _patch_hp_map(monkeypatch, {})
    # Act
    success, monster, message = asyncio.run(
        capture.attempt_capture(session, "enc-5", "ghost")
    )
    # Assert
    assert success is False
    assert monster is None
    assert "not found" in message.lower()


def test_attempt_capture_already_player_owned_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — a player-owned monster cannot be re-captured.
    models = pytest.importorskip("app.db.models")
    wild = _make_wild_monster(max_hp=100)
    wild.id = "wild-6"
    wild.owner = models.MonsterOwner.player
    session = _CaptureFakeSession(wild)
    _patch_hp_map(monkeypatch, {"wild-6": 1})
    # Act
    success, monster, message = asyncio.run(
        capture.attempt_capture(session, "enc-6", "wild-6")
    )
    # Assert
    assert success is False
    assert monster is wild
    assert "already captured" in message.lower()
