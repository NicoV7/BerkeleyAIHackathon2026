"""Centralized settings loaded from environment / .env.

Every subsystem (gateway, db, redis) reads from this single `settings` object so
Wave 1 workstreams share one source of truth for configuration.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# pydantic resolves a bare ``env_file=".env"`` relative to the PROCESS CWD, but
# uvicorn runs from ``apps/api/`` while the real ``.env`` (with provider API
# keys) lives at the repo root. That mismatch silently loaded zero keys, so the
# gateway fell back to local Ollama and battles/NPC dialogue came out as canned
# stubs. Resolve absolute candidate paths from this file's location instead, so
# the keys load no matter where the API is started. ``apps/api/.env`` is kept as
# a secondary location; os.environ (e.g. docker compose env_file) still wins.
_API_DIR = Path(__file__).resolve().parents[1]  # apps/api
_REPO_ROOT = Path(__file__).resolve().parents[3]  # repo root


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", _API_DIR / ".env"),
        extra="ignore",
    )

    # Model gateway
    llm_provider: str = "ollama"
    llm_default_model: str = "gemma3:4b"
    llm_judge_model: str = "gemma3:4b"
    llm_embed_model: str = "nomic-embed-text"
    gateway_fallback_enabled: bool = True
    gateway_actor_candidates: str = (
        "groq/llama-3.1-8b-instant,"
        "cerebras/llama-3.3-70b,"
        "gemini/gemini-2.5-flash-lite,"
        "openrouter/openrouter/free,"
        "ollama/gemma3:1b"
    )
    gateway_judge_candidates: str = (
        "groq/llama-3.3-70b-versatile,"
        "cerebras/llama-3.3-70b,"
        "gemini/gemini-2.5-flash,"
        "ollama/gemma3:1b"
    )

    # --- Latency fast-path (battle playability) ------------------------------
    # The default debater/judge models (gemma3:4b) are too slow on a contended
    # local CPU: rounds were timing out at the gateway's old 120s ceiling, so
    # every utterance fell back to a useless stub. These additive knobs route
    # actor turns + the judge to a SMALL, fast model and cap every LLM call at a
    # short per-call timeout so a stuck call fails fast (and we render real
    # fallback text) instead of hanging the whole round.
    #
    #   actor_model       — fast model for combatant turns / enemy rebuttals.
    #   judge_model_fast  — fast model for scoring (defaults to actor_model).
    #   llm_call_timeout_s— per-call wall-clock budget for a single NON-streaming
    #                       completion (actor `complete`, judge). RAISED to ~28s so
    #                       a real argument has room to finish instead of failing
    #                       over to templated text under contention. The streaming
    #                       path does NOT use this — see first_token_timeout_s.
    #   first_token_timeout_s — SMALL guard on the live streaming path: how long we
    #                       wait for the model's FIRST token before failing this one
    #                       utterance over to a templated fallback. Keeping it small
    #                       (~8s) means "first token <= 6-8s" holds and a
    #                       slow-to-start model never hangs the WS round. Tokens
    #                       AFTER the first stream freely (the round wall-clock is
    #                       bounded by ROUND_TIMEOUT_S + actor_max_tokens).
    #   actor_max_tokens  — cap on an actor turn's length. Small (~64) keeps turns
    #                       to a punchy 1-2 sentences and bounds generation time.
    #   prewarm_enabled   — fire a tiny throwaway call on encounter creation so
    #                       the first real turn isn't paying the cold-load tax.
    #
    # Latency budget vs ROUND_TIMEOUT_S (routers/debate.py == 120s):
    #   * The live auto/human rounds STREAM, so per-actor first-token is capped at
    #     first_token_timeout_s (~8s) and total length is bounded by
    #     actor_max_tokens (~64) — a 1-party + N-enemy round stays well under 120s.
    #   * The non-streaming `complete` (headless self-play: 2 combatants) is capped
    #     at llm_call_timeout_s. We keep it at ~28s so even a hypothetical N=4 actor
    #     round of complete() calls (4 * 28 = 112s) stays under ROUND_TIMEOUT_S.
    # Installed Ollama models, fastest-first-ish: gemma3:1b, llama3.2:3b,
    # qwen2.5:3b, phi3:mini, gemma3:4b.
    # ONE model for actors AND judge so single-slot CPU Ollama never swaps models
    # mid-round (model-swap thrash was the measured ~75s/round bottleneck). gemma3:1b
    # is the fastest installed model and its argument quality was validated good. The
    # fabricated/wild enemies are already pinned to gemma3:1b, so everything aligns →
    # one warm model, no swaps.
    actor_model: str = "gemma3:1b"
    judge_model_fast: str = "gemma3:1b"
    llm_call_timeout_s: int = 28
    first_token_timeout_s: int = 15  # cold gemma3:1b first token can take >8s; 15 avoids premature fallback
    actor_max_tokens: int = 64
    prewarm_enabled: bool = True
    # WS-4 warm-path latency. Once the actor model is confirmed WARM (a prewarm
    # ping at encounter create succeeded, so the model is resident in Ollama and
    # not paying the cold-load tax), the live streaming path may wait a bit longer
    # for the first token before failing over to a templated fallback. This LOWERS
    # the fallback rate on a healthy-but-busy model without re-introducing the cold
    # hang: a truly stalled model is still bounded by `first_token_timeout_s` until
    # it's marked warm. Set to first_token_timeout_s to disable the widening.
    first_token_timeout_warm_s: int = 22
    # Ollama keep_alive sent on the encounter-create prewarm so the actor model
    # stays resident across the battle's idle gaps (turn-to-turn thinking + the
    # player typing). A string Ollama accepts ("10m") or seconds. Empty -> omit.
    ollama_keep_alive: str = "10m"
    ollama_base_url: str = "http://ollama:11434"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Judge provider switch (additive; default keeps the judge fully local).
    #
    # Pin the judge to Claude to cut scoring variance while debaters stay on
    # Ollama, WITHOUT touching the debater models. Two ways, in priority order:
    #
    #   1. Set ``JUDGE_MODEL`` (env, read by app.debate.judge) to a routable id
    #      such as ``claude`` or ``anthropic/claude-sonnet-4-6``. This wins.
    #   2. Or set ``judge_provider=anthropic`` here. ``judge_model_id`` then
    #      resolves to ``<judge_provider>/<llm_judge_model>`` so the existing
    #      ``judge`` alias / llm_judge_model can be escalated to Claude in one
    #      place. Leaving ``judge_provider`` empty preserves the local default.
    #
    # An Anthropic judge requires ``anthropic_api_key``; if it's missing the
    # gateway raises a clear error rather than silently degrading.
    judge_provider: str = ""  # "" | "ollama" | "anthropic" | "openai"

    @property
    def judge_model_id(self) -> str:
        """Routable model id for the judge, honoring the optional provider pin.

        With no ``judge_provider`` set this returns ``llm_judge_model`` unchanged
        (local default). With a provider set it returns ``<provider>/<model>``,
        which gateway.resolve() routes to that provider's adapter.
        """
        prov = self.judge_provider.strip().lower()
        if prov and prov in {"ollama", "anthropic", "openai"}:
            return f"{prov}/{self.llm_judge_model}"
        return self.llm_judge_model

    # Datastores
    database_url: str = "postgresql+asyncpg://debate:debate@postgres:5432/debate"
    redis_url: str = "redis://redis:6379/0"

    # Memory hot-cache (RedisVL vector index in front of pgvector)
    memory_cache_enabled: bool = True
    redis_index_name: str = "mem_idx"

    # Agent-generated world (Wave 3). OFF by default: local-without-good-model is
    # unaffected and /world stays purely procedural. When True, /world tries the
    # LLM generator and falls back to procedural on any failure.
    world_gen_enabled: bool = False

    # Onboarding (WS-2): a NEW run starts with NO party agents so the scripted
    # intro NPC grants the first agent via the gacha pull / onboarding endpoint.
    # ON by default (the onboarding flow is the intended new-game experience);
    # flip to False to restore the legacy auto-rolled 2-3 monster starter party.
    empty_start_enabled: bool = True

    # Living-layer hosted LLM adapter (Wave 4) — completely-free providers only.
    # The hosted adapter (app/llm/hosted_adapter.py) round-robins across these in
    # priority order with retry-on-429 failover, and degrades to a static stub
    # response when no keys are configured (so offline-dev never hangs).
    #
    # IMPORTANT: never commit real values for these. Keep them in .env.local.
    # App
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
