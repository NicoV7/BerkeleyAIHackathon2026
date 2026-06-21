"""Unit tests for BATTLE RESPONSIVENESS + CLEAR STANCES (Agent RESP).

Two live-play P0s these tests lock in WITHOUT a real Ollama/Redis:

  1. RESPONSIVENESS — the human-argue round must STREAM the enemy rebuttal token
     by token (same mechanism as the auto round) so perceived latency is the
     first token, not full generation; and the player-judge + enemy-generation
     LLM calls must OVERLAP (asyncio), not run strictly back-to-back.

  2. CLEAR STANCES — the player's lead argues FOR the topic and the enemy lead
     argues AGAINST it (deterministic); the stance is threaded into the prompt
     and carried on the emitted utterance/token events; and the model-failure
     fallback is a CONCRETE, side-taking sentence about the actual topic — never
     the old meta hedge "I take the side that survives the hardest question".

All gateway/judge/redis seams are faked; no network, no DB.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.debate import orchestrator as orch
from app.debate.orchestrator import Combatant


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #


def _combatant(role: str, mid: str, name: str, mtype: str = "LOGOS") -> Combatant:
    return Combatant(
        monster_id=mid, name=name, type=mtype, role=role, hp=100, max_hp=100, level=1
    )


class _FakeRedis:
    """Records rpush/expire/hset so the round can persist without real Redis."""

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
    """Neutralize every app.redis_state symbol the human round imports."""
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


# --------------------------------------------------------------------------- #
# 1. STREAMING — the human round emits token events, not just one utterance
# --------------------------------------------------------------------------- #


async def test_human_round_streams_enemy_tokens(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    # Enemy rebuttal arrives as several streamed chunks.
    async def fake_stream(messages, model=None, **k):
        for tok in ["Animals ", "don't ", "deserve ", "human ", "rights."]:
            yield tok

    monkeypatch.setattr(orch.gateway, "stream", fake_stream)

    async def fake_score(topic, items, fallback_model=None, **k):
        from app.debate.judge import JudgeScore

        return [JudgeScore(actor_id=it["actor_id"], score=60.0, rationale="ok") for it in items]

    monkeypatch.setattr(orch, "score_round", fake_score)

    combatants = [
        _combatant("party", "p1", "Sage"),
        _combatant("enemy", "e1", "Brute"),
    ]
    # start_turn>0 = a later round -> live STREAMING rebuttal (round 1 emits the
    # materialized opening as a single token; that is a separate A1/A2 concern).
    events = await _drain(
        orch.run_human_round_stream(
            "enc1", "animals deserve rights", combatants, None, 2,
            {"party": 1.0, "enemy": 1.0}, "Animals deserve rights because they feel pain.",
        )
    )

    token_events = [e for e in events if e.kind == "token"]
    # The enemy rebuttal streamed: more than one token event (not a single final blob).
    assert len(token_events) >= 2
    # Tokens reconstruct the enemy text and all carry the enemy's side.
    streamed = "".join(e.data["text"] for e in token_events)
    assert "rights" in streamed
    assert all(e.data["side"] == "against" for e in token_events)
    assert all("server_ts" in e.data and "elapsed_ms" in e.data for e in token_events)

    # The canonical enemy utterance still emits with the full assembled text.
    enemy_utt = [e for e in events if e.kind == "utterance" and e.data["actor_role"] == "enemy"]
    assert enemy_utt and enemy_utt[0].data["text"].strip() == streamed.strip()
    assert "server_ts" in enemy_utt[0].data and "elapsed_ms" in enemy_utt[0].data


# --------------------------------------------------------------------------- #
# 2. PARALLELISM — player-judge runs concurrently with enemy generation
# --------------------------------------------------------------------------- #


async def test_human_round_judges_both_in_one_combined_call(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """Judging is ONE combined score_round call scoring BOTH actors (was two calls).

    Local judging is the measured round-time bottleneck; one call halves it. Per-actor
    damage attribution must survive (score_round maps results back by actor_id).
    """
    calls: list[list[str]] = []

    async def fake_stream(messages, model=None, **k):
        for tok in ["Rights ", "demand ", "duties."]:
            yield tok

    async def fake_score(topic, items, fallback_model=None, **k):
        from app.debate.judge import JudgeScore

        calls.append([it["actor_id"] for it in items])
        # distinct scores per actor so attribution is observable
        return [
            JudgeScore(actor_id=it["actor_id"], score=70.0 if it["actor_id"] == "p1" else 40.0, rationale="ok")
            for it in items
        ]

    monkeypatch.setattr(orch.gateway, "stream", fake_stream)
    monkeypatch.setattr(orch, "score_round", fake_score)

    combatants = [
        _combatant("party", "p1", "Sage"),
        _combatant("enemy", "e1", "Brute"),
    ]
    events = await _drain(
        orch.run_human_round_stream(
            "enc2", "topic", combatants, None, 0,
            {"party": 1.0, "enemy": 1.0}, "My argument.",
        )
    )

    # Exactly ONE judge call, and it scored BOTH actors together.
    assert len(calls) == 1, f"expected one combined judge call, got {len(calls)}"
    assert set(calls[0]) == {"p1", "e1"}
    # Attribution preserved: both scores emit, but only the cycle winner damages HP.
    verdicts = [e.data for e in events if e.kind == "verdict"]
    by_actor = {v["actor_id"]: v for v in verdicts}
    assert {"p1", "e1"} <= set(by_actor)
    assert by_actor["p1"]["target"] == "e1" and by_actor["e1"]["target"] == "p1"
    assert by_actor["p1"]["damage"] > 0
    assert by_actor["e1"]["damage"] == 0


async def test_human_round_streams_enemy_tokens_carry_actor_id(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """The enemy rebuttal must STREAM token events (perceived latency = first token)
    and every token must carry the enemy's actor_id."""
    async def fake_stream(messages, model=None, **k):
        for tok in ["Coun", "ter", "point."]:
            yield tok

    async def fake_score(topic, items, fallback_model=None, **k):
        from app.debate.judge import JudgeScore

        return [JudgeScore(actor_id=it["actor_id"], score=50.0, rationale="ok") for it in items]

    monkeypatch.setattr(orch.gateway, "stream", fake_stream)
    monkeypatch.setattr(orch, "score_round", fake_score)

    combatants = [_combatant("party", "p1", "Sage"), _combatant("enemy", "e1", "Brute")]
    # start_turn>0 = a LATER round, so the enemy rebuts via the live STREAMING path
    # (the first round emits the cached/materialized opening as a single token, which
    # is a separate A1/A2 concern; this test guards the streaming rebuttal).
    events = await _drain(
        orch.run_human_round_stream(
            "enc3", "topic", combatants, None, 2, {"party": 1.0, "enemy": 1.0}, "x"
        )
    )
    token_events = [e for e in events if e.kind == "token"]
    assert len(token_events) >= 2, "enemy rebuttal must stream multiple token events"
    assert all(t.data.get("actor_id") == "e1" for t in token_events)


async def test_stream_assembly_preserves_chunk_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming must not sanitize each chunk so aggressively that words glue."""

    async def fake_stream(messages, model=None, **k):
        for tok in ["Confucius'", " argument ", "relies ", "on ", "standards."]:
            yield tok

    monkeypatch.setattr(orch.gateway, "stream", fake_stream)

    enemy = _combatant("enemy", "e1", "Pedantus18")
    enemy.side = "against"
    chunks = [
        chunk
        async for chunk in orch._stream_utterance(
            enemy,
            "A four-day work week should be the standard.",
            [{"actor_id": "p1", "actor_role": "party", "text": "Four days improves rest."}],
            {},
            [],
            {"p1": "Confucius", "e1": "Pedantus18"},
        )
    ]

    streamed = "".join(chunk["text"] for chunk in chunks if chunk["kind"] == "token")
    done = next(chunk for chunk in chunks if chunk["kind"] == "done")
    assert "Confucius' argument relies on standards." in streamed
    assert "Confucius' argument relies on standards." in done["text"]


async def test_first_human_enemy_turn_rebuts_player_and_uses_cached_context(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
) -> None:
    """Round-one human play should rebut the submitted player line, not emit an opener."""
    import app.redis_state as rs
    import app.debate.materialize as mz

    transcript: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}

    async def append_utterance(eid: str, utt: dict) -> None:
        transcript.append(utt)

    async def get_transcript(eid: str) -> list[dict]:
        return list(transcript)

    async def fake_cached_opening(topic: str, side: str = "against") -> str | None:
        if side == "against":
            return "I argue AGAINST four-day weeks because coordination debt compounds."
        if side == "for":
            return "I argue FOR four-day weeks because rest improves focus."
        return None

    async def fake_stream(messages, model=None, **k):
        captured["model"] = model
        captured["messages"] = messages
        for tok in [
            "Your rest claim ",
            "skips coordination debt. ",
            "Shorter weeks still need handoffs people can trust.",
        ]:
            yield tok

    async def fake_score(topic, items, fallback_model=None, **k):
        from app.debate.judge import JudgeScore

        return [JudgeScore(actor_id=it["actor_id"], score=60.0, rationale="ok") for it in items]

    monkeypatch.setattr(rs, "append_utterance", append_utterance)
    monkeypatch.setattr(rs, "get_transcript", get_transcript)
    monkeypatch.setattr(mz, "get_cached_opening", fake_cached_opening)
    monkeypatch.setattr(orch.gateway, "stream", fake_stream)
    monkeypatch.setattr(orch, "score_round", fake_score)

    events = await _drain(
        orch.run_human_round_stream(
            "enc-grounded",
            "A four-day work week should be the standard.",
            [_combatant("party", "p1", "Confucius"), _combatant("enemy", "e1", "Pedantus18")],
            None,
            0,
            {"party": 1.0, "enemy": 1.0},
            "Four days should be the standard to improve rest.",
        )
    )

    prompt_text = "\n".join(message["content"] for message in captured["messages"])
    enemy_utt = next(e.data for e in events if e.kind == "utterance" and e.data["actor_role"] == "enemy")
    assert captured["model"] == orch.settings.enemy_rebuttal_model
    assert "Four days should be the standard to improve rest." in prompt_text
    assert "Latest opposing claim you must answer" in prompt_text
    assert "own cached opening angle" in prompt_text
    assert "opposing cached opening angle" in prompt_text
    assert "coordination debt" in prompt_text
    assert enemy_utt["text"].startswith("Your rest claim skips coordination debt.")


# --------------------------------------------------------------------------- #
# 3. CLEAR SIDES — player = for, enemy = against, surfaced on utterances
# --------------------------------------------------------------------------- #


async def test_sides_assigned_for_player_against_enemy(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    async def fake_stream(messages, model=None, **k):
        yield "Counterpoint."

    async def fake_score(topic, items, fallback_model=None, **k):
        from app.debate.judge import JudgeScore

        return [JudgeScore(actor_id=it["actor_id"], score=50.0, rationale="ok") for it in items]

    monkeypatch.setattr(orch.gateway, "stream", fake_stream)
    monkeypatch.setattr(orch, "score_round", fake_score)

    player = _combatant("party", "p1", "Sage")
    enemy = _combatant("enemy", "e1", "Brute")
    events = await _drain(
        orch.run_human_round_stream(
            "enc4", "topic", [player, enemy], None, 0,
            {"party": 1.0, "enemy": 1.0}, "Player text.",
        )
    )

    # Sides threaded onto the combatants (used by the prompt builder).
    assert player.side == "for"
    assert enemy.side == "against"

    # Surfaced on the emitted utterances.
    utts = {e.data["actor_role"]: e.data for e in events if e.kind == "utterance"}
    assert utts["party"]["side"] == "for"
    assert utts["enemy"]["side"] == "against"


def test_prompt_states_explicit_side() -> None:
    player = _combatant("party", "p1", "Sage")
    player.side = "for"
    enemy = _combatant("enemy", "e1", "Brute")
    enemy.side = "against"

    p_msgs = orch._build_actor_messages(player, "animals deserve rights", [], {}, [], {})
    e_msgs = orch._build_actor_messages(enemy, "animals deserve rights", [], {}, [], {})
    p_sys = p_msgs[0]["content"]
    e_sys = e_msgs[0]["content"]

    assert "FOR" in p_sys and "animals deserve rights" in p_sys
    assert "AGAINST" in e_sys and "animals deserve rights" in e_sys
    # No meta-hedging instruction leaking into the prompt as guidance to imitate.
    assert "side that survives" not in p_sys.lower()


def test_opening_prompt_does_not_request_missing_rebuttal() -> None:
    player = _combatant("party", "p1", "Sage")
    player.side = "for"

    msgs = orch._build_actor_messages(player, "remote work improves performance", [], {}, [], {})
    sys = msgs[0]["content"]
    user = msgs[1]["content"]

    assert "You are opening for your side" in sys
    assert "first answer the latest opposing claim" not in sys
    assert "Do not mention that no opponent has spoken" in user


def test_rebuttal_prompt_when_opponent_has_spoken() -> None:
    player = _combatant("party", "p1", "Sage")
    player.side = "for"
    transcript = [
        {
            "actor_id": "e1",
            "actor_role": "enemy",
            "text": "Remote work erodes spontaneous collaboration.",
        }
    ]

    msgs = orch._build_actor_messages(
        player,
        "remote work improves performance",
        transcript,
        {},
        [],
        {"e1": "Brute"},
    )

    assert "first answer the latest opposing claim" in msgs[0]["content"]
    assert "Remote work erodes spontaneous collaboration" in msgs[1]["content"]


def test_reaction_utterances_are_not_prompt_context() -> None:
    player = _combatant("party", "p1", "Sage")
    player.side = "for"
    transcript = [
        {
            "actor_id": "e1",
            "actor_role": "enemy",
            "reaction_state": "takes_damage",
            "text": "That hit stings but does not settle the case.",
        },
        {
            "actor_id": "e1",
            "actor_role": "enemy",
            "text": "Remote work erodes spontaneous collaboration.",
        },
    ]

    msgs = orch._build_actor_messages(
        player,
        "remote work improves performance",
        transcript,
        {},
        [],
        {"e1": "Brute"},
    )

    assert "first answer the latest opposing claim" in msgs[0]["content"]
    assert "Remote work erodes spontaneous collaboration" in msgs[1]["content"]
    assert "That hit stings" not in msgs[1]["content"]


def test_reaction_utterances_emit_damage_and_low_hp_lines() -> None:
    party = _combatant("party", "p1", "Sage")
    party.persona = {"tone": "earnest", "voice": "A clear team advocate."}
    enemy = _combatant("enemy", "e1", "Brute")
    enemy.hp = 20
    enemy.persona = {"tone": "combative", "voice": "A wandering contrarian."}
    verdicts = [{"actor_id": "p1", "target": "e1", "damage": 30}]

    reactions = orch._reaction_utterances([party, enemy], verdicts, turn_no=4)

    assert [r["reaction_state"] for r in reactions] == [
        "deals_damage",
        "takes_damage",
        "enemy_low_hp",
    ]
    assert reactions[0]["actor_id"] == "p1"
    assert reactions[1]["actor_id"] == "e1"
    assert reactions[2]["actor_id"] == "p1"
    assert all(r["skill_used"].startswith("reaction:") for r in reactions)
    assert all(r["text"] for r in reactions)


# --------------------------------------------------------------------------- #
# 4. CLEAR-STANCE FALLBACK — concrete + side-taking, NOT the old meta filler
# --------------------------------------------------------------------------- #


def test_fallback_takes_concrete_side_not_meta() -> None:
    topic = "animals deserve rights"
    pro = _combatant("party", "p1", "Sage")
    pro.side = "for"
    con = _combatant("enemy", "e1", "Brute")
    con.side = "against"

    for_text = orch._fallback_argument(pro, topic)
    against_text = orch._fallback_argument(con, topic)

    # Concrete: references the actual topic.
    assert topic in for_text and topic in against_text
    # Side-taking and DIFFERENT per side.
    assert for_text != against_text
    assert "FOR" in for_text
    assert "AGAINST" in against_text
    # NEVER the old meta hedge or the old stage-direction stub.
    for t in (for_text, against_text):
        assert "side that survives" not in t.lower()
        assert "presses the point" not in t.lower()
        assert not t.startswith("(")
        assert len(t.split()) >= 12


def test_fallback_varies_by_side_and_type() -> None:
    topic = "remote work"
    against_logos = _combatant("enemy", "a", "A", "LOGOS")
    against_logos.side = "against"
    against_pathos = _combatant("enemy", "b", "B", "PATHOS")
    against_pathos.side = "against"
    for_logos = _combatant("party", "c", "C", "LOGOS")
    for_logos.side = "for"

    t1 = orch._fallback_argument(against_logos, topic)
    t2 = orch._fallback_argument(against_pathos, topic)
    t3 = orch._fallback_argument(for_logos, topic)
    # Same side, different type -> different framing.
    assert t1 != t2
    # Same type, different side -> different stance.
    assert t1 != t3
    assert "AGAINST" in t1 and "FOR" in t3


async def test_stream_failure_falls_back_to_concrete_side(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """If the enemy stream stalls/errors, the enemy utterance is a real AGAINST line."""

    async def boom_stream(messages, model=None, **k):
        raise RuntimeError("model stalled")
        yield  # pragma: no cover — generator marker

    async def fake_score(topic, items, fallback_model=None, **k):
        from app.debate.judge import JudgeScore

        return [JudgeScore(actor_id=it["actor_id"], score=50.0, rationale="ok") for it in items]

    monkeypatch.setattr(orch.gateway, "stream", boom_stream)
    monkeypatch.setattr(orch, "score_round", fake_score)

    combatants = [_combatant("party", "p1", "Sage"), _combatant("enemy", "e1", "Brute")]
    events = await _drain(
        orch.run_human_round_stream(
            "enc5", "animals deserve rights", combatants, None, 0,
            {"party": 1.0, "enemy": 1.0}, "Player text.",
        )
    )
    enemy_utt = [e for e in events if e.kind == "utterance" and e.data["actor_role"] == "enemy"][0]
    txt = enemy_utt.data["text"]
    assert "AGAINST" in txt
    assert "animals deserve rights" in txt
    assert "side that survives" not in txt.lower()


# --------------------------------------------------------------------------- #
# 5. AUTO round also assigns + surfaces sides (regression guard)
# --------------------------------------------------------------------------- #


def test_side_helper_defaults_from_role() -> None:
    assert orch._side_for(_combatant("party", "p", "P")) == "for"
    assert orch._side_for(_combatant("enemy", "e", "E")) == "against"
    c = _combatant("enemy", "x", "X")
    c.side = "for"  # explicit override wins
    assert orch._side_for(c) == "for"


def test_simultaneous_ko_is_not_reported_as_party_win() -> None:
    party = _combatant("party", "p1", "Sage")
    enemy = _combatant("enemy", "e1", "Brute")
    party.hp = 0
    enemy.hp = 0

    phase, capturable = orch._phase_for([party, enemy])

    assert phase == "lost"
    assert capturable == []


def test_apply_round_damage_uses_autonomous_skill_power() -> None:
    party = _combatant("party", "p1", "Sage", "LOGOS")
    enemy = _combatant("enemy", "e1", "Brute", "LOGOS")
    momentum = {"party": 1.0, "enemy": 1.0}

    verdicts = orch._apply_round_damage(
        [party, enemy],
        [
            (
                party,
                70.0,
                "solid",
                {},
                {"attack_type": "LOGOS", "skill_mult": 2.0},
            ),
            (enemy, 40.0, "weak", {}, {"attack_type": "LOGOS", "skill_mult": 1.0}),
        ],
        momentum,
        topic="remote work",
    )

    expected = round(
        orch.compute_damage(
            score=100.0,
            attacker_type="LOGOS",
            defender_type="LOGOS",
            skill_mult=2.0,
        )
        * orch._battle_damage_multiplier()
    )
    assert verdicts[0]["damage"] == expected
    assert verdicts[1]["damage"] == 0
    assert enemy.hp == 100 - expected
    assert party.hp == 100


def test_apply_round_damage_only_enemy_winner_hits_party() -> None:
    party = _combatant("party", "p1", "Sage", "LOGOS")
    enemy = _combatant("enemy", "e1", "Brute", "LOGOS")
    momentum = {"party": 1.0, "enemy": 1.0}

    verdicts = orch._apply_round_damage(
        [party, enemy],
        [
            (party, 68.0, "solid", {}, {"attack_type": "LOGOS", "skill_mult": 1.0}),
            (enemy, 82.0, "strong", {}, {"attack_type": "LOGOS", "skill_mult": 1.0}),
        ],
        momentum,
        topic="remote work",
    )

    assert verdicts[0]["damage"] == 0
    assert verdicts[1]["damage"] > 0
    assert party.hp < 100
    assert enemy.hp == 100


def test_apply_round_damage_tie_scores_no_one_hits() -> None:
    party = _combatant("party", "p1", "Sage", "LOGOS")
    enemy = _combatant("enemy", "e1", "Brute", "LOGOS")
    momentum = {"party": 1.0, "enemy": 1.0}

    verdicts = orch._apply_round_damage(
        [party, enemy],
        [
            (party, 70.0, "solid", {}, {"attack_type": "LOGOS", "skill_mult": 1.0}),
            (enemy, 70.0, "solid", {}, {"attack_type": "LOGOS", "skill_mult": 1.0}),
        ],
        momentum,
        topic="remote work",
    )

    assert [v["damage"] for v in verdicts] == [0, 0]
    assert party.hp == 100
    assert enemy.hp == 100


def test_cycle_damage_score_scales_with_judge_margin() -> None:
    close = orch._cycle_damage_score("party", 70.0, {"party": [70.0], "enemy": [60.0]})
    decisive = orch._cycle_damage_score("party", 90.0, {"party": [90.0], "enemy": [50.0]})

    assert close == 80.0
    assert decisive == 100.0


def test_sanitize_strips_markdown_labels_and_caps_to_two_sentences() -> None:
    raw = """**Against Remote Work**

Claim: Remote work weakens spontaneous collaboration.
Support: When teams lose fast hallway correction, decisions slow down.
Rebuttal: Async tools help, but they do not replace live trust.
"""

    text = orch._sanitize(raw)

    assert "**" not in text
    assert "Claim:" not in text
    assert "Support:" not in text
    assert "Against Remote Work" not in text
    assert text.count(".") == 2


def test_sanitize_removes_meta_instruction_leaks() -> None:
    raw = (
        'The user wants me to open the debate as the FOR side supporting "Remote work". '
        "I need to output exactly two plain sentences."
    )

    assert orch._sanitize(raw) == ""


def test_sanitize_keeps_substantive_sentence_after_no_opponent_meta() -> None:
    raw = (
        "There is no opposing claim for me to answer yet. "
        "Remote work improves team performance because quiet focus blocks let people finish deep work."
    )

    text = orch._sanitize(raw)

    assert "no opposing claim" not in text.lower()
    assert text.startswith("Remote work improves")


async def test_meta_only_generation_falls_back_to_real_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_complete(messages, model=None, **kwargs):
        return (
            "The user wants me to open the debate as the FOR side. "
            "I need to output exactly two plain sentences."
        )

    monkeypatch.setattr(orch.gateway, "complete", fake_complete)
    player = _combatant("party", "p1", "Sage")
    player.side = "for"

    text, used_fallback, reason = await orch._generate_utterance_traced(
        player,
        "remote work improves performance",
        [],
        {},
        [],
        {"p1": "Sage"},
    )

    assert used_fallback is True
    assert reason == "empty"
    assert "FOR" in text
    assert "remote work improves performance" in text


async def test_generated_actor_turn_gets_second_sentence_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_complete(messages, model=None, **kwargs):
        return "Remote work increases productivity by 4. 8% in controlled studies."

    monkeypatch.setattr(orch.gateway, "complete", fake_complete)
    player = _combatant("party", "p1", "Sage")
    player.side = "for"

    text, used_fallback, reason = await orch._generate_utterance_traced(
        player,
        "remote work improves performance",
        [],
        {},
        [],
        {"p1": "Sage"},
    )

    assert used_fallback is False
    assert reason is None
    assert "4.8%" in text
    assert text.endswith("team throughput.")
    assert "team throughput" in text
