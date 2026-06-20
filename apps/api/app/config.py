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

    # Datastores
    database_url: str = "postgresql+asyncpg://debate:debate@postgres:5432/debate"
    redis_url: str = "redis://redis:6379/0"

    # Memory hot-cache (RedisVL vector index in front of pgvector)
    memory_cache_enabled: bool = True
    redis_index_name: str = "mem_idx"

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
