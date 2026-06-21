"""Dual-agent battle training loop.

This is a lightweight GEPA-style loop for the live battle harness. It does not
fine-tune model weights; it evolves the prompt genome for both the party and the
enemy, then keeps mutations that improve a latency-first reward.

The scored side is always passed to ``selfplay.play`` as the party debater
because that seam returns a 0..100 score for the party. For enemy training we
flip the stance: the optimized enemy is the scored party side, but argues
AGAINST while the sparring party argues FOR.
"""
from __future__ import annotations

import copy
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.party.persona import (
    BATTLE_RESPONSE_DIRECTIVES,
    ENEMY_SKILL_FRAGMENTS,
    PARTY_SKILL_FRAGMENTS,
)
from app.training import genome as genome_mod
from app.training import selfplay

DEFAULT_TOPIC = "Remote work makes teams more productive."
DEFAULT_MODEL = "pareto-actor"
DEFAULT_CYCLES = 1
DEFAULT_ROUNDS = 1
DEFAULT_VARIANTS = 2
DEFAULT_LATENCY_TARGET_SECONDS = 6.0

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "more",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "with",
}


@dataclass(frozen=True)
class LoopConfig:
    topic: str = DEFAULT_TOPIC
    cycles: int = DEFAULT_CYCLES
    rounds: int = DEFAULT_ROUNDS
    variants: int = DEFAULT_VARIANTS
    model: str = DEFAULT_MODEL
    seed: int = 7
    latency_target_s: float = DEFAULT_LATENCY_TARGET_SECONDS
    quality_floor: float = 0.25
    accept_margin: float = 0.005


@dataclass(frozen=True)
class CandidateEval:
    role: str
    op: str
    judge_score: float
    latency_s: float
    latency_score: float
    quality_score: float
    error_rate: float
    composite_score: float
    source: str
    transcript_excerpt: list[dict[str, Any]]


@dataclass(frozen=True)
class RoleUpdate:
    role: str
    baseline: CandidateEval
    selected: CandidateEval
    accepted: bool
    evaluated: list[CandidateEval]


@dataclass(frozen=True)
class CycleResult:
    cycle: int
    party: RoleUpdate
    enemy: RoleUpdate


@dataclass(frozen=True)
class BattleTrainingResult:
    config: LoopConfig
    party_initial: CandidateEval
    party_final: CandidateEval
    enemy_initial: CandidateEval
    enemy_final: CandidateEval
    cycles: list[CycleResult]
    party_genome: dict[str, Any]
    enemy_genome: dict[str, Any]


def default_party_genome() -> dict[str, Any]:
    """A small starter genome for CLI/local loops that do not load DB monsters."""
    return _with_battle_directives(
        {
            "harness": {
                "system_prompt": (
                    "Thin party battle harness: obey role, stance, and output contract."
                ),
                "directives": [],
            },
            "persona": {
                "name": "Party Vanguard",
                "type": "LOGOS",
                "tone": "calm and methodical",
            },
            "skill_prompt_fragments": [
                "Prefer a short rebuttal that turns the opponent's wording against them.",
            ],
            "gambit_rules": [],
            "skills": [],
        },
        role="party",
    )


def default_enemy_genome() -> dict[str, Any]:
    """A small starter genome for the opposing battle agent."""
    return _with_battle_directives(
        {
            "harness": {
                "system_prompt": (
                    "Thin enemy battle harness: obey role, stance, and output contract."
                ),
                "directives": [],
            },
            "persona": {
                "name": "Enemy Vanguard",
                "type": "PATHOS",
                "tone": "incisive and confident",
            },
            "skill_prompt_fragments": [
                "Force the opponent to defend the practical cost of their claim.",
            ],
            "gambit_rules": [],
            "skills": [],
        },
        role="enemy",
    )


async def run_battle_training(
    config: LoopConfig | None = None,
    *,
    party_genome: dict[str, Any] | None = None,
    enemy_genome: dict[str, Any] | None = None,
) -> BattleTrainingResult:
    """Mutate and evaluate party/enemy genomes in alternating battle loops."""
    cfg = config or LoopConfig()
    rng = random.Random(cfg.seed)
    party = _with_battle_directives(party_genome or default_party_genome(), role="party")
    enemy = _with_battle_directives(enemy_genome or default_enemy_genome(), role="enemy")

    cycles: list[CycleResult] = []
    party_initial: CandidateEval | None = None
    enemy_initial: CandidateEval | None = None
    party_best_eval: CandidateEval | None = None
    enemy_best_eval: CandidateEval | None = None

    for cycle_idx in range(max(1, int(cfg.cycles))):
        party, party_update = await _train_role(
            role="party",
            incumbent=party,
            opponent=enemy,
            cfg=cfg,
            rng=rng,
            accept_floor=party_best_eval,
        )
        if party_initial is None:
            party_initial = party_update.baseline
        if (
            party_best_eval is None
            or party_update.selected.composite_score > party_best_eval.composite_score
        ):
            party_best_eval = party_update.selected

        enemy, enemy_update = await _train_role(
            role="enemy",
            incumbent=enemy,
            opponent=party,
            cfg=cfg,
            rng=rng,
            accept_floor=enemy_best_eval,
        )
        if enemy_initial is None:
            enemy_initial = enemy_update.baseline
        if (
            enemy_best_eval is None
            or enemy_update.selected.composite_score > enemy_best_eval.composite_score
        ):
            enemy_best_eval = enemy_update.selected

        cycles.append(CycleResult(cycle=cycle_idx + 1, party=party_update, enemy=enemy_update))

    assert party_initial is not None
    assert enemy_initial is not None
    assert party_best_eval is not None
    assert enemy_best_eval is not None
    return BattleTrainingResult(
        config=cfg,
        party_initial=party_initial,
        party_final=party_best_eval,
        enemy_initial=enemy_initial,
        enemy_final=enemy_best_eval,
        cycles=cycles,
        party_genome=party,
        enemy_genome=enemy,
    )


async def _train_role(
    *,
    role: str,
    incumbent: dict[str, Any],
    opponent: dict[str, Any],
    cfg: LoopConfig,
    rng: random.Random,
    accept_floor: CandidateEval | None = None,
) -> tuple[dict[str, Any], RoleUpdate]:
    baseline = await _score_role(
        incumbent,
        opponent,
        role=role,
        op="incumbent",
        cfg=cfg,
    )
    best_genome = incumbent
    best_eval = baseline
    evaluated: list[CandidateEval] = []

    for variant, op in genome_mod.sample_mutations(
        incumbent,
        max(0, int(cfg.variants)),
        rng=rng,
    ):
        candidate = _with_battle_directives(variant, role=role)
        ev = await _score_role(candidate, opponent, role=role, op=op, cfg=cfg)
        evaluated.append(ev)
        threshold = best_eval
        if accept_floor and accept_floor.composite_score > threshold.composite_score:
            threshold = accept_floor
        if _candidate_beats(ev, threshold, cfg):
            best_genome = candidate
            best_eval = ev

    accepted = best_eval.op != "incumbent"
    return best_genome, RoleUpdate(
        role=role,
        baseline=baseline,
        selected=best_eval,
        accepted=accepted,
        evaluated=evaluated,
    )


async def _score_role(
    candidate: dict[str, Any],
    opponent: dict[str, Any],
    *,
    role: str,
    op: str,
    cfg: LoopConfig,
) -> CandidateEval:
    t0 = time.perf_counter()
    try:
        result = await selfplay.play(
            candidate,
            topic=cfg.topic,
            rounds=cfg.rounds,
            sparring_genome=opponent,
            model=cfg.model,
            party_id=role,
            enemy_id="enemy" if role == "party" else "party",
            party_stance=_stance_for(role, cfg.topic),
            enemy_stance=_stance_for("enemy" if role == "party" else "party", cfg.topic),
        )
    except Exception as e:  # noqa: BLE001
        result = {
            "score": 50.0,
            "source": "error",
            "transcript": [
                {
                    "actor_id": role,
                    "actor_role": "party",
                    "text": f"(no response: {type(e).__name__})",
                }
            ],
        }
    latency_s = max(0.0, time.perf_counter() - t0)

    transcript = list(result.get("transcript") or [])
    judge_score = _clamp(float(result.get("score", 50.0)), 0.0, 100.0)
    error_rate = _error_rate(transcript, role)
    quality_score = _quality_score(transcript, cfg.topic, role, error_rate)
    latency_score = _latency_score(latency_s, cfg.latency_target_s)
    composite = _composite_score(judge_score, latency_score, quality_score, error_rate)

    return CandidateEval(
        role=role,
        op=op,
        judge_score=judge_score,
        latency_s=latency_s,
        latency_score=latency_score,
        quality_score=quality_score,
        error_rate=error_rate,
        composite_score=composite,
        source=str(result.get("source", "unknown")),
        transcript_excerpt=_transcript_excerpt(transcript),
    )


def _candidate_beats(candidate: CandidateEval, incumbent: CandidateEval, cfg: LoopConfig) -> bool:
    if candidate.error_rate >= 0.5:
        return False
    if candidate.quality_score < cfg.quality_floor:
        return False
    return candidate.composite_score > incumbent.composite_score + cfg.accept_margin


def _with_battle_directives(genome: dict[str, Any], *, role: str = "party") -> dict[str, Any]:
    g = copy.deepcopy(genome)
    harness = g.setdefault("harness", {})
    directives = harness.get("directives")
    if not isinstance(directives, list):
        directives = list(directives) if directives else []
        harness["directives"] = directives
    for directive in BATTLE_RESPONSE_DIRECTIVES:
        if directive not in directives:
            directives.append(directive)
    fragments = g.setdefault("skill_prompt_fragments", [])
    if not isinstance(fragments, list):
        fragments = list(fragments) if fragments else []
        g["skill_prompt_fragments"] = fragments
    role_fragments = ENEMY_SKILL_FRAGMENTS if role == "enemy" else PARTY_SKILL_FRAGMENTS
    for fragment in role_fragments:
        if fragment not in fragments:
            fragments.append(fragment)
    return g


def _stance_for(role: str, topic: str) -> str:
    if role == "enemy":
        return f"Argue AGAINST the proposition: {topic}."
    return f"Argue FOR the proposition: {topic}."


def _quality_score(
    transcript: list[dict[str, Any]],
    topic: str,
    role: str,
    error_rate: float,
) -> float:
    texts = _texts_for(transcript, role)
    if not texts:
        return 0.0

    topic_tokens = set(_tokens(topic))
    text_tokens = set(_tokens(" ".join(texts)))
    topic_overlap = len(topic_tokens & text_tokens) / max(1, len(topic_tokens))

    opponent_tokens = set(_tokens(" ".join(_opponent_texts(transcript, role)[-2:])))
    opponent_overlap = (
        len(opponent_tokens & text_tokens) / max(1, min(len(opponent_tokens), 12))
        if opponent_tokens
        else 0.5
    )

    joined = " ".join(texts).lower()
    connective = 1.0 if re.search(r"\b(because|however|but|therefore|while|claim)\b", joined) else 0.35
    avg_words = sum(len(_tokens(t, keep_stopwords=True)) for t in texts) / max(1, len(texts))
    if 12 <= avg_words <= 85:
        concise = 1.0
    elif 6 <= avg_words <= 120:
        concise = 0.65
    else:
        concise = 0.25
    clean_format = _format_score(texts)
    reliability = max(0.0, 1.0 - error_rate)

    return _clamp(
        (0.25 * topic_overlap)
        + (0.20 * opponent_overlap)
        + (0.15 * connective)
        + (0.15 * concise)
        + (0.15 * clean_format)
        + (0.10 * reliability),
        0.0,
        1.0,
    )


def _format_score(texts: list[str]) -> float:
    """Reward plain debate prose over markdown/scaffolded model output."""
    joined = "\n".join(texts)
    penalties = 0
    if re.search(r"[*_`#]|^\s*[-*•]\s", joined, flags=re.MULTILINE):
        penalties += 1
    if re.search(
        r"^\s*(claim|support|evidence|rebuttal|argument)\s*:",
        joined,
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        penalties += 1
    if len(re.findall(r"[.!?]", joined)) > 3:
        penalties += 1
    return max(0.0, 1.0 - penalties * 0.3)


def _texts_for(transcript: list[dict[str, Any]], role: str) -> list[str]:
    texts = [
        str(u.get("text", "")).strip()
        for u in transcript
        if _is_argument_utterance(u) and u.get("actor_id") == role
    ]
    if texts:
        return texts
    # Older tests and self-play stubs often omit actor_id. The scored side is
    # represented as actor_role="party" by convention.
    return [
        str(u.get("text", "")).strip()
        for u in transcript
        if _is_argument_utterance(u) and u.get("actor_role") == "party"
    ]


def _opponent_texts(transcript: list[dict[str, Any]], role: str) -> list[str]:
    return [
        str(u.get("text", "")).strip()
        for u in transcript
        if _is_argument_utterance(u) and u.get("actor_id") not in {role, "judge"}
    ]


def _is_argument_utterance(u: dict[str, Any]) -> bool:
    return u.get("actor_role") != "judge" and not u.get("reaction_state")


def _error_rate(transcript: list[dict[str, Any]], role: str) -> float:
    texts = _texts_for(transcript, role)
    if not texts:
        return 1.0
    failures = sum(
        1
        for text in texts
        if "(no response" in text.lower()
        or "traceback" in text.lower()
        or "rate limit" in text.lower()
    )
    return failures / len(texts)


def _latency_score(latency_s: float, target_s: float) -> float:
    target = max(0.25, target_s)
    return _clamp(1.0 / (1.0 + (latency_s / target)), 0.0, 1.0)


def _composite_score(
    judge_score: float,
    latency_score: float,
    quality_score: float,
    error_rate: float,
) -> float:
    reliability = max(0.0, 1.0 - error_rate)
    return _clamp(
        (0.50 * latency_score)
        + (0.30 * (judge_score / 100.0))
        + (0.15 * quality_score)
        + (0.05 * reliability),
        0.0,
        1.0,
    )


def _tokens(text: str, *, keep_stopwords: bool = False) -> list[str]:
    tokens = [t for t in re.findall(r"[a-z0-9']+", text.lower()) if len(t) > 1]
    if keep_stopwords:
        return tokens
    return [t for t in tokens if t not in _STOPWORDS]


def _transcript_excerpt(transcript: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for u in transcript[:limit]:
        text = str(u.get("text", "")).strip()
        out.append(
            {
                "turn": u.get("turn"),
                "actor_id": u.get("actor_id"),
                "actor_role": u.get("actor_role"),
                "text": text[:240],
            }
        )
    return out


def result_to_dict(
    result: BattleTrainingResult,
    *,
    include_transcripts: bool = True,
    include_genomes: bool = True,
) -> dict[str, Any]:
    """JSON-friendly representation for the CLI and future API endpoints."""
    data = {
        "config": asdict(result.config),
        "party": {
            "initial": _eval_to_dict(result.party_initial, include_transcripts),
            "final": _eval_to_dict(result.party_final, include_transcripts),
            "composite_delta": result.party_final.composite_score
            - result.party_initial.composite_score,
            "judge_delta": result.party_final.judge_score - result.party_initial.judge_score,
        },
        "enemy": {
            "initial": _eval_to_dict(result.enemy_initial, include_transcripts),
            "final": _eval_to_dict(result.enemy_final, include_transcripts),
            "composite_delta": result.enemy_final.composite_score
            - result.enemy_initial.composite_score,
            "judge_delta": result.enemy_final.judge_score - result.enemy_initial.judge_score,
        },
        "cycles": [
            {
                "cycle": cycle.cycle,
                "party": _role_update_to_dict(cycle.party, include_transcripts),
                "enemy": _role_update_to_dict(cycle.enemy, include_transcripts),
            }
            for cycle in result.cycles
        ],
    }
    if include_genomes:
        data["final_genomes"] = {
            "party": result.party_genome,
            "enemy": result.enemy_genome,
        }
    return data


def _role_update_to_dict(update: RoleUpdate, include_transcripts: bool) -> dict[str, Any]:
    return {
        "role": update.role,
        "accepted": update.accepted,
        "selected_op": update.selected.op,
        "baseline": _eval_to_dict(update.baseline, include_transcripts),
        "selected": _eval_to_dict(update.selected, include_transcripts),
        "evaluated": [
            _eval_to_dict(candidate, include_transcripts) for candidate in update.evaluated
        ],
    }


def _eval_to_dict(candidate: CandidateEval, include_transcripts: bool) -> dict[str, Any]:
    data = asdict(candidate)
    if not include_transcripts:
        data.pop("transcript_excerpt", None)
    return data


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
