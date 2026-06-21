"""Model registry — maps friendly aliases to (provider, model_id).

Bottom-up capability approach: aliases default to local Ollama models. Swap a
single entry's provider to escalate a specific agent (e.g. pin the judge to
Anthropic) without touching call sites.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class ModelRef:
    provider: str  # ollama | anthropic | openai | groq | cerebras | gemini | openrouter
    model: str


# Static aliases. Anything not listed is treated as "<provider>/<model>" or a
# bare model name on the default provider.
REGISTRY: dict[str, ModelRef] = {
    "default": ModelRef("ollama", settings.llm_default_model),
    "judge": ModelRef("ollama", settings.llm_judge_model),
    "gemma": ModelRef("ollama", "gemma3:4b"),
    "qwen": ModelRef("ollama", "qwen3:4b"),
    "claude": ModelRef("anthropic", "claude-sonnet-4-6"),
    "claude-opus": ModelRef("anthropic", "claude-opus-4-8"),
    "gpt": ModelRef("openai", "gpt-4o-mini"),
    "groq-fast": ModelRef("groq", "llama-3.1-8b-instant"),
    "groq-judge": ModelRef("groq", "llama-3.3-70b-versatile"),
    "cerebras-fast": ModelRef("cerebras", "llama-3.3-70b"),
    "gemini-fast": ModelRef("gemini", "gemini-2.5-flash-lite"),
    "gemini-judge": ModelRef("gemini", "gemini-2.5-flash"),
    "openrouter-free": ModelRef("openrouter", "openrouter/free"),
}


def resolve(alias_or_model: str | None) -> ModelRef:
    """Resolve an alias, a 'provider/model' string, or a bare model name."""
    if not alias_or_model:
        return REGISTRY["default"]
    if alias_or_model in REGISTRY:
        return REGISTRY[alias_or_model]
    if "/" in alias_or_model:
        provider, _, model = alias_or_model.partition("/")
        if provider in {
            "ollama",
            "anthropic",
            "openai",
            "groq",
            "cerebras",
            "gemini",
            "openrouter",
        }:
            return ModelRef(provider, model)
    # Bare name -> default provider.
    return ModelRef(settings.llm_provider, alias_or_model)
