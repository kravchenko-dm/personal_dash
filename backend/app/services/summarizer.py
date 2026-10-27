"""Summaries and task proposals via Claude (structured JSON output)."""

import json
import logging
from dataclasses import dataclass, field

import anthropic

from app.config import get_settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — личный ассистент владельца дашборда. Тебе дают выгрузку сообщений из
Telegram-чатов (рабочие и личные), текущий список открытых задач и события календаря.

Сделай короткое саммари на русском: что важного произошло, какие решения приняты, что
требует внимания владельца. Затем предложи задачи, которые логично завести, исходя из переписки.
Не дублируй уже существующие открытые задачи. Предлагай только конкретные, выполнимые действия с
понятной формулировкой; если в переписке нет поводов для задач, верни пустой список."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Саммари в 3-10 предложениях, markdown без заголовков"},
        "highlights": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ключевые пункты, 0-7 штук",
        },
        "proposed_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "reason": {"type": "string", "description": "Почему задача предложена, 1 фраза"},
                    "priority": {"type": "integer", "description": "1 высокий, 2 обычный, 3 низкий"},
                    "chat": {"type": "string", "description": "Название чата-источника"},
                },
                "required": ["title", "reason", "priority", "chat"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "highlights", "proposed_tasks"],
    "additionalProperties": False,
}


@dataclass
class SummaryResult:
    summary: str
    highlights: list[str] = field(default_factory=list)
    proposed_tasks: list[dict] = field(default_factory=list)

    def as_markdown(self) -> str:
        parts = [self.summary.strip()]
        if self.highlights:
            parts.append("\n".join(f"• {h}" for h in self.highlights))
        return "\n\n".join(parts)


async def summarize(
    messages_text: str, open_tasks: str, calendar_text: str = "", period_label: str = ""
) -> SummaryResult:
    s = get_settings()
    if not s.claude_enabled:
        raise RuntimeError("ANTHROPIC_API_KEY не задан")

    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
    user_content = (
        f"Период: {period_label or 'последние сообщения'}\n\n"
        f"## Открытые задачи\n{open_tasks}\n\n"
        f"## Календарь\n{calendar_text or 'нет данных'}\n\n"
        f"## Сообщения\n{messages_text}"
    )

    response = await client.beta.messages.create(
        model=s.claude_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude отказался обрабатывать запрос")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return SummaryResult(
        summary=data["summary"],
        highlights=data.get("highlights", []),
        proposed_tasks=data.get("proposed_tasks", []),
    )
