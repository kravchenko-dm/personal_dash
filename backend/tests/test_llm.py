import httpx
import pytest

from app.config import Settings
from app.services import summarizer
from app.services.llm import LLMError, describe, get_llm
from app.services.llm.base import parse_json_loosely
from app.services.llm.openai_compat import OpenAICompatClient


def test_describe_and_select_provider():
    s = Settings(llm_provider="kimi", llm_api_key="k")
    info = describe(s)
    assert info == {"provider": "kimi", "model": "kimi-k2-0905-preview", "enabled": True}
    client = get_llm(s)
    assert isinstance(client, OpenAICompatClient) and client.base_url == "https://api.moonshot.ai/v1"

    assert describe(Settings(llm_provider="kimi"))["enabled"] is False
    assert describe(Settings(llm_provider="anthropic", anthropic_api_key="x"))["model"] == "claude-opus-5-5"
    assert describe(Settings(llm_provider="ollama", llm_base_url="http://h:11434/v1"))["enabled"] is True

    with pytest.raises(LLMError):
        get_llm(Settings(llm_provider="nope"))
    with pytest.raises(LLMError):
        get_llm(Settings(llm_provider="custom"))  # needs base url + model
    with pytest.raises(LLMError):
        get_llm(Settings(llm_provider="anthropic"))  # no key


def test_parse_json_loosely():
    assert parse_json_loosely('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loosely('Вот ответ: {"a": {"b": 2}} спасибо') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        parse_json_loosely("no json here")


async def test_openai_compat_request_and_json_mode_fallback(monkeypatch):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append(body)
        assert request.url == "https://api.moonshot.ai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer k"
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        assert "JSON-схеме" in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '```json\n{"summary": "ок", "highlights": [], "proposed_tasks": []}\n```'}}]},
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    client = OpenAICompatClient("https://api.moonshot.ai/v1", "k", "kimi-k2", provider="kimi")
    data = await client.complete_json("sys", "user", summarizer.OUTPUT_SCHEMA)
    assert data["summary"] == "ок"
    assert len(calls) == 2 and "response_format" in calls[0] and "response_format" not in calls[1]
    assert calls[1]["model"] == "kimi-k2"


async def test_summarize_validates_schema(monkeypatch):
    class Bad:
        provider, model = "x", "y"

        async def complete_json(self, system, user, schema):
            return {"summary": "s", "proposed_tasks": [{"reason": "no title"}]}

    monkeypatch.setattr(summarizer, "get_llm", lambda: Bad())
    with pytest.raises(LLMError, match="не соответствует схеме"):
        await summarizer.summarize("msgs", "tasks")

    class Good(Bad):
        async def complete_json(self, system, user, schema):
            return {"summary": "s", "highlights": ["h"], "proposed_tasks": [{"title": "t", "reason": "r", "priority": 1, "chat": "c"}]}

    monkeypatch.setattr(summarizer, "get_llm", lambda: Good())
    result = await summarizer.summarize("msgs", "tasks")
    assert result.as_markdown() == "s\n\n• h"
    assert result.tasks_as_dicts()[0]["priority"] == 1
