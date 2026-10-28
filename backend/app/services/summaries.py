"""Collect Telegram messages + tasks + calendar, ask Claude, store the result."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Summary, TelegramChat, TelegramMessage
from app.services import agenda, apple_reminders, summarizer
from app.services import projects as projects_svc
from app.services.tasks import open_tasks_text

log = logging.getLogger(__name__)
MAX_CHARS = 120_000  # ~30k tokens of chat text per summary request


async def _messages_text(
    db: AsyncSession, chat_ids: list[int] | None, since: datetime, until: datetime
) -> str:
    chats = {c.chat_id: c for c in (await db.scalars(select(TelegramChat))).all()}
    stmt = (
        select(TelegramMessage)
        .where(TelegramMessage.sent_at >= since, TelegramMessage.sent_at < until)
        .order_by(TelegramMessage.chat_id, TelegramMessage.sent_at)
    )
    if chat_ids is not None:
        if not chat_ids:
            return ""
        stmt = stmt.where(TelegramMessage.chat_id.in_(chat_ids))
    rows = list((await db.scalars(stmt)).all())
    lines: list[str] = []
    current: int | None = None
    for m in rows:
        if m.chat_id != current:
            current = m.chat_id
            title = chats[m.chat_id].title if m.chat_id in chats else str(m.chat_id)
            lines.append(f"\n### Чат: {title}")
        stamp = m.sent_at.astimezone().strftime("%d.%m %H:%M")
        lines.append(f"[{stamp}] {m.sender}: {m.text}")
    text = "\n".join(lines).strip()
    if len(text) > MAX_CHARS:
        text = "…(начало обрезано)…\n" + text[-MAX_CHARS:]
    return text


async def build_summary(
    db: AsyncSession,
    chat_id: int | None = None,
    hours: int = 24,
    mark_summarized: bool = False,
    project_id: int | None = None,
) -> Summary | None:
    """Summarize one chat, all chats, or all chats bound to a project. When the chat belongs to a
    project (or project_id is given) the prompt gets the project's description and open features
    so the model proposes features for the board instead of generic tasks."""
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)

    project: Project | None = None
    chat_ids: list[int] | None = None
    if chat_id is not None:
        chat_ids = [chat_id]
        chat = await db.scalar(select(TelegramChat).where(TelegramChat.chat_id == chat_id))
        if chat and chat.project_id and project_id is None:
            project_id = chat.project_id
    if project_id is not None:
        project = await db.get(Project, project_id)
        if project is None:
            raise RuntimeError("Проект не найден")
        if chat_ids is None:
            chat_ids = [c.chat_id for c in await projects_svc.chats_for_project(db, project_id)]

    text = await _messages_text(db, chat_ids, since, until)
    if not text:
        return None

    tasks = await open_tasks_text(db)
    if project is not None:
        features = projects_svc.open_features_text(await projects_svc.project_features(db, project.id))
        tasks = (
            f"## Проект: {project.name} (стадия {project.stage})\n{project.description or ''}\n"
            f"Текущий статус: {project.status_note or 'не указан'}\n\n"
            f"## Фичи проекта на доске\n{features}\n\n## Открытые задачи\n{tasks}"
        )
    reminders = apple_reminders.reminders_text(await apple_reminders.list_reminders(db))
    if reminders:
        tasks += f"\n\nНапоминания Apple:\n{reminders}"
    calendar_text = ""
    try:
        calendar_text = await agenda.upcoming_text(db, days=3)
    except Exception as exc:
        log.info("calendar unavailable for summary: %s", exc)

    label = f"{since.astimezone():%d.%m %H:%M} — {until.astimezone():%d.%m %H:%M}"
    result = await summarizer.summarize(text, tasks, calendar_text, label)

    row = Summary(
        chat_id=chat_id,
        project_id=project.id if project else None,
        period_start=since,
        period_end=until,
        text=result.as_markdown(),
        proposed_tasks=result.tasks_as_dicts(),
    )
    db.add(row)
    if mark_summarized:
        stmt = (
            update(TelegramMessage)
            .where(TelegramMessage.sent_at >= since, TelegramMessage.sent_at < until)
            .values(summarized=True)
        )
        if chat_ids is not None:
            stmt = stmt.where(TelegramMessage.chat_id.in_(chat_ids))
        await db.execute(stmt)
    await db.commit()
    await db.refresh(row)
    return row
