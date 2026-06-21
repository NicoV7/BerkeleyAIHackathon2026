"""Centralized settings loaded from environment / .env.

Every subsystem (gateway, db, redis) reads from this single `settings` object so
Wave 1 workstreams share one source of truth for configuration.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Model gateway
    llm_provider: str = "ollama"
    llm_default_model: str = "gemma3:4b"
    llm_judge_model: str = "gemma3:4b"
    llm_embed_model: str = "nomic-embed-text"
    ollama_base_url: str = "http://ollama:11434"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

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
