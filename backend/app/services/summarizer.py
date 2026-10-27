"""Summaries and task proposals: prompt + schema live here, the model is pluggable (services.llm)."""

import logging

from pydantic import BaseModel, Field, ValidationError

from app.services.llm import LLMError, get_llm

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — личный ассистент владельца дашборда. Тебе дают выгрузку сообщений из
Telegram-чатов (рабочие и личные), текущий список открытых задач и события календаря.

Сделай короткое саммари на русском: что важного произошло, какие решения приняты, что
требует внимания владельца. Затем предложи задачи, которые логично завести, исходя из переписки.
Не дублируй уже существующие открытые задачи. Предлагай только конкретные, выполнимые действия с
понятной формулировкой; если в переписке нет поводов для задач, верни пустой список."""


class ProposedTask(BaseModel):
    title: str
    reason: str = ""
    priority: int = Field(default=2, ge=1, le=3)
    chat: str = ""


class SummaryResult(BaseModel):
    summary: str
    highlights: list[str] = Field(default_factory=list)
    proposed_tasks: list[ProposedTask] = Field(default_factory=list)

    def as_markdown(self) -> str:
        parts = [self.summary.strip()]
        if self.highlights:
            parts.append("\n".join(f"• {h}" for h in self.highlights))
        return "\n\n".join(parts)

    def tasks_as_dicts(self) -> list[dict]:
        return [t.model_dump() for t in self.proposed_tasks]


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Саммари в 3-10 предложениях, без заголовков"},
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


async def summarize(
    messages_text: str, open_tasks: str, calendar_text: str = "", period_label: str = ""
) -> SummaryResult:
    llm = get_llm()
    user_content = (
        f"Период: {period_label or 'последние сообщения'}\n\n"
        f"## Открытые задачи\n{open_tasks}\n\n"
        f"## Календарь\n{calendar_text or 'нет данных'}\n\n"
        f"## Сообщения\n{messages_text}"
    )
    data = await llm.complete_json(SYSTEM_PROMPT, user_content, OUTPUT_SCHEMA)
    try:
        result = SummaryResult.model_validate(data)
    except ValidationError as exc:
        raise LLMError(
            f"{llm.provider}/{llm.model}: ответ не соответствует схеме: {exc.errors()[0]['msg']}"
        ) from exc
    log.info("summary via %s/%s: %d proposals", llm.provider, llm.model, len(result.proposed_tasks))
    return result
