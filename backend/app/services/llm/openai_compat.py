"""Any provider that speaks the OpenAI Chat Completions API: Kimi (Moonshot), DeepSeek,
OpenRouter, OpenAI, Polza.AI, Ollama, Qwen (DashScope) and custom endpoints."""

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.llm.base import LLMError, parse_json_loosely


@dataclass(frozen=True)
class Preset:
    base_url: str
    model: str
    needs_key: bool = True
    json_mode: bool = True  # send response_format={"type": "json_object"}
    json_schema: bool = False  # provider accepts response_format={"type": "json_schema", ...}


# Model names change often: override with LLM_MODEL in .env.
PRESETS: dict[str, Preset] = {
    "kimi": Preset("https://api.moonshot.ai/v1", "kimi-k2-0905-preview"),
    "deepseek": Preset("https://api.deepseek.com/v1", "deepseek-chat"),
    "openrouter": Preset("https://openrouter.ai/api/v1", "moonshotai/kimi-k2"),
    "openai": Preset("https://api.openai.com/v1", "gpt-5"),
    # Polza.AI: Russian aggregator, model ids are "vendor/model" (GET /api/v1/models?type=chat),
    # pay in rubles. Options go into the model string: "anthropic/claude-sonnet-5.5@reasoning_effort=low".
    "polza": Preset("https://polza.ai/api/v1", "anthropic/claude-sonnet-5.5", json_schema=True),
    "qwen": Preset("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "ollama": Preset("http://ollama:11434/v1", "qwen2.5:14b", needs_key=False),
    "custom": Preset("", "", needs_key=False, json_mode=False),
}


class OpenAICompatClient:
    def __init__(
        self, base_url: str, api_key: str, model: str, provider: str = "custom", timeout: float = 120
    ):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        preset = PRESETS.get(provider, PRESETS["custom"])
        self.json_mode = preset.json_mode
        self.json_schema = preset.json_schema

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider in ("openrouter", "polza"):
            headers["X-Title"] = "Personal Dash"
        return headers

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        # No universal structured-output API across vendors: put the schema into the prompt and,
        # where supported, switch on JSON mode. The result is validated by the caller anyway.
        system_with_schema = (
            f"{system}\n\nОтвечай ТОЛЬКО валидным JSON без пояснений и без markdown, строго по этой JSON-схеме:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_with_schema},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
        }
        if self.json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": False},
            }
        elif self.json_mode:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions", json=body, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"{self.provider}: нет соединения ({exc})") from exc
        if (
            resp.status_code == 400
            and (self.json_schema or self.json_mode)
            and "response_format" in resp.text
        ):
            # provider rejects this response_format -> degrade json_schema -> json_object -> none
            if self.json_schema:
                self.json_schema = False
            else:
                self.json_mode = False
            return await self.complete_json(system, user, schema)
        if resp.status_code >= 400:
            raise LLMError(f"{self.provider} {resp.status_code}: {resp.text[:300]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"{self.provider}: неожиданный ответ {resp.text[:200]!r}") from exc
        if isinstance(content, list):  # some vendors return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return parse_json_loosely(content or "")
