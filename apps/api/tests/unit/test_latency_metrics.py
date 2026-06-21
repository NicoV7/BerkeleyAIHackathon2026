"""Unit tests for WS-0-LAT per-round latency instrumentation.

Scope (pure, no live model / no DB / no Redis):
  * The RoundTimer records per-utterance gen time + fallback (and WHY), computes
    p50/p95/max, and feeds the process-wide fallback counters.
  * The streaming path signals fallback (timeout vs empty) on its `done` chunk so
    the metrics can attribute it without re-deriving.
  * REGRESSION GUARD — a normal skill turn makes NO EXTRA gateway call beyond the
    baseline (one generation per actor turn + one judge call per round). This is
    the "do not silently route battles anywhere / no parallel model calls" invariant:
    instrumentation must be observe-only.

The gateway/judge are mocked with COUNTING stubs so we assert on call counts and
the recorded metrics without any Ollama/Anthropic/OpenAI call.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.debate import latency_metrics as lm
from app.debate import orchestrator as orch
from app.debate.judge import JudgeScore
from app.debate.orchestrator import Combatant


@pytest.fixture(autouse=True)
def _reset_counters() -> None:
    lm.reset_counters()
    yield
    lm.reset_counters()


# --------------------------------------------------------------------------- #
# 1. RoundTimer mechanics
# --------------------------------------------------------------------------- #


def test_round_timer_records_utterances_and_fallbacks() -> None:
    rt = lm.RoundTimer.start("enc1", round_no=2)
    with rt.utterance("a1", "party", "for"):
        pass  # success — no fallback
    with rt.utterance("e1", "enemy", "against") as u:
        u.mark_fallback("timeout")

    summary = rt.finish()
    assert summary["event"] == "round"
    assert summary["eid"] == "enc1"
    assert summary["round_no"] == 2
    assert summary["utterances"] == 2
    assert summary["fallbacks"] == 1
    assert summary["fallback_rate"] == pytest.approx(0.5)
    assert set(summary["gen_ms"].keys()) == {"p50", "p95", "max"}
    # The fallback actor carries the reason.
    e1 = next(a for a in summary["actors"] if a["actor_id"] == "e1")
    assert e1["fallback"] is True
    assert e1["fallback_reason"] == "timeout"


def test_process_counters_aggregate_across_rounds_and_split_by_reason() -> None:
    rt = lm.RoundTimer.start("enc2")
    with rt.utterance("a", "party", "for"):
        pass
    with rt.utterance("b", "enemy", "against") as u:
        u.mark_fallback("timeout")
    with rt.utterance("c", "enemy", "against") as u:
        u.mark_fallback("empty")
    rt.finish()

    snap = lm.counters_snapshot()
    assert snap["utterances"] == 3
    assert snap["fallbacks"] == 2
    assert snap["fallback_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert snap["fallback_by_reason"] == {"timeout": 1, "empty": 1}


def test_percentile_nearest_rank() -> None:
    vals = [10.0, 20.0, 30.0, 40.0, 100.0]
    assert lm._percentile(vals, 50) == 30.0
    assert lm._percentile(vals, 95) == 100.0
    assert lm._percentile([], 50) == 0.0
    assert lm._percentile([7.0], 95) == 7.0


# --------------------------------------------------------------------------- #
# 2. The streaming path signals fallback reason on `done`
# --------------------------------------------------------------------------- #


def _combatant(side: str, role: str) -> Combatant:
    return Combatant(
        monster_id=f"m-{side}", name="T", type="LOGOS", role=role, hp=100,
        max_hp=100, side=side,
    )


def test_stream_done_reports_timeout_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled first token -> the `done` chunk reports fallback=True reason=timeout."""
    # WS-4: _first_token_timeout now takes an optional model arg (warm widening).
    monkeypatch.setattr(orch, "_first_token_timeout", lambda model=None: 0.01)

    async def _never_yields(messages, model=None, **k):
        await asyncio.sleep(5)
        yield "too late"

    monkeypatch.setattr(orch.gateway, "stream", _never_yields)

    actor = _combatant("against", "enemy")

    async def _drive() -> dict[str, Any]:
        done: dict[str, Any] = {}
        async for chunk in orch._stream_utterance(
            actor, "Topic", [], {"behavior": None, "skill": None}, [], {}
        ):
            if chunk["kind"] == "done":
                done = chunk
        return done

    done = asyncio.run(_drive())
    assert done["fallback"] is True
    assert done["fallback_reason"] == "timeout"
    assert done["text"]  # a real templated fallback line, not empty


def test_stream_done_reports_no_fallback_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(messages, model=None, **k):
        for tok in ("I ", "argue ", "AGAINST."):
            yield tok

    monkeypatch.setattr(orch.gateway, "stream", _ok)
    actor = _combatant("against", "enemy")

    async def _drive() -> dict[str, Any]:
        done: dict[str, Any] = {}
        async for chunk in orch._stream_utterance(
            actor, "Topic", [], {"behavior": None, "skill": None}, [], {}
        ):
            if chunk["kind"] == "done":
                done = chunk
        return done

    done = asyncio.run(_drive())
    assert done["fallback"] is False
    assert done["fallback_reason"] is None


# --------------------------------------------------------------------------- #
# 3. REGRESSION GUARD — instrumentation adds NO extra gateway call
# --------------------------------------------------------------------------- #


def test_skill_turn_makes_no_extra_gateway_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One headless round (2 actors) must make exactly the baseline LLM calls:
    one `complete` per actor turn (2) + one `score_round` (judged once). The
    latency instrumentation must NOT add a single extra gateway call, and must NOT
    route to any candidate chain (battles stay local + single-slot)."""
    complete_calls: list[dict[str, Any]] = []
    stream_calls: list[dict[str, Any]] = []
    score_calls: list[dict[str, Any]] = []

    async def fake_complete(messages, model=None, **kwargs):
        complete_calls.append({"model": model})
        return "I argue a concrete claim about the topic."

    async def fake_stream(messages, model=None, **kwargs):
        stream_calls.append({"model": model})
        yield "streamed"

    async def fake_score(topic, items, fallback_model=None, **k):
        score_calls.append({"n": len(items)})
        return [JudgeScore(actor_id=it["actor_id"], score=55.0, rationale="ok") for it in items]

    monkeypatch.setattr(orch.gateway, "complete", fake_complete)
    monkeypatch.setattr(orch.gateway, "stream", fake_stream)
    monkeypatch.setattr(orch, "score_round", fake_score)

    party = {"id": "p", "name": "P", "type": "LOGOS", "owner": "player", "max_hp": 100}
    enemy = {"id": "e", "name": "E", "type": "LOGOS", "owner": "wild", "max_hp": 100}

    result = orch.run_self_play(party, enemy, "Topic", rounds=1)
    assert result["result"] in {"debating", "won", "lost", "capturable"}

    # Headless self-play uses `complete` (non-streaming) — exactly ONE per actor turn.
    assert len(complete_calls) == 2, f"expected 2 actor completes, got {len(complete_calls)}"
    # Exactly ONE judge call for the round (no per-utterance judging).
    assert len(score_calls) == 1, f"expected 1 judge call, got {len(score_calls)}"
    # The headless path must NOT use the streaming gateway at all.
    assert stream_calls == []
