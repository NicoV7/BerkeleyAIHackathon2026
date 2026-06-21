"""Offline tests for latency-first Pareto gateway selection."""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

from app.gateway import pareto


@pytest.fixture(autouse=True)
def _reset_pareto_cache():
    pareto.clear_cache()
    yield
    pareto.clear_cache()


class _FakeCompleteClient:
    """Small fake that returns provider-shaped text without network calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, model=None, json_mode=False, **kwargs):
        self.calls.append({"model": model, "json_mode": json_mode})
        if model == "groq/fast-bad-json":
            return "not json"
        if json_mode:
            return json.dumps({"a": {"score": 80, "rationale": "solid"}})
        return "Remote work is better because it saves commute time and improves focus."


def test_fallback_order_uses_configured_order_before_benchmark(monkeypatch):
    # Arrange
    monkeypatch.setattr(
        pareto.settings,
        "gateway_actor_candidates",
        "groq/fast,cerebras/strong,ollama/gemma3:1b",
    )
    monkeypatch.setattr(pareto.settings, "groq_api_key", "fake")
    monkeypatch.setattr(pareto.settings, "cerebras_api_key", "fake")

    # Act
    order = pareto.fallback_order(pareto.ACTOR_ROLE)

    # Assert
    assert order == ["groq/fast", "cerebras/strong", "ollama/gemma3:1b"]


def test_fallback_order_filters_unconfigured_hosted_providers(monkeypatch):
    # Arrange
    monkeypatch.setattr(
        pareto.settings,
        "gateway_actor_candidates",
        "groq/fast,cerebras/strong,ollama/gemma3:1b",
    )
    monkeypatch.setattr(pareto.settings, "groq_api_key", "")
    monkeypatch.setattr(pareto.settings, "cerebras_api_key", "fake")

    # Act / Assert
    assert pareto.fallback_order(pareto.ACTOR_ROLE) == [
        "cerebras/strong",
        "ollama/gemma3:1b",
    ]


def test_frontier_prefers_fast_non_dominated_candidate(monkeypatch):
    # Arrange: fast dominates slow on latency with equal-or-better dimensions.
    monkeypatch.setattr(
        pareto.settings,
        "gateway_actor_candidates",
        "groq/fast,cerebras/slow,ollama/gemma3:1b",
    )
    monkeypatch.setattr(pareto.settings, "groq_api_key", "fake")
    monkeypatch.setattr(pareto.settings, "cerebras_api_key", "fake")
    pareto._BENCH_CACHE[pareto.ACTOR_ROLE] = [  # noqa: SLF001 - intentional cache setup
        pareto.BenchResult(
            role=pareto.ACTOR_ROLE,
            model="groq/fast",
            provider="groq",
            runs_attempted=3,
            runs_completed=3,
            median_latency_s=0.2,
            p90_latency_s=0.3,
            quality_score=0.7,
            json_compliance=1.0,
            error_rate=0.0,
            free_tier_preference=1.0,
        ),
        pareto.BenchResult(
            role=pareto.ACTOR_ROLE,
            model="cerebras/slow",
            provider="cerebras",
            runs_attempted=3,
            runs_completed=3,
            median_latency_s=0.5,
            p90_latency_s=0.8,
            quality_score=0.7,
            json_compliance=1.0,
            error_rate=0.0,
            free_tier_preference=1.0,
        ),
    ]

    # Act
    order = pareto.fallback_order(pareto.ACTOR_ROLE)

    # Assert
    assert order[0] == "groq/fast"
    assert "ollama/gemma3:1b" in order


async def test_benchmark_role_scores_json_compliance(monkeypatch):
    # Arrange
    monkeypatch.setattr(
        pareto.settings,
        "gateway_judge_candidates",
        "groq/fast-bad-json,groq/good-json,ollama/gemma3:1b",
    )
    monkeypatch.setattr(pareto.settings, "groq_api_key", "fake")
    client = _FakeCompleteClient()

    # Act
    result = await pareto.benchmark_role(client, pareto.JUDGE_ROLE, runs=2)

    # Assert
    by_model = {r["model"]: r for r in result["results"]}
    assert by_model["groq/fast-bad-json"]["json_compliance"] == 0.0
    assert by_model["groq/good-json"]["json_compliance"] == 1.0
    assert result["selected"] != "groq/fast-bad-json"


def test_redacted_status_never_contains_secret_values(monkeypatch):
    # Arrange
    monkeypatch.setattr(pareto.settings, "groq_api_key", "super-secret")

    # Act
    status = pareto.redacted_status()
    dumped = json.dumps(status)

    # Assert
    assert "super-secret" not in dumped
    assert status["providers"]["groq"]["configured"] is True


async def test_pareto_status_router_returns_redacted_payload(monkeypatch):
    # Arrange
    from app.routers.models import pareto_status

    monkeypatch.setattr(pareto.settings, "openrouter_api_key", "router-secret")

    # Act
    payload = await pareto_status()

    # Assert
    dumped = json.dumps(payload)
    assert "router-secret" not in dumped
    assert payload["providers"]["openrouter"]["configured"] is True
    assert "actor" in payload["roles"]


async def test_live_model_bench_smoke_is_opt_in():
    if os.environ.get("RUN_LIVE_MODEL_BENCH") != "1":
        pytest.skip("set RUN_LIVE_MODEL_BENCH=1 to run live provider smoke bench")
    from app.gateway.gateway import gateway

    result = await pareto.benchmark_role(gateway, pareto.ACTOR_ROLE, runs=1)
    assert result["results"]
