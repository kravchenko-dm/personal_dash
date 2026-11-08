"""Pluggable LLM backends.

Pick one with LLM_PROVIDER in .env:
  anthropic  -> Claude via the official SDK (default)
  kimi, deepseek, openrouter, openai, polza, ollama, qwen, custom -> any OpenAI-compatible chat API

Each backend implements `complete_json(system, user, schema) -> dict`.
"""

from app.config import Settings, get_settings
from app.services.llm.base import LLMClient, LLMError
from app.services.llm.openai_compat import PRESETS, OpenAICompatClient


def describe(settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    provider = s.llm_provider.lower()
    if provider == "anthropic":
        return {"provider": "anthropic", "model": s.claude_model, "enabled": bool(s.anthropic_api_key)}
    preset = PRESETS.get(provider, PRESETS["custom"])
    model = s.llm_model or preset.model
    base_url = s.llm_base_url or preset.base_url
    enabled = bool(base_url and model and (s.llm_api_key or not preset.needs_key))
    return {"provider": provider, "model": model, "enabled": enabled}


def get_llm(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    provider = s.llm_provider.lower()
    if provider == "anthropic":
        from app.services.llm.anthropic_client import AnthropicClient

        if not s.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY не задан")
        return AnthropicClient(api_key=s.anthropic_api_key, model=s.claude_model)

    preset = PRESETS.get(provider)
    if preset is None:
        raise LLMError(f"Неизвестный LLM_PROVIDER={provider!r}. Доступны: anthropic, {', '.join(PRESETS)}")
    base_url = s.llm_base_url or preset.base_url
    model = s.llm_model or preset.model
    if not base_url or not model:
        raise LLMError("Для этого провайдера нужны LLM_BASE_URL и LLM_MODEL")
    if preset.needs_key and not s.llm_api_key:
        raise LLMError("LLM_API_KEY не задан")
    return OpenAICompatClient(base_url=base_url, api_key=s.llm_api_key, model=model, provider=provider)


__all__ = ["LLMClient", "LLMError", "get_llm", "describe", "PRESETS"]
