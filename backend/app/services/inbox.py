"""Request collector inbox: whatever the collectors (supplier mail, MAX) forward to a user's bot chat.

Every forwarded item is stored once (user, source, external id) so a restart or a second poll never
sends it twice, and the "➕ в задачи" button under the bot message can turn it into a task."""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import InboxItem
from app.schemas import TaskIn
from app.services import bot_access
from app.services.tasks import create_task

log = logging.getLogger(__name__)
router = Router()

TG_LIMIT = 4000  # Telegram allows 4096 characters per message


def task_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➕ в задачи", callback_data=f"inbox:task:{item_id}")]]
    )


async def store(
    db: AsyncSession, user_id: int, source: str, external_id: str, sender: str, subject: str, text: str
) -> InboxItem | None:
    """Saves the item; None when it was already forwarded."""
    exists = await db.scalar(
        select(InboxItem.id).where(
            InboxItem.user_id == user_id, InboxItem.source == source, InboxItem.external_id == external_id
        )
    )
    if exists:
        return None
    item = InboxItem(
        user_id=user_id, source=source, external_id=external_id[:300], sender=sender[:200],
        subject=subject[:500], text=text,
    )  # fmt: skip
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def deliver(user_id: int, text: str, item_id: int | None = None) -> None:
    """To the user's private chat with the bot."""
    from app.services.telegram_bot import send_to_user

    acc = bot_access.get(user_id)
    if acc is None or not acc.telegram_id:
        return
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 1] + "…"
    await send_to_user(acc.telegram_id, text, reply_markup=task_keyboard(item_id) if item_id else None)


def task_title(item: InboxItem) -> str:
    if item.source == "mail":
        head = f"{item.sender}: {item.subject or 'письмо без темы'}"
    else:
        first = " ".join(item.text.split())
        head = f"MAX, {item.sender}: {first[:80]}{'…' if len(first) > 80 else ''}"
    return head[:500]


@router.callback_query(F.data.startswith("inbox:task:"))
async def cb_inbox_task(query: CallbackQuery) -> None:
    acc = bot_access.user_for(query.from_user.id)
    item_id = int(query.data.rsplit(":", 1)[1])
    async with SessionLocal() as db:
        item = await db.get(InboxItem, item_id)
        if not acc or not acc.has_workspace or not item or item.user_id != acc.id:
            await query.answer("Нет доступа", show_alert=True)
            return
        task = await create_task(
            db,
            TaskIn(title=task_title(item), notes=item.text[:10000] or None),
            source=item.source,
            source_ref=f"inbox:{item.id}",
            user_id=item.user_id,
        )
    await query.answer(f"Добавлено: #{task.id}")
