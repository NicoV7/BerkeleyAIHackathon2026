"""Unit tests for Agent 3 — BALANCE + PROGRESSION.

Pure-logic coverage, NO live DB / Redis / model gateway. Exercises:

  * ``app.party.balance``        — level curve monotonic, XP economy, evolution
                                    thresholds; defaults match the shipped numbers.
  * ``app.party.progress``       — evolution APPENDS a genome fragment and does
                                    NOT overwrite a trained system_prompt / gambits.
  * ``app.training.genome``      — ``append_fragment`` is append-only & idempotent.
  * ``app.debate.damage``        — overridable type chart returns the SAME
                                    multipliers as before for known pairs.
  * ``app.scripts.seed_catalog`` — seeding is idempotent (running twice = no dup).

Style: Arrange-Act-Assert with descriptive names.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

balance = pytest.importorskip("app.party.balance")
progress = pytest.importorskip("app.party.progress")
genome = pytest.importorskip("app.training.genome")
damage = pytest.importorskip("app.debate.damage")
seed_catalog = pytest.importorskip("app.scripts.seed_catalog")


# --------------------------------------------------------------------------- #
# balance — level -> stat curve is monotonic
# --------------------------------------------------------------------------- #
class TestLevelCurve:
    def test_hp_for_level_is_monotonic_non_decreasing(self) -> None:
        # Arrange / Act
        hps = [balance.hp_for_level(lvl) for lvl in range(1, 31)]
        # Assert: never decreases as level rises
        assert all(b >= a for a, b in zip(hps, hps[1:]))

    def test_hp_for_level_matches_base_plus_per_level_bonus(self) -> None:
        # Arrange / Act / Assert: documented defaults (base 100, +10/level)
        assert balance.hp_for_level(1) == 100
        assert balance.hp_for_level(2) == 110
        assert balance.hp_for_level(5) == 140
        assert balance.hp_for_level(1) == balance.BASE_HP

    def test_hp_for_level_adds_evolution_bonus_per_stage(self) -> None:
        # Arrange / Act / Assert: each stage adds the +20 evolution bonus
        assert balance.hp_for_level(5, evolution_stage=1) == 140 + 20
        assert balance.hp_for_level(10, evolution_stage=2) == 190 + 40

    def test_hp_bonus_for_level_one_is_zero(self) -> None:
        # Arrange / Act / Assert: you start at level 1 having gained nothing
        assert balance.hp_bonus_for_level(1) == 0
        assert balance.hp_bonus_for_level(3) == 2 * balance.HP_PER_LEVEL

    def test_xp_to_next_is_strictly_increasing(self) -> None:
        # Arrange / Act
        costs = [balance.xp_to_next(lvl) for lvl in range(1, 21)]
        # Assert: strictly increasing linear curve
        assert all(b > a for a, b in zip(costs, costs[1:]))
        assert balance.xp_to_next(1) == 100
        assert balance.xp_to_next(2) == 200

    def test_total_xp_for_level_accumulates_costs(self) -> None:
        # Arrange / Act / Assert: reaching L1 is free; L3 = 100 + 200
        assert balance.total_xp_for_level(1) == 0
        assert balance.total_xp_for_level(3) == 100 + 200

    def test_xp_reward_scales_with_enemy_level_and_drops_on_loss(self) -> None:
        # Arrange / Act
        win = balance.xp_reward(5, won=True)
        loss = balance.xp_reward(5, won=False)
        tougher = balance.xp_reward(10, won=True)
        # Assert
        assert win == 50 + 10 * 5            # base + per-enemy-level
        assert loss < win                    # consolation < full
        assert tougher > win                 # tougher foe worth more


# --------------------------------------------------------------------------- #
# balance — evolution thresholds
# --------------------------------------------------------------------------- #
class TestEvolutionThresholds:
    @pytest.mark.parametrize(
        ("level", "expected_stage"),
        [(1, 0), (4, 0), (5, 1), (9, 1), (10, 2), (50, 2)],
    )
    def test_evolution_stage_for_level(self, level: int, expected_stage: int) -> None:
        # Arrange / Act / Assert
        assert balance.evolution_stage_for_level(level) == expected_stage

    def test_should_evolve_true_only_when_outgrown_current_stage(self) -> None:
        # Arrange / Act / Assert
        assert balance.should_evolve(5, current_stage=0) is True
        assert balance.should_evolve(5, current_stage=1) is False
        assert balance.should_evolve(10, current_stage=1) is True
        assert balance.should_evolve(4, current_stage=0) is False

    def test_evolution_hp_bonus_default(self) -> None:
        # Arrange / Act / Assert
        assert balance.evolution_hp_bonus() == 20


# --------------------------------------------------------------------------- #
# balance defaults wire through to progress (no behavior change)
# --------------------------------------------------------------------------- #
def test_progress_constants_mirror_balance() -> None:
    # Arrange / Act / Assert: progress re-exports balance values unchanged
    assert progress.XP_PER_LEVEL == balance.XP_PER_LEVEL == 100
    assert progress.HP_PER_LEVEL == balance.HP_PER_LEVEL == 10
    assert progress.xp_needed(3) == balance.xp_to_next(3) == 300
    assert progress.SKILL_UNLOCK_LEVELS == {3, 6, 9}


# --------------------------------------------------------------------------- #
# genome.append_fragment — append-only, never overwrite trained fields
# --------------------------------------------------------------------------- #
class TestAppendFragment:
    def _trained_genome(self) -> dict[str, Any]:
        return {
            "harness": {"system_prompt": "TRAINED PROMPT — do not touch."},
            "persona": {"name": "Sage", "tone": "coldly precise"},
            "skill_prompt_fragments": ["pre-existing fragment"],
            "gambit_rules": [{"priority": 0, "condition": {}, "action": "press"}],
        }

    def test_appends_fragment_and_persona_note_without_touching_trained_fields(self) -> None:
        # Arrange
        g = self._trained_genome()
        # Act
        out = genome.append_fragment(g, "EVOLVED FRAGMENT", persona_note="Evolved note.")
        # Assert: fragment + note appended
        assert "EVOLVED FRAGMENT" in out["skill_prompt_fragments"]
        assert "pre-existing fragment" in out["skill_prompt_fragments"]
        assert "Evolved note." in out["persona"]["evolution_notes"]
        # trained fields untouched
        assert out["harness"]["system_prompt"] == "TRAINED PROMPT — do not touch."
        assert out["gambit_rules"] == g["gambit_rules"]
        # persona base preserved
        assert out["persona"]["name"] == "Sage"
        assert out["persona"]["tone"] == "coldly precise"

    def test_does_not_mutate_input_genome(self) -> None:
        # Arrange
        g = self._trained_genome()
        import copy as _copy

        snapshot = _copy.deepcopy(g)
        # Act
        genome.append_fragment(g, "X", persona_note="Y")
        # Assert
        assert g == snapshot

    def test_appending_existing_fragment_is_idempotent(self) -> None:
        # Arrange
        g = self._trained_genome()
        # Act: append the same fragment twice
        once = genome.append_fragment(g, "DUP")
        twice = genome.append_fragment(once, "DUP")
        # Assert: no duplication
        assert twice["skill_prompt_fragments"].count("DUP") == 1


# --------------------------------------------------------------------------- #
# progress.maybe_evolve — mutates genome append-only, preserves trained fields
# --------------------------------------------------------------------------- #
class _FakeGenomeMonster:
    """A monster-like object carrying a (possibly trained) genome."""

    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", "mon-evo")
        self.level = kw.get("level", 5)
        self.xp = kw.get("xp", 0)
        self.evolution_stage = kw.get("evolution_stage", 0)
        self.max_hp = kw.get("max_hp", 140)
        self.genome_version = kw.get("genome_version", 1)
        self.harness = kw.get("harness", {})
        self.persona = kw.get("persona", {})
        self.skills = kw.get("skills", [])


class TestEvolutionMutatesGenome:
    def test_evolution_appends_fragment_and_bumps_version_and_hp(self) -> None:
        # Arrange — level 5, stage 0, empty genome.
        monster = _FakeGenomeMonster(level=5, evolution_stage=0, max_hp=140)
        # Act
        evolved = progress.maybe_evolve(monster)
        # Assert: stats bumped
        assert evolved is True
        assert monster.evolution_stage == 1
        assert monster.max_hp == 140 + 20
        # genome behavior changed: a fragment was appended
        frags = monster.harness.get("skill_prompt_fragments", [])
        assert any("EVOLVED" in f for f in frags)
        # version bumped so caches know behavior changed
        assert monster.genome_version == 2

    def test_evolution_does_not_overwrite_trained_system_prompt_or_gambits(self) -> None:
        # Arrange — a TRAINED genome with a real system_prompt + gambit rules.
        trained_prompt = "You are Demosthenes. TRAINED BY GEPA — keep this."
        trained_gambits = [{"priority": 0, "condition": {"hp_below": 0.3}, "action": "defend"}]
        monster = _FakeGenomeMonster(
            level=5,
            evolution_stage=0,
            max_hp=140,
            harness={
                "system_prompt": trained_prompt,
                "skill_prompt_fragments": ["trained fragment"],
                "gambit_rules": trained_gambits,
            },
            persona={"name": "Demosthenes", "tone": "aggressive and relentless"},
        )
        # Act
        progress.maybe_evolve(monster)
        # Assert: trained fields are IDENTICAL after evolution
        assert monster.harness["system_prompt"] == trained_prompt
        assert monster.harness["gambit_rules"] == trained_gambits
        assert monster.persona["name"] == "Demosthenes"
        assert monster.persona["tone"] == "aggressive and relentless"
        # but new behavior was appended on top
        assert "trained fragment" in monster.harness["skill_prompt_fragments"]
        assert len(monster.harness["skill_prompt_fragments"]) > 1

    def test_evolution_is_idempotent_at_target_stage(self) -> None:
        # Arrange — already stage 1 at level 6.
        monster = _FakeGenomeMonster(level=6, evolution_stage=1, max_hp=160)
        before_version = monster.genome_version
        # Act
        evolved = progress.maybe_evolve(monster)
        # Assert: nothing happens
        assert evolved is False
        assert monster.evolution_stage == 1
        assert monster.max_hp == 160
        assert monster.genome_version == before_version

    def test_award_xp_to_level_five_triggers_genome_evolution(self) -> None:
        # Arrange — level 4, one level-up to 5 trips evolution.
        monster = _FakeGenomeMonster(level=4, evolution_stage=0, max_hp=130)

        class _Sess:
            def add(self, *_a: Any) -> None:  # noqa: D401
                pass

        # Act — level 4 needs 400 XP to reach level 5.
        result = progress.award_xp(_Sess(), monster, 400)
        # Assert
        assert monster.level == 5
        assert result["evolved"] is True
        assert monster.evolution_stage == 1
        assert any("EVOLVED" in f for f in monster.harness.get("skill_prompt_fragments", []))


# --------------------------------------------------------------------------- #
# damage — overridable type chart returns the SAME multipliers for known pairs
# --------------------------------------------------------------------------- #
class TestTypeChartCompat:
    def test_active_chart_equals_default_chart_out_of_the_box(self) -> None:
        # Arrange / Act / Assert: no override -> identical to shipped defaults
        assert damage.TYPE_CHART == damage.DEFAULT_TYPE_CHART

    @pytest.mark.parametrize(
        ("attacker", "defender", "expected"),
        [
            ("LOGOS", "PATHOS", 1.5),
            ("LOGOS", "ETHOS", 0.75),
            ("CHAOS", "RHETORIC", 1.5),
            ("SOCRATIC", "LOGOS", 0.75),
            ("LOGOS", "SOCRATIC", 1.0),  # unlisted -> neutral
        ],
    )
    def test_multiplier_matches_original_values(
        self, attacker: str, defender: str, expected: float
    ) -> None:
        # Arrange / Act / Assert
        assert damage.type_multiplier(attacker, defender) == expected

    def test_override_then_reset_restores_defaults(self) -> None:
        # Arrange
        original = damage.type_multiplier("LOGOS", "PATHOS")
        # Act: override a single pairing
        damage.override_type_chart({"LOGOS": {"PATHOS": 3.0}})
        overridden = damage.type_multiplier("LOGOS", "PATHOS")
        # reset
        damage.reset_type_chart()
        restored = damage.type_multiplier("LOGOS", "PATHOS")
        # Assert
        assert original == 1.5
        assert overridden == 3.0
        assert restored == 1.5
        # defaults object itself was never mutated
        assert damage.DEFAULT_TYPE_CHART["LOGOS"]["PATHOS"] == 1.5

    def test_set_type_chart_normalizes_keys_to_upper(self) -> None:
        # Arrange / Act
        try:
            damage.set_type_chart({"logos": {"pathos": 2.0}})
            # Assert
            assert damage.type_multiplier("LOGOS", "PATHOS") == 2.0
            assert damage.type_multiplier("logos", "pathos") == 2.0
        finally:
            damage.reset_type_chart()


# --------------------------------------------------------------------------- #
# seed_catalog — idempotent upsert (running twice = no dup)
# --------------------------------------------------------------------------- #
class _FakeSkill:
    """Mimics a Skill row well enough for upsert logic."""

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeSkillSession:
    """In-memory session that upserts _FakeSkill rows by name."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self._pending_name: str | None = None
        self.flushes = 0

    async def execute(self, _stmt: Any) -> Any:
        # The stmt is select(Skill).where(Skill.name == name); we recover the
        # name from the bound parameters of the compiled clause.
        name = _extract_name(_stmt)
        self._pending_name = name
        existing = self.rows.get(name)
        return SimpleNamespace(scalar_one_or_none=lambda: existing)

    def add(self, obj: Any) -> None:
        self.rows[obj.name] = obj

    async def flush(self) -> None:
        self.flushes += 1


def _extract_name(stmt: Any) -> str:
    """Best-effort pull the compared name literal out of a SQLAlchemy select."""
    try:
        compiled = stmt.compile()
        for val in compiled.params.values():
            if isinstance(val, str):
                return val
    except Exception:  # noqa: BLE001
        pass
    return ""


def _models_importable() -> bool:
    try:
        import app.db.models  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_DB_MODELS = pytest.mark.skipif(
    not _models_importable(),
    reason="app.db.models not importable on host (impl fleet mid-edit).",
)


class TestSeedCatalog:
    def test_catalog_rows_have_valid_types_and_power(self) -> None:
        # Arrange / Act
        rows = seed_catalog.catalog()
        valid_types = {"LOGOS", "PATHOS", "ETHOS", "CHAOS", "SOCRATIC", "RHETORIC"}
        # Assert
        assert len(rows) >= 6
        names = [r["name"] for r in rows]
        assert len(names) == len(set(names))  # unique names
        for r in rows:
            assert r["type"] in valid_types
            assert r["power"] > 0
            assert r["prompt_fragment"]  # behavior injection present

    def test_format_type_chart_lists_every_attacker(self) -> None:
        # Arrange / Act
        text = seed_catalog.format_type_chart()
        # Assert
        for attacker in damage.DEFAULT_TYPE_CHART:
            assert attacker in text

    @_DB_MODELS
    def test_seed_is_idempotent_no_duplicates_on_second_run(self) -> None:
        # Arrange
        session = _FakeSkillSession()
        # Act: run twice
        created1, updated1 = asyncio.run(seed_catalog.seed_skill_catalog(session))
        created2, updated2 = asyncio.run(seed_catalog.seed_skill_catalog(session))
        # Assert: first run creates all, second updates all (zero new dupes)
        n = len(seed_catalog.SKILL_CATALOG)
        assert created1 == n
        assert updated1 == 0
        assert created2 == 0          # nothing created on re-run
        assert updated2 == n          # all upserted in place
        assert len(session.rows) == n  # no duplicate rows
