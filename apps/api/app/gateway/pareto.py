"""Latency-first Pareto model selection for gateway fallbacks.

The selector is intentionally in-process and rebuildable: live benchmark results
guide routing when present, while the configured candidate order remains the
startup-safe default. No provider keys are ever returned from this module.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.config import settings
from app.gateway.models import resolve

ACTOR_ROLE = "actor"
JUDGE_ROLE = "judge"
QUALITY_FLOOR = 0.55
JSON_FLOOR = 0.80

_LOCAL_FALLBACK = "ollama/gemma3:1b"
_BENCH_CACHE: dict[str, list["BenchResult"]] = {ACTOR_ROLE: [], JUDGE_ROLE: []}


class CompleteClient(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str: ...


@dataclass
class BenchResult:
    role: str
    model: str
    provider: str
    runs_attempted: int
    runs_completed: int
    median_latency_s: float | None
    p90_latency_s: float | None
    quality_score: float
    json_compliance: float
    error_rate: float
    free_tier_preference: float

    @property
    def acceptable(self) -> bool:
        if self.runs_completed <= 0 or self.median_latency_s is None:
            return False
        json_ok = self.json_compliance >= (JSON_FLOOR if self.role == JUDGE_ROLE else 0.0)
        return self.quality_score >= QUALITY_FLOOR and json_ok and self.error_rate < 1.0


def candidate_models(role: str) -> list[str]:
    """Configured candidate list for a role, with the local fallback guaranteed."""
    raw = (
        settings.gateway_judge_candidates
        if role == JUDGE_ROLE
        else settings.gateway_actor_candidates
    )
    models = [m.strip() for m in raw.split(",") if m.strip()]
    if _LOCAL_FALLBACK not in models:
        models.append(_LOCAL_FALLBACK)
    return _dedupe(models)


def fallback_order(role: str, *, json_mode: bool = False) -> list[str]:
    """Return model ids in the order the gateway should attempt them."""
    if not settings.gateway_fallback_enabled:
        return candidate_models(role)[:1]
    cached = _BENCH_CACHE.get(role) or []
    configured = [m for m in candidate_models(role) if provider_configured(m)]
    if not cached:
        return configured
    frontier = _frontier(cached, require_json=json_mode or role == JUDGE_ROLE)
    ranked = sorted(frontier, key=lambda r: (r.median_latency_s or 9999, -r.quality_score))
    rest = [m for m in configured if m not in {r.model for r in ranked}]
    return _dedupe([r.model for r in ranked] + rest)


def selected_model(role: str, *, json_mode: bool = False) -> str:
    """Current first-choice model for a role."""
    order = fallback_order(role, json_mode=json_mode)
    return order[0] if order else _LOCAL_FALLBACK


def clear_cache() -> None:
    """Clear in-process benchmark results; useful for tests and fresh benches."""
    _BENCH_CACHE[ACTOR_ROLE] = []
    _BENCH_CACHE[JUDGE_ROLE] = []


def provider_configured(model: str) -> bool:
    """Whether a model's provider can be called without exposing its secret."""
    provider = resolve(model).provider
    if provider == "ollama":
        return True
    return bool(_key_for_provider(provider))


def redacted_status() -> dict[str, Any]:
    """Provider/model routing status safe to return from an API endpoint."""
    return {
        "fallback_enabled": settings.gateway_fallback_enabled,
        "providers": {
            p: {"configured": bool(_key_for_provider(p))}
            for p in ("groq", "cerebras", "gemini", "openrouter", "anthropic", "openai")
        },
        "roles": {
            role: {
                "candidates": candidate_models(role),
                "selected": selected_model(role, json_mode=role == JUDGE_ROLE),
                "fallback_order": fallback_order(role, json_mode=role == JUDGE_ROLE),
                "frontier": [asdict(r) for r in _frontier(_BENCH_CACHE.get(role, []))],
            }
            for role in (ACTOR_ROLE, JUDGE_ROLE)
        },
    }


async def benchmark_role(client: CompleteClient, role: str, runs: int = 3) -> dict[str, Any]:
    """Benchmark configured candidates and cache the resulting Pareto frontier."""
    results: list[BenchResult] = []
    for model in candidate_models(role):
        if not provider_configured(model):
            results.append(_skipped_result(role, model, runs))
            continue
        results.append(await benchmark_model(client, role, model, runs=runs))
    _BENCH_CACHE[role] = results
    frontier = _frontier(results, require_json=role == JUDGE_ROLE)
    return {
        "role": role,
        "runs": max(1, int(runs)),
        "selected": selected_model(role, json_mode=role == JUDGE_ROLE),
        "fallback_order": fallback_order(role, json_mode=role == JUDGE_ROLE),
        "frontier": [asdict(r) for r in frontier],
        "results": [asdict(r) for r in results],
    }


async def benchmark_model(
    client: CompleteClient, role: str, model: str, runs: int = 3
) -> BenchResult:
    """Run a small live benchmark for one model without raising on failures."""
    latencies: list[float] = []
    quality: list[float] = []
    json_ok = 0
    attempts = max(1, int(runs))
    for index in range(attempts):
        started = time.monotonic()
        try:
            if role == JUDGE_ROLE:
                text = await client.complete(
                    _judge_messages(), model=model, temperature=0.1,
                    max_tokens=120, json_mode=True, timeout=_bench_timeout(),
                )
                ok, score = _judge_quality(text)
                json_ok += 1 if ok else 0
            else:
                text = await client.complete(
                    _actor_messages(index), model=model, temperature=0.6,
                    max_tokens=80, timeout=_bench_timeout(),
                )
                score = _actor_quality(text)
            latencies.append(time.monotonic() - started)
            quality.append(score)
        except Exception:  # noqa: BLE001
            quality.append(0.0)
    return BenchResult(
        role=role,
        model=model,
        provider=resolve(model).provider,
        runs_attempted=attempts,
        runs_completed=len(latencies),
        median_latency_s=_median(latencies),
        p90_latency_s=_percentile(latencies, 90),
        quality_score=round(statistics.mean(quality), 3) if quality else 0.0,
        json_compliance=round(json_ok / attempts, 3) if role == JUDGE_ROLE else 1.0,
        error_rate=round((attempts - len(latencies)) / attempts, 3),
        free_tier_preference=_free_tier_preference(model),
    )


def _frontier(results: list[BenchResult], require_json: bool = False) -> list[BenchResult]:
    acceptable = [r for r in results if r.acceptable]
    if require_json:
        acceptable = [r for r in acceptable if r.json_compliance >= JSON_FLOOR]
    return [r for r in acceptable if not any(_dominates(other, r) for other in acceptable)]


def _dominates(left: BenchResult, right: BenchResult) -> bool:
    if left.model == right.model or left.median_latency_s is None or right.median_latency_s is None:
        return False
    dims = (
        left.median_latency_s <= right.median_latency_s,
        left.quality_score >= right.quality_score,
        left.json_compliance >= right.json_compliance,
        left.error_rate <= right.error_rate,
        left.free_tier_preference >= right.free_tier_preference,
    )
    strict = (
        left.median_latency_s < right.median_latency_s
        or left.quality_score > right.quality_score
        or left.json_compliance > right.json_compliance
        or left.error_rate < right.error_rate
        or left.free_tier_preference > right.free_tier_preference
    )
    return all(dims) and strict


def _actor_messages(index: int) -> list[dict[str, str]]:
    topic = "Remote work is better than office work"
    return [
        {"role": "system", "content": "You are a concise debate agent."},
        {
            "role": "user",
            "content": (
                f"Topic: {topic}\nArgue FOR it in 1-2 concrete sentences. "
                f"Run {index}; no preamble."
            ),
        },
    ]


def _judge_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a JSON-only debate judge."},
        {
            "role": "user",
            "content": (
                'Topic: Remote work is better than office work\n'
                'Argument id "a": Remote work saves commute time and gives workers '
                'more control over focused work.\n'
                'Return {"a":{"score":0-100,"rationale":"short"}} only.'
            ),
        },
    ]


def _actor_quality(text: str) -> float:
    try:
        from app.debate.judge import heuristic_score

        return round(heuristic_score("Remote work is better than office work", text) / 100.0, 3)
    except Exception:  # noqa: BLE001
        return 0.0


def _judge_quality(text: str) -> tuple[bool, float]:
    try:
        data = json.loads(text or "")
    except Exception:  # noqa: BLE001
        return False, 0.0
    entry = data.get("a") if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return False, 0.0
    try:
        score = float(entry.get("score"))
    except (TypeError, ValueError):
        return False, 0.0
    return True, round(max(0.0, min(score, 100.0)) / 100.0, 3)


def _skipped_result(role: str, model: str, runs: int) -> BenchResult:
    return BenchResult(
        role=role, model=model, provider=resolve(model).provider,
        runs_attempted=max(1, int(runs)), runs_completed=0,
        median_latency_s=None, p90_latency_s=None, quality_score=0.0,
        json_compliance=0.0 if role == JUDGE_ROLE else 1.0,
        error_rate=1.0, free_tier_preference=_free_tier_preference(model),
    )


def _key_for_provider(provider: str) -> str:
    env_name = f"{provider.upper()}_API_KEY"
    return getattr(settings, f"{provider}_api_key", "") or os.environ.get(env_name, "")


def _free_tier_preference(model: str) -> float:
    provider = resolve(model).provider
    if provider in {"ollama", "groq", "cerebras", "gemini"}:
        return 1.0
    return 1.0 if model.endswith("/free") or ":free" in model else 0.5


def _bench_timeout() -> float:
    return float(getattr(settings, "llm_call_timeout_s", 28) or 28)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((pct / 100.0) * (len(ordered) - 1))
    return round(ordered[int(index)], 3)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out
