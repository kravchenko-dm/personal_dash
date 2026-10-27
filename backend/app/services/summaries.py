"""Collect Telegram messages + tasks + calendar, ask Claude, store the result."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Summary, TelegramChat, TelegramMessage
from app.services import agenda, summarizer
from app.services.tasks import open_tasks_text

log = logging.getLogger(__name__)
MAX_CHARS = 120_000  # ~30k tokens of chat text per summary request


async def _messages_text(db: AsyncSession, chat_id: int | None, since: datetime, until: datetime) -> str:
    chats = {c.chat_id: c for c in (await db.scalars(select(TelegramChat))).all()}
    stmt = (
        select(TelegramMessage)
        .where(TelegramMessage.sent_at >= since, TelegramMessage.sent_at < until)
        .order_by(TelegramMessage.chat_id, TelegramMessage.sent_at)
    )
    if chat_id is not None:
        stmt = stmt.where(TelegramMessage.chat_id == chat_id)
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
) -> Summary | None:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    text = await _messages_text(db, chat_id, since, until)
    if not text:
        return None

    tasks = await open_tasks_text(db)
    calendar_text = ""
    try:
        calendar_text = await agenda.upcoming_text(db, days=3)
    except Exception as exc:
        log.info("calendar unavailable for summary: %s", exc)

    label = f"{since.astimezone():%d.%m %H:%M} — {until.astimezone():%d.%m %H:%M}"
    result = await summarizer.summarize(text, tasks, calendar_text, label)

    row = Summary(
        chat_id=chat_id,
        period_start=since,
        period_end=until,
        text=result.as_markdown(),
        proposed_tasks=result.proposed_tasks,
    )
    db.add(row)
    if mark_summarized:
        stmt = (
            update(TelegramMessage)
            .where(TelegramMessage.sent_at >= since, TelegramMessage.sent_at < until)
            .values(summarized=True)
        )
        if chat_id is not None:
            stmt = stmt.where(TelegramMessage.chat_id == chat_id)
        await db.execute(stmt)
    await db.commit()
    await db.refresh(row)
    return row
