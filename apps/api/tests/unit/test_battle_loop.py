"""Unit tests for the dual-agent battle training loop.

All tests stub self-play, so they exercise prompt-genome selection without a
live model provider. The loop is latency-first, but judge score and transcript
quality still need to improve enough for a mutation to be accepted.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from app.training import battle_loop as BL
from app.training import selfplay


def _genome(name: str) -> dict[str, Any]:
    return {
        "harness": {"system_prompt": f"{name} system.", "directives": []},
        "persona": {"name": name, "tone": "measured"},
        "skill_prompt_fragments": [],
        "gambit_rules": [],
        "skills": [],
    }


def _stub_transcript(role: str, opponent: str, topic: str) -> list[dict[str, Any]]:
    return [
        {
            "turn": 1,
            "actor_id": role,
            "actor_role": "party",
            "text": (
                f"{topic} succeeds because this claim answers the opponent's "
                "latest claim with a concrete rebuttal."
            ),
        },
        {
            "turn": 2,
            "actor_id": opponent,
            "actor_role": "enemy",
            "text": f"The opponent claim says {topic} fails because trust breaks.",
        },
    ]


@pytest.mark.asyncio
async def test_battle_loop_trains_party_and_enemy_with_correct_stances(monkeypatch):
    # Arrange
    calls: list[dict[str, Any]] = []

    def fake_mutations(genome, k, rng=None, weights=None):
        variant = copy.deepcopy(genome)
        name = variant.get("persona", {}).get("name", "")
        variant.setdefault("harness", {})["test_score"] = 92.0 if "Enemy" in name else 88.0
        return [(variant, "test_mutation")][:k]

    async def fake_play(genome, **kwargs):
        calls.append(kwargs)
        score = float(genome.get("harness", {}).get("test_score", 60.0))
        return {
            "score": score,
            "source": "stub",
            "transcript": _stub_transcript(
                kwargs["party_id"], kwargs["enemy_id"], kwargs["topic"]
            ),
        }

    monkeypatch.setattr(BL.genome_mod, "sample_mutations", fake_mutations)
    monkeypatch.setattr(BL.selfplay, "play", fake_play)

    # Act
    result = await BL.run_battle_training(
        BL.LoopConfig(topic="Remote work improves focus.", cycles=1, variants=1),
        party_genome=_genome("Party Test"),
        enemy_genome=_genome("Enemy Test"),
    )

    # Assert
    assert result.cycles[0].party.accepted is True
    assert result.cycles[0].enemy.accepted is True
    assert result.party_genome["harness"]["test_score"] == 88.0
    assert result.enemy_genome["harness"]["test_score"] == 92.0
    assert any(c["party_id"] == "party" and "Argue FOR" in c["party_stance"] for c in calls)
    assert any(c["party_id"] == "enemy" and "Argue AGAINST" in c["party_stance"] for c in calls)


@pytest.mark.asyncio
async def test_battle_loop_adds_response_directives_even_without_variants(monkeypatch):
    # Arrange
    async def fake_play(genome, **kwargs):
        return {
            "score": 60.0,
            "source": "stub",
            "transcript": _stub_transcript(
                kwargs["party_id"], kwargs["enemy_id"], kwargs["topic"]
            ),
        }

    monkeypatch.setattr(BL.selfplay, "play", fake_play)

    # Act
    result = await BL.run_battle_training(
        BL.LoopConfig(cycles=1, variants=0),
        party_genome=_genome("Party Test"),
        enemy_genome=_genome("Enemy Test"),
    )

    # Assert
    party_directives = result.party_genome["harness"]["directives"]
    enemy_directives = result.enemy_genome["harness"]["directives"]
    for directive in BL.BATTLE_RESPONSE_DIRECTIVES:
        assert directive in party_directives
        assert directive in enemy_directives


@pytest.mark.asyncio
async def test_battle_loop_does_not_regress_below_prior_best(monkeypatch):
    # Arrange: cycle 1 accepts a strong variant; cycle 2 has a weak baseline and
    # a variant that beats that weak baseline but not the prior best.
    plays = {"n": 0}

    def fake_mutations(genome, k, rng=None, weights=None):
        variant = copy.deepcopy(genome)
        variant.setdefault("harness", {})["is_variant"] = True
        return [(variant, "test_mutation")][:k]

    async def fake_play(genome, **kwargs):
        plays["n"] += 1
        role = kwargs["party_id"]
        is_variant = bool(genome.get("harness", {}).get("is_variant"))
        if role == "party":
            score = 90.0 if plays["n"] <= 2 and is_variant else 70.0
        else:
            score = 70.0
        return {
            "score": score,
            "source": "stub",
            "transcript": _stub_transcript(role, kwargs["enemy_id"], kwargs["topic"]),
        }

    monkeypatch.setattr(BL.genome_mod, "sample_mutations", fake_mutations)
    monkeypatch.setattr(BL.selfplay, "play", fake_play)

    # Act
    result = await BL.run_battle_training(
        BL.LoopConfig(cycles=2, variants=1),
        party_genome=_genome("Party Test"),
        enemy_genome=_genome("Enemy Test"),
    )

    # Assert: final summary keeps the best accepted party score instead of the
    # lower cycle-2 local winner.
    assert result.party_final.judge_score == 90.0


def test_candidate_selection_rejects_errors_and_low_quality() -> None:
    # Arrange
    incumbent = BL.CandidateEval(
        role="party",
        op="incumbent",
        judge_score=60.0,
        latency_s=1.0,
        latency_score=0.85,
        quality_score=0.8,
        error_rate=0.0,
        composite_score=0.75,
        source="stub",
        transcript_excerpt=[],
    )
    broken = BL.CandidateEval(
        role="party",
        op="broken",
        judge_score=100.0,
        latency_s=0.1,
        latency_score=0.98,
        quality_score=0.9,
        error_rate=0.5,
        composite_score=0.99,
        source="stub",
        transcript_excerpt=[],
    )
    vague = BL.CandidateEval(
        role="party",
        op="vague",
        judge_score=90.0,
        latency_s=0.1,
        latency_score=0.98,
        quality_score=0.1,
        error_rate=0.0,
        composite_score=0.9,
        source="stub",
        transcript_excerpt=[],
    )

    # Act / Assert
    assert BL._candidate_beats(broken, incumbent, BL.LoopConfig()) is False
    assert BL._candidate_beats(vague, incumbent, BL.LoopConfig()) is False


def test_selfplay_sanitizes_markdown_and_labels() -> None:
    raw = """## Against Remote Work

Claim: Remote work weakens collaboration.
Support: Teams lose spontaneous correction.
Rebuttal: Async tools help, but they do not fully restore trust.
"""

    text = selfplay._sanitize_turn(raw)

    assert "Claim:" not in text
    assert "Support:" not in text
    assert "Against Remote Work" not in text
    assert text.count(".") == 2


def test_selfplay_sanitizes_meta_and_preserves_argument() -> None:
    raw = (
        "My assigned stance is AGAINST the proposition that remote work improves performance. "
        "The claim that needs to be addressed is the assumption that collaboration survives remotely. "
        "Remote work weakens performance because async gaps slow correction."
    )

    text = selfplay._sanitize_turn(raw)

    assert "assigned stance" not in text.lower()
    assert "needs to be addressed" not in text.lower()
    assert text == "Remote work weakens performance because async gaps slow correction."


def test_selfplay_fallback_turn_is_side_taking() -> None:
    text = selfplay._fallback_turn(
        "remote work improves performance",
        "Argue AGAINST the proposition: remote work improves performance.",
    )

    assert "AGAINST" in text
    assert "remote work improves performance" in text
    assert ".:" not in text
    assert len(text.split(".")) >= 2


async def test_local_selfplay_gateway_failure_uses_argument_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("provider unavailable")

    monkeypatch.setattr(selfplay.gateway, "complete", boom)
    genome = {"persona": {"name": "Tester"}, "harness": {"system_prompt": "Be clear."}}

    # Act
    result = await selfplay._local_self_play(
        genome,
        topic="remote work improves performance",
        rounds=1,
        sparring_genome=None,
        model="broken-model",
        party_id="party",
        enemy_id="enemy",
    )

    # Assert
    texts = [u["text"] for u in result["transcript"] if u["actor_role"] != "judge"]
    assert len(texts) == 2
    assert all("no response" not in text.lower() for text in texts)
    assert all("provider unavailable" not in text.lower() for text in texts)
    assert "FOR remote work improves performance" in texts[0]
    assert "AGAINST remote work improves performance" in texts[1]


def test_default_genomes_keep_harness_thin_and_tactics_in_skills() -> None:
    party = BL.default_party_genome()
    enemy = BL.default_enemy_genome()

    assert party["harness"]["system_prompt"].startswith("Thin party battle harness")
    assert enemy["harness"]["system_prompt"].startswith("Thin enemy battle harness")
    assert any("team performance" in f for f in party["skill_prompt_fragments"])
    assert any("failure mode" in f for f in enemy["skill_prompt_fragments"])
    assert all("failure mode" not in d for d in enemy["harness"]["directives"])


def test_selfplay_sanitizer_does_not_cut_second_sentence_mid_thought() -> None:
    raw = (
        "Remote work can improve performance when teams use clear ownership and written goals. "
        "This second sentence is intentionally long because it should be dropped instead of "
        "being cut into a strange fragment that sounds unfinished and hurts battle quality."
    )

    text = selfplay._sanitize_turn(raw)

    assert text == "Remote work can improve performance when teams use clear ownership and written goals."
    assert "strange fragment" not in text
