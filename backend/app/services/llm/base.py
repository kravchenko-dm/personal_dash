import json
import re
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    provider: str
    model: str

    async def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return a dict matching `schema` (JSON Schema). Raise LLMError on failure."""
        ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def parse_json_loosely(text: str) -> dict[str, Any]:
    """Models without a strict JSON mode sometimes wrap JSON in fences or prose; cope with that."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            raise LLMError(f"Модель вернула не JSON: {text[:200]!r}") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Не удалось разобрать JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("Ожидался JSON-объект")
    return data
