import json
from typing import Any

import anthropic

from app.services.llm.base import LLMError


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str):
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Anthropic: превышен лимит запросов ({exc})") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Anthropic: нет соединения ({exc})") from exc
        if response.stop_reason == "refusal":
            raise LLMError("Claude отказался обрабатывать запрос")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Claude вернул невалидный JSON: {exc}") from exc
