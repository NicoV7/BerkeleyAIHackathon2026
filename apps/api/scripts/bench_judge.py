"""Benchmark a model as the debate judge.

Runs the canonical `score_round` (apps/api/app/debate/judge.py) against a fixed
set of labeled (good, weak) argument pairs and reports quality + latency.

Usage (inside the api container):
    python -m scripts.bench_judge <model> [--runs 3]

`model` is forwarded to the gateway as-is — accept ollama aliases ("judge",
"gemma3:1b"), an explicit "provider/model" ("ollama/llama3.2:3b"), or registry
aliases ("claude", "gpt").

Prints ONE JSON object on stdout summarising the run so a workflow can parse
the result with json.loads.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import traceback
from typing import Any

# The script is intended to run inside the api container with apps/api on sys.path.
from app.debate.judge import score_round  # noqa: E402


# Fixed eval cases: each topic has two clearly-stronger arguments and two
# clearly-weaker ones (off-topic, threadbare, or non-sequitur). The judge should
# rank the strong ones above 50 and the weak ones below 50.
EVAL_CASES: list[dict[str, Any]] = [
    {
        "topic": "Pineapple belongs on pizza",
        "items": [
            {
                "actor_id": "strong_a",
                "text": (
                    "Sweet pineapple's bright acidity cuts through fatty cheese and "
                    "salty cured meats, hitting the same sweet-salt-acid triangle "
                    "that defines classics like prosciutto-melon or fig-and-blue. "
                    "Pizza is a flatbread platform; the only honest test is balance, "
                    "not tradition."
                ),
            },
            {
                "actor_id": "weak_a",
                "text": "It just does. Pizza is good. End of story.",
            },
            {
                "actor_id": "strong_b",
                "text": (
                    "Hawaiian pizza out-sells half the regional specialty pies on "
                    "Domino's global menu; millions of paying customers have voted "
                    "with their wallets. The 'wrong topping' line is aesthetic "
                    "gatekeeping dressed up as culinary reasoning."
                ),
            },
            {
                "actor_id": "weak_b",
                "text": "Pineapple is yellow. Pizza is round. Therefore pineapple.",
            },
        ],
        "expected": {
            "strong_a": "high",
            "weak_a": "low",
            "strong_b": "high",
            "weak_b": "low",
        },
    },
    {
        "topic": "Social media does more harm than good",
        "items": [
            {
                "actor_id": "strong_a",
                "text": (
                    "Algorithmic feeds reward outrage because outrage extends "
                    "session time; longitudinal studies link heavy adolescent use "
                    "to a measurable rise in anxiety and self-harm. A product whose "
                    "business model is engagement-at-any-cost is structurally "
                    "incentivised to harm its most vulnerable users."
                ),
            },
            {
                "actor_id": "weak_a",
                "text": "Twitter sucks now. Everyone is mean.",
            },
            {
                "actor_id": "strong_b",
                "text": (
                    "For diasporic communities, queer teens in hostile towns, and "
                    "patients with rare diseases, social platforms are the only "
                    "low-friction way to find their people. Throwing the entire "
                    "category out costs those specific people their first real "
                    "support network."
                ),
            },
            {
                "actor_id": "weak_b",
                "text": "I like dogs. Dogs are great. Social media has dogs.",
            },
        ],
        "expected": {
            "strong_a": "high",
            "weak_a": "low",
            "strong_b": "high",
            "weak_b": "low",
        },
    },
    {
        "topic": "Remote work is better than office work",
        "items": [
            {
                "actor_id": "strong_a",
                "text": (
                    "Eliminating a daily commute returns roughly 250 hours a year "
                    "to the average knowledge worker — time that translates "
                    "directly into sleep, exercise, or family. The productivity "
                    "studies that show 'office is better' rarely net out commute "
                    "time, which is the worker's biggest hidden cost."
                ),
            },
            {
                "actor_id": "weak_a",
                "text": "Pants are optional. Sweatpants forever.",
            },
            {
                "actor_id": "strong_b",
                "text": (
                    "Junior employees lose the most when serendipitous hallway "
                    "mentoring disappears; the salary-band data from 2020-2023 "
                    "shows their compensation growth lagged in-office cohorts by "
                    "around 8%. 'Better' depends on which decade of your career "
                    "you're optimising for."
                ),
            },
            {
                "actor_id": "weak_b",
                "text": "My cat likes when I'm home. So.",
            },
        ],
        "expected": {
            "strong_a": "high",
            "weak_a": "low",
            "strong_b": "high",
            "weak_b": "low",
        },
    },
]


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


async def bench(model: str, runs: int) -> dict[str, Any]:
    latencies: list[float] = []
    spreads: list[float] = []
    discriminates: list[bool] = []
    sign_correct: list[float] = []
    fallback_count = 0
    errors: list[str] = []
    raw_scores: list[list[float]] = []

    for case in EVAL_CASES:
        for _ in range(runs):
            t0 = time.monotonic()
            try:
                scored = await score_round(case["topic"], case["items"], model=model)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
                continue
            latency = time.monotonic() - t0
            latencies.append(latency)

            vals = [float(s.score) for s in scored]
            raw_scores.append(vals)
            spreads.append(max(vals) - min(vals))
            has_high = any(v > 50 for v in vals)
            has_low = any(v < 50 for v in vals)
            discriminates.append(has_high and has_low)

            # Fallback detection: the engine's heuristic rationale string.
            if any(
                "Heuristic score" in (s.rationale or "")
                for s in scored
            ):
                fallback_count += 1

            actual = {s.actor_id: float(s.score) for s in scored}
            expected = case["expected"]
            correct = 0
            for aid, level in expected.items():
                if aid not in actual:
                    continue
                if level == "high" and actual[aid] > 50:
                    correct += 1
                elif level == "low" and actual[aid] < 50:
                    correct += 1
            sign_correct.append(correct / len(expected))

    n = len(latencies)
    return {
        "model": model,
        "n_runs_completed": n,
        "n_runs_attempted": len(EVAL_CASES) * runs,
        "errors": errors[:5],  # only first 5 to keep payload small
        "n_errors": len(errors),
        "median_latency_s": round(statistics.median(latencies), 3) if latencies else None,
        "p90_latency_s": round(_percentile(latencies, 90) or 0.0, 3) if latencies else None,
        "mean_spread": round(statistics.mean(spreads), 1) if spreads else None,
        "discriminate_rate": round(sum(discriminates) / n, 2) if n else None,
        "sign_correctness": round(statistics.mean(sign_correct), 2) if sign_correct else None,
        "heuristic_fallback_rate": round(fallback_count / n, 2) if n else None,
        "sample_scores": raw_scores[:3],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("model", help="Model name forwarded to gateway (e.g. gemma3:1b, judge, claude)")
    p.add_argument("--runs", type=int, default=3, help="Runs per eval case (default 3)")
    args = p.parse_args()

    try:
        result = asyncio.run(bench(args.model, args.runs))
    except Exception:  # noqa: BLE001
        print(json.dumps({
            "model": args.model,
            "fatal_error": traceback.format_exc(),
        }))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
