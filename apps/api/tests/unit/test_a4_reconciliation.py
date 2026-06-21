"""Unit tests for A4 — RECONCILIATION DEADLINE on the human-argue round.

Autoplan finding (A4): the optimistic `estimate` event renders a heuristic score
for the player instantly, but HP still waited on `await score_round(...)`, which on
a stalled single-slot Ollama serially times out each judge candidate (~28-56s) —
leaving the round visually dangling long after the estimate appeared.

These tests lock in:
  1. EVENT ORDER — player-utterance -> estimate -> verdict -> hp, so the UI gets
     instant feedback (estimate) and a single authoritative settle (verdict+hp).
  2. STALLED-JUDGE SETTLEMENT — when score_round hangs past the deadline, the round
     still settles HP from the heuristic WITHIN the deadline (the verdict carries
     the heuristic score, damage applies, hp emits) rather than dangling.

All gateway/judge/redis seams are faked (reusing the test_battle_responsiveness
pattern); no network, no DB.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.debate import orchestrator as orch
from app.debate.judge import JudgeScore, heuristic_score
from app.debate.orchestrator import Combatant


# --------------------------------------------------------------------------- #
# Helpers / fakes (mirrors test_battle_responsiveness.py)
# --------------------------------------------------------------------------- #


def _combatant(role: str, mid: str, name: str, mtype: str = "LOGOS") -> Combatant:
    return Combatant(
        monster_id=mid, name=name, type=mtype, role=role, hp=100, max_hp=100, level=1
    )


class _FakeRedis:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []

    async def rpush(self, key: str, val: str) -> None:
        self.pushed.append((key, val))

    async def expire(self, key: str, ttl: int) -> None:
        return None

    async def hset(self, *a: Any, **k: Any) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    import app.redis_state as rs

    fr = _FakeRedis()

    async def _append_utterance(eid: str, utt: dict) -> None:
        return None

    async def _set_hp(eid: str, mid: str, hp: int) -> None:
        return None

    async def _get_transcript(eid: str) -> list[dict]:
        return []

    monkeypatch.setattr(rs, "append_utterance", _append_utterance)
    monkeypatch.setattr(rs, "set_hp", _set_hp)
    monkeypatch.setattr(rs, "get_transcript", _get_transcript)
    monkeypatch.setattr(rs, "get_redis", lambda: fr)
    monkeypatch.setattr(rs, "k_judge", lambda eid: f"enc:{eid}:judge")
    monkeypatch.setattr(rs, "ENCOUNTER_TTL_SECONDS", 3600, raising=False)
    return fr


async def _drain(agen) -> list[orch.Event]:
    return [ev async for ev in agen]


def _fake_candidates(text: str = "Counterpoint.", ok: bool = True):
    """Fake `run_candidates`: the enemy turn now generates its full rebuttal in
    ONE non-streaming candidate-chain call (no token deltas). Returns `text`
    with no network so these reconciliation tests stay hermetic."""
    from app.gateway.candidates import CandidateResult

    async def fake_run_candidates(specs, messages, **k):
        return CandidateResult(text=text, ok=ok and bool(text), attempts=1)

    return fake_run_candidates


# --------------------------------------------------------------------------- #
# 1. EVENT ORDER — utterance -> estimate -> verdict -> hp
# --------------------------------------------------------------------------- #


async def test_event_order_player_utterance_estimate_verdict_hp(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    async def fake_score(topic, items, fallback_model=None, **k):
        return [JudgeScore(actor_id=it["actor_id"], score=60.0, rationale="ok") for it in items]

    # Round 1 (start_turn=0): the enemy rebuts the player's just-typed argument
    # via the candidate chain (no canned opening). Fake it so the test never
    # touches a real gateway/redis; the event-order invariant is what we assert.
    monkeypatch.setattr("app.gateway.candidates.run_candidates", _fake_candidates())
    monkeypatch.setattr(orch, "score_round", fake_score)

    player = _combatant("party", "p1", "Sage")
    enemy = _combatant("enemy", "e1", "Brute")
    events = await _drain(
        orch.run_human_round_stream(
            "encA4a", "topic", [player, enemy], None, 0,
            {"party": 1.0, "enemy": 1.0}, "My argument that is on topic.",
        )
    )

    kinds = [e.kind for e in events]

    # The player utterance is the first emitted event.
    first_player_utt = next(
        i for i, e in enumerate(events)
        if e.kind == "utterance" and e.data["actor_role"] == "party"
    )
    i_estimate = kinds.index("estimate")
    i_verdict = kinds.index("verdict")
    i_hp = kinds.index("hp")

    # player-utterance -> estimate -> verdict -> hp.
    assert first_player_utt < i_estimate < i_verdict < i_hp

    # The estimate is for the player, carries the heuristic display score + side.
    est = events[i_estimate].data
    assert est["actor_id"] == "p1"
    assert est["side"] == "for"
    assert est["score"] == pytest.approx(heuristic_score("topic", "My argument that is on topic."))


# --------------------------------------------------------------------------- #
# 2. STALLED JUDGE — HP settles from heuristic within the deadline
# --------------------------------------------------------------------------- #


async def test_stalled_judge_settles_from_heuristic_within_deadline(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """A stalled score_round must NOT dangle the round: the human-judge deadline
    fires and HP/verdict settle from the heuristic well inside the stall time."""

    # score_round hangs far longer than the deadline (simulates serial Ollama
    # candidate timeouts on a stalled single slot).
    async def hanging_score(topic, items, fallback_model=None, **k):
        await asyncio.sleep(30)
        return [JudgeScore(actor_id=it["actor_id"], score=99.0, rationale="late") for it in items]

    # Tight deadline so the test is fast and deterministic.
    monkeypatch.setattr(orch, "_human_judge_deadline", lambda: 0.05)
    monkeypatch.setattr("app.gateway.candidates.run_candidates", _fake_candidates())
    monkeypatch.setattr(orch, "score_round", hanging_score)

    player = _combatant("party", "p1", "Sage")
    enemy = _combatant("enemy", "e1", "Brute")
    player_text = "Animals deserve rights because they demonstrably feel pain."

    started = time.monotonic()
    # The enemy rebuts via the candidate chain; the judge-deadline is what gates
    # settlement here.
    events = await _drain(
        orch.run_human_round_stream(
            "encA4b", "animals deserve rights", [player, enemy], None, 2,
            {"party": 1.0, "enemy": 1.0}, player_text,
        )
    )
    elapsed = time.monotonic() - started

    # Settled FAST (deadline ~0.05s), nowhere near the 30s judge stall.
    assert elapsed < 5.0, f"round dangled on a stalled judge ({elapsed:.1f}s)"

    # Verdict + hp still emitted (round did not dangle).
    verdicts = [e.data for e in events if e.kind == "verdict"]
    hp_events = [e.data for e in events if e.kind == "hp"]
    assert verdicts, "no verdict emitted after the judge deadline"
    assert hp_events, "no hp emitted after the judge deadline"

    # The committed score is the HEURISTIC (NOT the late model's 99.0), i.e. the
    # already-displayed estimate became authoritative.
    by_actor = {v["actor_id"]: v for v in verdicts}
    assert "p1" in by_actor
    expected = heuristic_score("animals deserve rights", player_text)
    assert by_actor["p1"]["score"] == pytest.approx(expected)
    assert by_actor["p1"]["score"] != pytest.approx(99.0)

    # Damage actually applied to the enemy (HP committed, not left full).
    enemy_hp = next(h["hp"] for h in hp_events if h["monster_id"] == "e1")
    assert enemy_hp < 100


async def test_estimate_score_matches_committed_score_on_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """The optimistic estimate and the post-deadline authoritative verdict carry
    the SAME heuristic score for the player — the estimate is settled, not replaced
    by a different number, so the UI animation reconciles cleanly."""

    async def hanging_score(topic, items, fallback_model=None, **k):
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(orch, "_human_judge_deadline", lambda: 0.05)
    monkeypatch.setattr("app.gateway.candidates.run_candidates", _fake_candidates("Nope."))
    monkeypatch.setattr(orch, "score_round", hanging_score)

    player = _combatant("party", "p1", "Sage")
    enemy = _combatant("enemy", "e1", "Brute")
    events = await _drain(
        orch.run_human_round_stream(
            "encA4c", "topic", [player, enemy], None, 2,
            {"party": 1.0, "enemy": 1.0}, "A reasonably substantive argument here.",
        )
    )

    est = next(e.data for e in events if e.kind == "estimate")
    player_verdict = next(
        e.data for e in events if e.kind == "verdict" and e.data["actor_id"] == "p1"
    )
    assert est["score"] == pytest.approx(player_verdict["score"])
