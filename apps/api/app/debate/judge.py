"""Judge agent — scores debate utterances against the topic.

Quality-critical and latency-sensitive on a local model, so:
  * one short rubric + a one-shot JSON example, JSON-only output (json_mode)
  * parse with json_repair to survive malformed JSON
  * if parsing still fails, fall back to a deterministic heuristic score
    (argument length + keyword overlap with the topic)

Public surface:
    await score_round(topic, items, model="judge") -> list[JudgeScore]
    heuristic_score(topic, text) -> float
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from app.gateway.gateway import gateway

try:  # json_repair is in the venv; degrade gracefully if not
    from json_repair import repair_json
except Exception:  # noqa: BLE001
    repair_json = None  # type: ignore[assignment]


# The "judge" alias maps to settings.llm_judge_model (gemma3:4b), which may not
# be pulled in every environment. Allow an env override; orchestrator also passes
# a fallback_model (the combatants' own model) so judging degrades to a present
# model before resorting to the heuristic.
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "judge")

_RUBRIC = (
    "You are a strict but fair debate judge. Score each argument 0-100 on how "
    "persuasive, relevant to the topic, and well-reasoned it is. 50 is an average "
    "argument; 80+ is excellent; below 40 is weak or off-topic. Be discriminating."
)

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "to",
    "of", "in", "on", "for", "that", "this", "it", "as", "with", "we", "you",
    "i", "they", "should", "would", "could", "can", "will", "not", "do", "does",
}


@dataclass
class JudgeScore:
    actor_id: str
    score: float
    rationale: str


def _tokens(text: str) -> set[str]:
    out = set()
    for raw in text.lower().split():
        w = "".join(c for c in raw if c.isalnum())
        if w and w not in _STOP_WORDS and len(w) > 2:
            out.add(w)
    return out


def heuristic_score(topic: str, text: str) -> float:
    """Deterministic fallback: blend length signal + topic keyword overlap."""
    if not text or not text.strip():
        return 20.0
    words = text.split()
    # length signal: rewards substance up to ~60 words, saturates.
    length_sig = min(len(words) / 60.0, 1.0)
    topic_tokens = _tokens(topic)
    text_tokens = _tokens(text)
    if topic_tokens:
        overlap = len(topic_tokens & text_tokens) / len(topic_tokens)
    else:
        overlap = 0.0
    # 35..85 range so an on-topic, substantial argument lands above 50.
    raw = 35.0 + 30.0 * length_sig + 25.0 * overlap
    return round(min(raw, 92.0), 1)


def _coerce_score(val: Any) -> float | None:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, f))


def _parse_json(raw: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass
    if repair_json is not None:
        try:
            repaired = repair_json(raw)
            return json.loads(repaired) if isinstance(repaired, str) else repaired
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_messages(topic: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
    lines = []
    for it in items:
        lines.append(f'- id "{it["actor_id"]}": {it["text"]}')
    args_block = "\n".join(lines)
    ids = ", ".join(f'"{it["actor_id"]}"' for it in items)
    user = (
        f"Topic of debate: {topic}\n\n"
        f"Arguments this round:\n{args_block}\n\n"
        f"Score EACH argument. Use these EXACT ids as the JSON keys: {ids}.\n"
        "Return ONLY a JSON object mapping each id to "
        '{"score": <0-100>, "rationale": "<one short sentence>"}.\n'
        "Do not invent ids; copy the ids given above verbatim."
    )
    return [
        {"role": "system", "content": _RUBRIC},
        {"role": "user", "content": user},
    ]


async def _judge_call(topic: str, items: list[dict[str, Any]], model: str) -> Any:
    raw = await gateway.complete(
        _build_messages(topic, items),
        model=model,
        temperature=0.2,
        max_tokens=400,
        json_mode=True,
    )
    return _parse_json(raw)


async def score_round(
    topic: str,
    items: list[dict[str, Any]],
    model: str | None = None,
    fallback_model: str | None = None,
) -> list[JudgeScore]:
    """Score a whole round at once (one judge call per round for latency).

    `items` is a list of {"actor_id": str, "text": str}. Returns one JudgeScore
    per item, in input order. Always returns a score for every item.

    Resilience ladder: primary judge model -> `fallback_model` (e.g. the
    combatants' own model, which is guaranteed present) -> deterministic
    heuristic. A usable result is a dict that scores at least one item.
    """
    if not items:
        return []

    candidates = [model or JUDGE_MODEL]
    if fallback_model and fallback_model not in candidates:
        candidates.append(fallback_model)

    parsed: Any = None
    for cand in candidates:
        try:
            res = await _judge_call(topic, items, cand)
        except Exception:  # noqa: BLE001 — network/model failure -> next candidate
            continue
        if isinstance(res, dict) and res:
            parsed = res
            break

    # Positional fallback: small models sometimes echo example keys instead of
    # the real actor ids. If the keys don't match but the entry count lines up,
    # map results by order.
    positional: list[Any] | None = None
    if isinstance(parsed, dict):
        matched = any(it["actor_id"] in parsed for it in items)
        if not matched and len(parsed) == len(items):
            positional = list(parsed.values())

    results: list[JudgeScore] = []
    for idx, it in enumerate(items):
        aid = it["actor_id"]
        text = it["text"]
        score: float | None = None
        rationale = ""
        entry: Any = None
        if isinstance(parsed, dict):
            entry = parsed.get(aid)
            if entry is None and positional is not None:
                entry = positional[idx]
        if isinstance(entry, dict):
            score = _coerce_score(entry.get("score"))
            rationale = str(entry.get("rationale", "")).strip()
        elif entry is not None:
            score = _coerce_score(entry)
        if score is None:
            score = heuristic_score(topic, text)
            if not rationale:
                rationale = "Heuristic score (judge output unavailable)."
        results.append(JudgeScore(actor_id=aid, score=score, rationale=rationale or "Scored."))
    return results
