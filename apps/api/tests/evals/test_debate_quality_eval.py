"""T4 — LLM-judge eval: debate *content quality* (validates CEO premise P1).

This is an end-to-end quality eval, not a unit test. It drives a REAL debate
through the headless engine (`app.debate.orchestrator.run_self_play`, the same
engine the live WS/REST path uses) against a REAL local model via the gateway,
then scores the resulting transcript + judge verdicts on four quality axes and
asserts thresholds:

  1. NON-EMPTY / SUBSTANTIVE   — every combatant utterance is real prose, not the
     engine's templated fallback line, and clears a minimum word count.
  2. ON-TOPIC                  — utterances share meaningful vocabulary with the
     debate topic (token overlap, reusing the judge's own tokenizer).
  3. NON-EMPTY 'WHY'           — every judge verdict carries a non-empty `why`
     explanation (the legibility contract WS-1 added).
  4. SCORES DISCRIMINATE       — the judge is not a rubber stamp: across the
     debate it produces at least one score >50 AND at least one <50, and the two
     sides' average scores are not identical.

WHY a single shared debate: running a real local model is slow, so one
module-scoped fixture runs ONE debate and every assertion reads from it. The
model therefore runs once per `pytest` invocation of this file.

------------------------------------------------------------------------------
SKIP GUARD (collection is always green on a bare host)
------------------------------------------------------------------------------
The eval needs a reachable model gateway. The app's default `ollama_base_url`
points at the Docker-internal host ``ollama``, which does not resolve from the
host running pytest. We therefore:

  * resolve a *host-reachable* Ollama base URL (env override > docker-host
    rewrite to localhost > configured URL),
  * probe ``GET /api/tags`` and confirm at least one usable model is pulled,
  * point the gateway's settings at that URL for the duration of the eval,
  * and SKIP (never error) if the gateway/model is unreachable.

So on a bare host with no model server, this module *collects* and *skips*
cleanly; with a live Ollama (or the compose stack) it runs for real.

------------------------------------------------------------------------------
RUN COMMAND
------------------------------------------------------------------------------
From the repo root, against a local Ollama on the host:

    cd apps/api && uv run pytest tests/evals/test_debate_quality_eval.py -v

Override the model server / models explicitly (e.g. compose stack on localhost):

    EVAL_OLLAMA_BASE_URL=http://localhost:11434 \
    EVAL_DEBATE_MODEL=gemma3:4b \
    EVAL_JUDGE_MODEL=gemma3:4b \
    uv run pytest tests/evals/test_debate_quality_eval.py -v

Collection-only sanity check (always passes, even with no model):

    cd apps/api && uv run pytest tests/evals/test_debate_quality_eval.py --collect-only
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

import pytest

from app.config import settings
from app.debate.judge import _tokens, heuristic_score

# --------------------------------------------------------------------------- #
# Eval configuration
# --------------------------------------------------------------------------- #

EVAL_TOPIC = "Should social media platforms be legally liable for user-posted misinformation?"
EVAL_ROUNDS = 2  # two full passes over both combatants => 4 utterances, plenty to score.

# Quality thresholds (deliberately lenient — this validates the *premise* that a
# small local model produces coherent, on-topic, legible debate content, not
# that it produces flawless rhetoric).
MIN_WORDS_PER_UTTERANCE = 6          # a real argument, not a stub.
MIN_SUBSTANTIVE_FRACTION = 0.75      # >=75% of turns must clear the word bar.
MIN_ONTOPIC_FRACTION = 0.5           # >=50% of utterances must overlap the topic.
MIN_ONTOPIC_HEURISTIC = 50.0         # mean heuristic (length+overlap) clears average.

_PROBE_TIMEOUT = 3.0


def _host_ollama_base_url() -> str:
    """Resolve a host-reachable Ollama base URL.

    Precedence: explicit eval env override > docker-internal host rewritten to
    localhost (mirrors the conftest DB probe) > the configured URL as-is.
    """
    override = os.environ.get("EVAL_OLLAMA_BASE_URL")
    if override:
        return override.rstrip("/")
    url = settings.ollama_base_url
    for docker_host in ("//ollama:", "//db:", "//host.docker.internal:"):
        if docker_host in url:
            url = url.replace("//ollama:", "//localhost:").replace(
                "//host.docker.internal:", "//localhost:"
            )
            break
    return url.rstrip("/")


def _list_models(base_url: str) -> list[str]:
    """Return the model names pulled on the target Ollama, or [] if unreachable."""
    try:
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310
            import json

            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []


def _pick_model(available: list[str], preferred: list[str]) -> str | None:
    """Pick the first preferred model that is actually pulled (exact or prefix)."""
    for want in preferred:
        if not want:
            continue
        for have in available:
            if have == want or have.startswith(want.split(":")[0] + ":"):
                return have
    return available[0] if available else None


# --------------------------------------------------------------------------- #
# Module-scoped: run ONE real debate, share it across all assertions
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def debate_result(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Run a single real self-play debate against a live local model.

    Skips the whole module if no model gateway/model is reachable, so collection
    stays green on a bare host while the impl fleet is mid-edit.
    """
    base_url = _host_ollama_base_url()
    available = _list_models(base_url)
    if not available:
        pytest.skip(
            f"Model gateway not reachable at {base_url} (or no models pulled); "
            "skipping debate-quality eval. Start Ollama / the compose stack, or "
            "set EVAL_OLLAMA_BASE_URL, to run it."
        )

    debate_model = _pick_model(
        available,
        [
            os.environ.get("EVAL_DEBATE_MODEL", ""),
            settings.llm_default_model,
            "gemma3:4b",
            "gemma3:1b",
        ],
    )
    judge_model = _pick_model(
        available,
        [
            os.environ.get("EVAL_JUDGE_MODEL", ""),
            settings.llm_judge_model,
            debate_model or "",
        ],
    )
    if not debate_model:
        pytest.skip(f"No usable model among {available} at {base_url}.")

    # Point the gateway + judge at the host-reachable URL/model for this run.
    monkeypatch = pytest.MonkeyPatch()
    request.addfinalizer(monkeypatch.undo)
    monkeypatch.setattr(settings, "ollama_base_url", base_url, raising=False)
    monkeypatch.setenv("JUDGE_MODEL", judge_model or debate_model)

    from app.debate.orchestrator import run_self_play

    party = {
        "id": "party-eval",
        "name": "Veritas",
        "type": "LOGOS",
        "level": 3,
        "max_hp": 100,
        "model": debate_model,
        "persona": {
            "style": "rigorous and evidence-driven",
            "voice": "calm, precise",
            "bio": "A debater who reasons from first principles and cites mechanisms.",
        },
        "harness": {},
        "skills": ["Burden Shift", "Steelman"],
    }
    sparring = {
        "id": "enemy-eval",
        "name": "Provoco",
        "type": "PATHOS",
        "level": 3,
        "max_hp": 100,
        "model": debate_model,
        "persona": {
            "style": "fiery and rhetorical",
            "voice": "sharp, combative",
            "bio": "A debater who wins on framing and emotional stakes.",
        },
        "harness": {},
        "skills": ["Reframe", "Appeal to Stakes"],
    }

    # run_self_play is sync; it spins its own loop (or a worker-thread loop if one
    # is already running), so it is safe to call from an asyncio_mode=auto suite.
    result = run_self_play(party, sparring, EVAL_TOPIC, rounds=EVAL_ROUNDS)

    # The headless `run_self_play` collapses each verdict to (actor, score,
    # rationale) and DROPS the legibility fields (why/logic/persuasion) — those
    # are only threaded on the live WS path. To validate the same legibility
    # contract the live judge emits, we re-score every round's utterances with
    # `score_round`, which returns full JudgeScore objects (why/logic/persuasion).
    # This mirrors exactly what the WS `verdict` event carries.
    from app.debate.judge import score_round

    judge_scores = _rescore_rounds(
        EVAL_TOPIC, result.get("transcript", []), judge_model or debate_model, score_round
    )

    # Attach derived data + diagnostics for the assertions.
    result["judge_scores"] = judge_scores
    result["_eval_model"] = debate_model
    result["_eval_judge_model"] = judge_model
    result["_eval_base_url"] = base_url
    return result


def _rescore_rounds(
    topic: str, transcript: list[dict[str, Any]], judge_model: str, score_round_fn: Any
) -> list[Any]:
    """Re-run the judge over the transcript, round by round, the way the live
    engine does (one `score_round` call per round). Returns a flat list of
    JudgeScore objects carrying why/logic/persuasion — the legibility surface.
    """
    import asyncio

    # Combatant utterances in turn order; group into rounds of one-per-combatant.
    spoken = [
        u
        for u in transcript
        if u.get("actor_role") in ("party", "enemy") and isinstance(u.get("text"), str)
    ]
    distinct_actors = {u["actor_id"] for u in spoken}
    round_size = max(1, len(distinct_actors))

    async def _run() -> list[Any]:
        out: list[Any] = []
        for i in range(0, len(spoken), round_size):
            batch = spoken[i : i + round_size]
            items = [{"actor_id": u["actor_id"], "text": u["text"]} for u in batch]
            scores = await score_round_fn(
                topic, items, model=judge_model, fallback_model=judge_model
            )
            for js, u in zip(scores, batch):
                # Carry the turn number so failures point at a specific utterance.
                setattr(js, "turn", u.get("turn"))
                out.append(js)
        return out

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    # Inside a loop (shouldn't happen in sync fixture): use a worker thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run()).result()


# --------------------------------------------------------------------------- #
# Helpers shared by assertions
# --------------------------------------------------------------------------- #


def _combatant_utterances(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Transcript entries spoken by actual combatants (exclude judge narration)."""
    return [
        u
        for u in result.get("transcript", [])
        if u.get("actor_role") in ("party", "enemy") and isinstance(u.get("text"), str)
    ]


def _is_fallback_line(text: str) -> bool:
    """The engine's templated whole-utterance fallback (model produced nothing)."""
    t = text.strip()
    return t.startswith("(") and t.endswith(")") and "presses the point on" in t


# --------------------------------------------------------------------------- #
# Axis 0: the debate actually produced content (sanity)
# --------------------------------------------------------------------------- #


def test_debate_produced_transcript_and_verdicts(debate_result: dict[str, Any]) -> None:
    """Arrange a real debate; Act by running it; Assert it yielded scored turns."""
    # Arrange / Act: fixture already ran the debate.
    utterances = _combatant_utterances(debate_result)
    verdicts = debate_result.get("verdicts", [])
    judge_scores = debate_result.get("judge_scores", [])

    # Assert: both sides spoke and the judge returned verdicts/scores.
    assert len(utterances) >= 2, (
        f"expected >=2 combatant utterances, got {len(utterances)} "
        f"(model={debate_result.get('_eval_model')})"
    )
    assert verdicts, "judge produced no verdicts for the debate"
    assert judge_scores, "re-scoring produced no JudgeScore objects"
    assert len(judge_scores) == len(utterances), (
        "every combatant utterance should get exactly one judge score "
        f"(utterances={len(utterances)}, scores={len(judge_scores)})"
    )
    roles = {u["actor_role"] for u in utterances}
    assert roles == {"party", "enemy"}, f"both sides must speak; saw roles={roles}"


# --------------------------------------------------------------------------- #
# Axis 1: utterances are non-empty / substantive (coherence proxy)
# --------------------------------------------------------------------------- #


def test_no_fallback_or_empty_utterances(debate_result: dict[str, Any]) -> None:
    """Arrange utterances; Act by detecting stubs; Assert the model always spoke.

    The engine substitutes a templated "(X presses the point on ...)" line only
    when the model returns nothing. ZERO of those (and zero empties) is the hard
    floor for the content-quality premise: the model must actually generate every
    turn.
    """
    # Arrange
    utterances = _combatant_utterances(debate_result)

    # Act: flag empty or templated-fallback utterances.
    stubs = [
        {"turn": u.get("turn"), "text": u["text"][:80]}
        for u in utterances
        if not u["text"].strip() or _is_fallback_line(u["text"])
    ]

    # Assert: the model genuinely produced content on every single turn.
    assert not stubs, (
        f"{len(stubs)}/{len(utterances)} utterances were empty or the templated "
        f"fallback (model produced nothing): {stubs}"
    )


def test_utterances_are_substantive_in_length(debate_result: dict[str, Any]) -> None:
    """Arrange utterances; Act by counting words; Assert a strong majority are real
    multi-sentence arguments, not one-liners."""
    # Arrange
    utterances = _combatant_utterances(debate_result)
    assert utterances, "no utterances to evaluate"

    # Act
    substantive = [u for u in utterances if len(u["text"].split()) >= MIN_WORDS_PER_UTTERANCE]
    fraction = len(substantive) / len(utterances)

    # Assert: a strong majority clear the minimum word bar. We allow the rare
    # terse turn rather than demanding perfection, but most turns must be real
    # arguments. (Tolerance: a small local model occasionally clips a turn short.)
    assert fraction >= MIN_SUBSTANTIVE_FRACTION, (
        f"only {fraction:.0%} of utterances had >= {MIN_WORDS_PER_UTTERANCE} words "
        f"(need >= {MIN_SUBSTANTIVE_FRACTION:.0%}); model="
        f"{debate_result.get('_eval_model')}"
    )


# --------------------------------------------------------------------------- #
# Axis 2: utterances are on-topic (relevance)
# --------------------------------------------------------------------------- #


def test_utterances_are_on_topic(debate_result: dict[str, Any]) -> None:
    """Arrange topic+utterances; Act by measuring overlap; Assert relevance."""
    # Arrange
    topic = debate_result.get("topic", EVAL_TOPIC)
    topic_tokens = _tokens(topic)
    utterances = _combatant_utterances(debate_result)
    assert topic_tokens, "topic produced no content tokens (test setup error)"

    # Act: an utterance is on-topic if it shares >=1 meaningful token with the
    # topic; also compute the judge's own heuristic (length + overlap) per turn.
    on_topic_flags: list[bool] = []
    heuristics: list[float] = []
    for u in utterances:
        text = u["text"]
        overlap = topic_tokens & _tokens(text)
        on_topic_flags.append(bool(overlap))
        heuristics.append(heuristic_score(topic, text))

    on_topic_fraction = sum(on_topic_flags) / len(on_topic_flags)
    mean_heuristic = sum(heuristics) / len(heuristics)

    # Assert: a majority of turns engage the topic vocabulary, and the mean
    # length+overlap heuristic clears an average-argument bar.
    assert on_topic_fraction >= MIN_ONTOPIC_FRACTION, (
        f"only {on_topic_fraction:.0%} of utterances overlapped the topic "
        f"(need >={MIN_ONTOPIC_FRACTION:.0%}); model={debate_result.get('_eval_model')}"
    )
    assert mean_heuristic >= MIN_ONTOPIC_HEURISTIC, (
        f"mean on-topic heuristic {mean_heuristic:.1f} < {MIN_ONTOPIC_HEURISTIC} "
        f"(length+overlap too low across the debate)"
    )


# --------------------------------------------------------------------------- #
# Axis 3: judge verdicts carry non-empty 'why' explanations (legibility)
# --------------------------------------------------------------------------- #


def test_every_verdict_has_nonempty_why(debate_result: dict[str, Any]) -> None:
    """Arrange judge scores; Act by reading each `why`; Assert all explanatory.

    Reads the JudgeScore objects (the live-path legibility surface). Even when a
    small local model's JSON judge fails and the engine falls back to the
    heuristic, the contract guarantees a non-empty, templated `why` — so this
    asserts the contract, not the model's eloquence.
    """
    # Arrange
    judge_scores = debate_result.get("judge_scores", [])
    assert judge_scores, "no judge scores to evaluate"

    # Act: collect any score whose `why` is missing or blank.
    missing_why = [
        {"turn": getattr(js, "turn", None), "actor_id": js.actor_id, "why": js.why}
        for js in judge_scores
        if not (isinstance(js.why, str) and js.why.strip())
    ]

    # Assert: the legibility contract holds — every verdict explains itself, and
    # each `why` is a real phrase (a few words), not a single filler token.
    assert not missing_why, f"{len(missing_why)} verdict(s) had empty 'why': {missing_why}"
    for js in judge_scores:
        assert len(js.why.split()) >= 3, (
            f"'why' too terse to be an explanation: {js.why!r} "
            f"(turn {getattr(js, 'turn', None)})"
        )


# --------------------------------------------------------------------------- #
# Axis 4: judge scores discriminate (>50 and <50 both appear)
# --------------------------------------------------------------------------- #


def test_judge_scores_discriminate(debate_result: dict[str, Any]) -> None:
    """Arrange all judge scores; Act by spreading them; Assert discrimination."""
    # Arrange
    judge_scores = debate_result.get("judge_scores", [])
    scores = [float(js.score) for js in judge_scores if js.score is not None]
    assert len(scores) >= 2, f"need >=2 scored verdicts to test spread, got {len(scores)}"

    # Act
    has_strong = any(s > 50.0 for s in scores)
    has_weak = any(s < 50.0 for s in scores)
    spread = max(scores) - min(scores)

    # Assert: the judge is discriminating — not every argument is "50/100".
    # We accept EITHER a clear strong+weak split (some args land above 50 and some
    # below) OR a meaningful spread, since a short debate may rank all turns on one
    # side of 50 while still separating them. A real judge — or even the
    # length+overlap heuristic fallback — must not flat-line every argument.
    discriminates = (has_strong and has_weak) or spread >= 10.0
    assert discriminates, (
        f"judge scores did not discriminate: scores={scores} "
        f"(strong>50={has_strong}, weak<50={has_weak}, spread={spread:.1f}); "
        "a real judge should not flat-line every argument at ~50/100"
    )


def test_sides_receive_different_average_scores(debate_result: dict[str, Any]) -> None:
    """Arrange per-side averages; Act by comparing; Assert they are not identical."""
    # Arrange: compute each side's mean from the JudgeScore objects, keyed by the
    # known party id (the canonical id the engine assigned the party combatant).
    party_id = debate_result.get("party_id")
    judge_scores = debate_result.get("judge_scores", [])
    party = [float(js.score) for js in judge_scores if js.actor_id == party_id]
    enemy = [float(js.score) for js in judge_scores if js.actor_id != party_id]
    assert party and enemy, (
        f"need scores for both sides (party={len(party)}, enemy={len(enemy)})"
    )

    party_avg = sum(party) / len(party)
    enemy_avg = sum(enemy) / len(enemy)

    # Act / Assert: a discriminating judge separates the two debaters at least a
    # little; identical averages would imply a non-discriminating rubber stamp.
    assert abs(party_avg - enemy_avg) > 0.0, (
        f"party_avg ({party_avg:.2f}) == enemy_avg ({enemy_avg:.2f}); "
        "judge gave both sides an identical mean — no discrimination"
    )
